#!/usr/bin/env python3
"""
PreToolUse guard: refuses irreversible actions, even in bypass mode.

Why a hook: sessions run with --dangerously-skip-permissions, with no prompt
at all. The permissions.deny rules of settings.json still apply in that mode
(permission-modes docs: "Deny rules block in every mode, including
bypassPermissions", reread on 2026-09-22), but they are static patterns. This
hook decides with context (current branch, cwd after cd, depth under home,
bash -c / eval) and returns permissionDecision=deny, which prevents the call
(hooks docs, PreToolUse decision control). Another PreToolUse hook that
rewrites commands (e.g. RTK) runs in parallel and sees the original command,
like this one.

What it refuses (rules in hooks/guard-rules.json, this script applies them):
  - Bash: recursive rm on a protected surface (root, home, any folder at
    depth <= 3 under home = every project, external volume at depth <= 2,
    protected prefixes such as ~/.ssh or ~/.claude/projects, .git);
    git push --force / --delete / +ref to a protected branch, git push
    --mirror, git branch -D of a protected branch, git reset --hard,
    git clean -f, git checkout/restore of the whole tree, git stash clear/drop;
    mkfs, dd to /dev, diskutil erase, shutdown/reboot, npm publish,
    gh repo delete, gh pr merge, docker volume rm/prune.
  - MCP tools: names matching deny_tools (shipped examples: destructive
    Hostinger tools, GitHub delete_repository / merge_pull_request / delete_file).
  Compound commands (&&, ||, ;, |), wrappers (sudo, env, and the RTK rewriter
  when installed: rtk, rtk proxy),
  bash -c / sh -c / eval and intermediate cd are followed. Heredoc bodies are
  ignored (text, not commands).

Accepted limits: a `python3 -c "shutil.rmtree(...)"`, a `find -delete` or an
`xargs rm` are not analysed. The goal is to stop common catastrophic
autonomous gestures, not to resist a deliberate workaround.

Escape hatch (controlled by the human, not by the model):
  touch ~/.claude/guard.off   -> the hook lets everything through and says so on stderr
Otherwise the human runs the command themselves with `! <cmd>` in the prompt.

On an internal error the hook lets the call through (fail-open) and writes to
stderr: a guard that breaks every command in bypass mode would be worse than none.

Refusal log: ~/.claude/guard.log (one JSON line per refusal).
Tests: tests/guard-tests.sh (synthetic payloads, expected deny/allow).
To unplug: remove the PreToolUse entry
"guard.py" from ~/.claude/settings.json.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.path.dirname(HERE)
RULES_PATH = os.path.join(HERE, "guard-rules.json")
OFF_FILE = os.path.join(CLAUDE_DIR, "guard.off")
LOG_PATH = os.path.join(CLAUDE_DIR, "guard.log")
HOME = os.path.expanduser("~")

MAX_NESTING = 3
WRAPPERS = {"sudo", "command", "nohup", "time", "exec", "nice", "caffeinate", "builtin"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
SEPARATORS = {"&&", "||", ";", ";;", "|", "|&", "&", "(", ")"}
REDIRECTS = {">", ">>", "<", "<<", "<<<", "2>", "&>", ">&", "2>&1"}
SYSTEM_TOPS = {
    "/Users", "/Volumes", "/System", "/Library", "/Applications", "/private",
    "/etc", "/usr", "/opt", "/bin", "/sbin", "/var", "/home", "/dev", "/cores",
}

# ---------------------------------------------------------------- outputs


def emit_allow():
    sys.stdout.write("{}\n")
    sys.stdout.flush()
    sys.exit(0)


def emit_deny(reason, payload):
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "tool": payload.get("tool_name"),
        "reason": reason,
        "input": json.dumps(payload.get("tool_input", {}), ensure_ascii=False)[:400],
    }
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    message = (
        "Refused by the guard (~/.claude/hooks/guard.py): " + reason + ". "
        "Irreversible action or protected surface: it is not executed, even in bypass mode. "
        "Do not work around it (no variant of the command, no other tool): explain to the human "
        "what you wanted to do and let them run it themselves (`! <command>`), "
        "or ask them to create ~/.claude/guard.off for the duration of the operation."
    )
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(0)


# ---------------------------------------------------------------- helpers


def load_rules():
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def expand_path(p):
    """Expand ~, $HOME, $TMPDIR; leaves other variables untouched."""
    p = p.strip().strip("'\"")
    if p == "~" or p.startswith("~/"):
        p = HOME + p[1:]
    p = p.replace("${HOME}", HOME).replace("$HOME", HOME)
    tmpdir = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    p = p.replace("${TMPDIR}", tmpdir).replace("$TMPDIR", tmpdir)
    return p


def resolve(p, cwd):
    """Normalised absolute path; `dir/*` and `*` are reduced to the folder."""
    p = expand_path(p)
    if p in ("*", "./*"):
        p = "."
    elif p.endswith("/*"):
        p = p[:-2] or "/"
    if not os.path.isabs(p):
        p = os.path.join(cwd or HOME, p)
    return os.path.normpath(p)


def protected_reason(path, rules):
    """None if the recursive rm is tolerated, otherwise the reason for the refusal."""
    path = os.path.normpath(path)
    if os.path.basename(path) == ".git":
        return "target is .git (repository history)"
    for pre in rules.get("tmp_prefixes", []):
        pre = expand_path(pre).rstrip("/")
        if path == pre:
            return "the temporary root folder itself"
        if path.startswith(pre + "/"):
            return None
    if path == "/":
        return "disk root"
    if path in SYSTEM_TOPS:
        return "system folder"
    if path == HOME:
        return "home folder"
    for pre in rules.get("protected_prefixes", []):
        pre = os.path.normpath(expand_path(pre))
        if path == pre or path.startswith(pre + "/"):
            return "under %s (protected prefix)" % pre
    if path.startswith(HOME + "/"):
        depth = len(path[len(HOME) + 1:].split("/"))
        limit = int(rules.get("home_max_depth", 3))
        if depth <= limit:
            return "depth %d under home (everything at <= %d levels is protected, project folders included)" % (depth, limit)
        return None
    if path.startswith("/Volumes/"):
        depth = len(path[len("/Volumes/"):].split("/"))
        limit = int(rules.get("volumes_max_depth", 2))
        if depth <= limit:
            return "depth %d on an external volume (protected up to %d)" % (depth, limit)
        return None
    depth = len(path.strip("/").split("/"))
    if depth <= 2:
        return "shallow system path"
    return None


def current_branch(repo_dir):
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        name = out.stdout.strip()
        return name or None
    except (OSError, subprocess.SubprocessError):
        return None


HEREDOC_RE = re.compile(
    r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S
)


def strip_heredocs(cmd):
    return HEREDOC_RE.sub(lambda m: "<<HEREDOC" + m.group(3), cmd)


def tokenize(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return [t for t in re.split(r"\s+", cmd.strip()) if t]


def split_segments(tokens):
    segs, cur = [], []
    for t in tokens:
        if t in SEPARATORS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def unwrap(tokens):
    """Strips rtk, rtk proxy (optional RTK rewriter), sudo, env VAR=x, time… to reach the real command."""
    changed = True
    while tokens and changed:
        changed = False
        head = os.path.basename(tokens[0])
        if head == "rtk":
            tokens = tokens[1:]
            if tokens and tokens[0] == "proxy":
                tokens = tokens[1:]
            changed = True
            continue
        if head in WRAPPERS:
            tokens = tokens[1:]
            while tokens and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]) or tokens[0].startswith("-")):
                tokens = tokens[1:]
            changed = True
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
            changed = True
    return tokens


def strip_redirections(tokens):
    out, skip = [], False
    for t in tokens:
        if skip:
            skip = False
            continue
        if t in REDIRECTS or re.match(r"^\d*>>?$", t) or re.match(r"^\d*>&\d+$", t):
            skip = t not in ("2>&1",)
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------- checks


def check_rm(args, cwd, rules):
    recursive, end_opts, targets = False, False, []
    for t in strip_redirections(args):
        if not end_opts and t == "--":
            end_opts = True
            continue
        if not end_opts and t.startswith("--"):
            if t == "--recursive":
                recursive = True
            continue
        if not end_opts and t.startswith("-") and len(t) > 1:
            if "r" in t or "R" in t:
                recursive = True
            continue
        targets.append(t)
    if not recursive:
        return None
    for t in targets:
        path = resolve(t, cwd)
        why = protected_reason(path, rules)
        if why:
            return "recursive rm on %s: %s" % (path, why)
    return None


def check_git(args, cwd, rules):
    protected = set(rules.get("protected_branches", []))
    gitrules = rules.get("git", {})
    repo_dir = cwd or HOME
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            repo_dir = resolve(args[i + 1], cwd)
            i += 2
            continue
        if args[i] == "-c" and i + 1 < len(args):
            i += 2
            continue
        i += 1
    if i >= len(args):
        return None
    sub, rest = args[i], strip_redirections(args[i + 1:])
    positional = [t for t in rest if not t.startswith("-")]

    if sub == "push":
        if "--mirror" in rest:
            return "git push --mirror"
        force = any(t in ("-f", "--force", "--force-with-lease", "--force-if-includes")
                    or t.startswith("--force") for t in rest)
        delete = any(t in ("-d", "--delete") for t in rest)
        names = []
        for r in positional[1:]:
            if r.startswith("+"):
                force = True
                r = r[1:]
            if ":" in r:
                src, dst = r.split(":", 1)
                if src == "":
                    delete = True
                r = dst
            names.append(r.split("/")[-1])
        if not (force or delete):
            return None
        kind = "--force" if force else "--delete"
        if names:
            hit = [n for n in names if n in protected]
            return "git push %s to %s" % (kind, hit[0]) if hit else None
        cur = current_branch(repo_dir)
        if cur is None:
            return "git push %s without refspec, current branch unknown" % kind
        if cur in protected:
            return "git push %s to the current branch %s" % (kind, cur)
        return None

    if sub == "branch":
        forced = any(t == "-D" or t == "--force"
                     or (t.startswith("-") and not t.startswith("--") and "D" in t) for t in rest)
        if forced:
            hit = [n for n in positional if n in protected]
            if hit:
                return "git branch -D %s" % hit[0]
        return None

    if sub == "reset" and gitrules.get("deny_reset_hard", True) and "--hard" in rest:
        return "git reset --hard (loses uncommitted work)"

    if sub == "clean" and gitrules.get("deny_clean_force", True):
        if "--force" in rest or any(t.startswith("-") and not t.startswith("--") and "f" in t for t in rest):
            return "git clean -f (deletes untracked files)"
        return None

    if sub in ("checkout", "restore") and gitrules.get("deny_checkout_all", True):
        if sub == "restore" and ("--staged" in rest or "-S" in rest) and "--worktree" not in rest and "-W" not in rest:
            return None
        if any(p in (".", "./", "*", ":/", ":/.") for p in positional):
            return "git %s of the whole tree (loses local changes)" % sub
        return None

    if sub == "stash" and gitrules.get("deny_stash_clear_drop", True) and rest and rest[0] in ("clear", "drop"):
        return "git stash %s" % rest[0]

    return None


def analyze_command(cmd, cwd, rules, nesting=0):
    if nesting > MAX_NESTING or not cmd or not cmd.strip():
        return None
    cmd = strip_heredocs(cmd)
    for seg in split_segments(tokenize(cmd)):
        seg = unwrap(seg)
        if not seg:
            continue
        head = os.path.basename(seg[0])
        if head == "cd":
            cwd = resolve(seg[1], cwd) if len(seg) > 1 else HOME
            continue
        if head in SHELLS and len(seg) >= 3 and re.match(r"^-[a-z]*c[a-z]*$", seg[1]):
            why = analyze_command(seg[2], cwd, rules, nesting + 1)
            if why:
                return why
            continue
        if head == "eval":
            why = analyze_command(" ".join(seg[1:]), cwd, rules, nesting + 1)
            if why:
                return why
            continue
        seg_str = " ".join(seg)
        for rx in rules.get("deny_command_regex", []):
            if re.search(rx, seg_str):
                return "forbidden command (%s)" % seg_str[:120]
        if head == "rm":
            why = check_rm(seg[1:], cwd, rules)
            if why:
                return why
        elif head == "git":
            why = check_git(seg[1:], cwd, rules)
            if why:
                return why
    return None


def check_mcp(tool_name, rules):
    for rx in rules.get("deny_tools", []):
        if re.search(rx, tool_name):
            return "tool %s is in the deny_tools list" % tool_name
    return None


# ---------------------------------------------------------------- main


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        emit_allow()
    if not isinstance(payload, dict):
        emit_allow()
    if os.path.exists(OFF_FILE):
        sys.stderr.write("[guard] ~/.claude/guard.off present: guard disabled\n")
        emit_allow()
    try:
        rules = load_rules()
        tool = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
        cwd = payload.get("cwd") or os.getcwd()
        reason = None
        if tool == "Bash":
            reason = analyze_command(str(tool_input.get("command", "")), cwd, rules)
        elif tool.startswith("mcp__"):
            reason = check_mcp(tool, rules)
        if reason:
            emit_deny(reason, payload)
    except SystemExit:
        raise
    except Exception as exc:  # fail-open, but visible
        sys.stderr.write("[guard] internal error, command let through: %r\n" % (exc,))
    emit_allow()


if __name__ == "__main__":
    main()
