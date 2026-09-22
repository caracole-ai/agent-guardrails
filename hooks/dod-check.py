#!/usr/bin/env python3
"""
Definition of Done: Stop hook, level 2 (v2, 2026-09-18).

Blocks the end of the turn if the current turn changed something while the
last assistant answer lacks the **Verified** and **Not verified** markers
(Definition of Done, see ~/.claude/CLAUDE.md and examples/CLAUDE.snippet.md).

Mutation detection, (A) or (B):
  (A) git fingerprint of the project taken at the prompt by dod-snapshot.py
      (UserPromptSubmit hook) and compared again here: covers any edit in the
      working repository, whatever the path (Bash, MCP, subagents).
  (B) scan of the current turn's tool_use entries in the transcript:
      - Edit / Write / NotebookEdit;
      - Bash: sed -i, perl -i, > >> redirections outside tmp/scratchpad, tee,
        cp/mv/rm/mkdir/touch/chmod/chown/ln outside tmp, git commit/merge/rebase/
        reset/push/tag/stash/cherry-pick/am/apply/rm/mv/checkout -b/switch -c,
        npm/pnpm/yarn/bun install|add|remove|uninstall, pip install, claude
        mcp|plugin add|remove|disable|enable, inline python/node code that writes
        a file (open(…,'w'), writeFile, fs.write…);
      - writing MCP tools (name containing create/update/delete/push/set/…;
        exceptions, as examples: the jcodemunch indexers index_folder/
        index_repo and the browser servers claude-in-chrome / chrome-devtools);
      - Artifact: no action (publish) or publish/write_db/upload_asset/
        delete_asset/copy_from/delete/reply/resolve;
      - Agent: any subagent_type that is not read-only (Explore, Plan,
        claude-code-guide, feature-dev:code-*, statusline-setup).

Emergency bypass: include __bypass_dod__ in the answer.
Loop guard: honours stop_hook_active (lets Claude finish if it is already in a forced re-prompt).

To unplug: remove both hook entries (UserPromptSubmit and Stop)
from ~/.claude/settings.json, then delete dod-snapshot.py and dod-check.py.
"""
import importlib.util
import json
import os
import re
import shlex
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BYPASS_KEYWORD = "__bypass_dod__"
# DoD markers, matched as literal prefixes: `**Verified` covers `**Verified**`,
# `**Verified and tested**`…; `**Not verified` covers `**Not verified, risks**`
# (canonical form, see examples/CLAUDE.snippet.md). Neither contains the other.
VERIFIED_MARKER = "**Verified"
NOT_VERIFIED_MARKER = "**Not verified"

FILE_TOOLS = {"Edit", "Write", "NotebookEdit"}
READ_ONLY_AGENTS = {
    "Explore", "Plan", "claude-code-guide", "feature-dev:code-explorer",
    "feature-dev:code-architect", "feature-dev:code-reviewer", "statusline-setup",
}
ARTIFACT_MUTATIONS = {
    "publish", "write_db", "upload_asset", "delete_asset", "copy_from", "delete", "reply", "resolve",
}
# Example: MCP tools that only write to their own cache (jcodemunch indexer).
MCP_EXEMPT_TOOLS = {"mcp__jcodemunch__index_folder", "mcp__jcodemunch__index_repo"}
MCP_EXEMPT_SERVERS = {"claude-in-chrome", "chrome-devtools"}  # browsing, not writes
MCP_MUTATION_RE = re.compile(
    r"(create|update|delete|remove|write|push|set|merge|deploy|recreate|restore|reset"
    r"|purchase|upload|publish|add|batch|str_replace|rename|move|clone|install|uninstall"
    r"|disable|enable|fork|import|attach|activate|deactivate|start|stop|restart|resize"
    r"|sync|execute|instantiate)",
    re.IGNORECASE,
)
MCP_READ_PREFIX_RE = re.compile(
    r"^(get|list|search|read|describe|check|query|find|show|take|is_|has_|resolve|validate|guide)",
    re.IGNORECASE,
)

SHELL_OPERATORS = {"&&", "||", ";", ";;", "|", "|&", "&", "(", ")"}
SHELL_PREFIX_SKIP = {
    "sudo", "command", "nohup", "time", "env", "exec", "builtin", "nice", "caffeinate", "export",
    "do", "then", "else", "elif", "if", "while", "until", "!", "{", "}",
}
TMP_PREFIXES = (
    "/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/",
    "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/fd/",
)
GIT_MUTATING_SUBCOMMANDS = {"commit", "merge", "rebase", "reset", "push", "cherry-pick", "am", "apply", "rm", "mv"}
GIT_GLOBAL_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
PKG_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
PKG_MUTATING = {"install", "i", "add", "remove", "uninstall", "un", "rm"}
REDIRECT_TOKEN_RE = re.compile(r"^(\d*)(>>|>\|?|&>>?)(.*)$")
INLINE_WRITE_RE = re.compile(
    r"open\s*\((?:[^()]|\([^()]*\))*?['\"][rwabxt+]{0,3}[wa][rwabxt+]{0,3}['\"]"
    r"|\.write_text\s*\(|\.write_bytes\s*\("
    r"|\bwriteFile(?:Sync)?\s*\(|\bappendFile(?:Sync)?\s*\(|\bfs\.write"
)
ASSIGN_RE = re.compile(r"(?:^|[;&|(\n]\s*)(?:export\s+)?([A-Za-z_]\w*)=([^\s;&|]+)")


# --------------------------------------------------------------------------- transcript

def read_transcript(path):
    """Reads the JSONL, skipping malformed lines."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, PermissionError, OSError):
        return []
    return out


def get_role(entry):
    if entry.get("role"):
        return entry["role"]
    msg = entry.get("message")
    if isinstance(msg, dict):
        return msg.get("role", "")
    return ""


def get_content(entry):
    if "content" in entry:
        return entry["content"]
    msg = entry.get("message")
    if isinstance(msg, dict):
        return msg.get("content", "")
    return ""


def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def find_tool_uses_in_content(content):
    out = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out.append(block)
    return out


def is_user_entry(entry):
    """A real user prompt (not a tool_result).

    Claude Code uses `type=user` for two things: human prompts
    (content=string or text block) AND tool_results sent back to the model
    (content=[{type:tool_result,...}]). Mixing them up makes the hook treat
    the tool_result as the turn boundary and miss mutations that happened
    *before* the tool_result but after the real human prompt.
    """
    is_user_typed = (
        entry.get("type") == "user"
        or (entry.get("type") == "message" and get_role(entry) == "user")
    )
    if not is_user_typed:
        return False
    content = get_content(entry)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
    return True


def is_assistant_entry(entry):
    if entry.get("type") == "assistant":
        return True
    if entry.get("type") == "message" and get_role(entry) == "assistant":
        return True
    return False


# --------------------------------------------------------------------------- (B) Bash

def _expand_vars(token, assigns):
    def repl(match):
        name = match.group(1) or match.group(2)
        if name in assigns:
            return assigns[name]
        if name == "TMPDIR":
            return os.environ.get("TMPDIR", "/tmp/")
        if name == "HOME":
            return os.path.expanduser("~")
        return match.group(0)
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", repl, token)


def is_tmp_path(token, assigns):
    path = os.path.expanduser(_expand_vars(token.strip("\"'"), assigns))
    if path in ("/tmp", "/private/tmp") or "/scratchpad" in path:
        return True
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        tmpdir = tmpdir.rstrip("/")
        if path == tmpdir or path.startswith(tmpdir + "/"):
            return True
    return path.startswith(TMP_PREFIXES)


def _segments(command):
    """Splits into segments (token lists) on && || ; | ( ); tolerant."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes (heredoc with apostrophes…)
        parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command)
        return [part.split() for part in parts if part.strip()]
    segments, current = [], []
    for token in tokens:
        if token in SHELL_OPERATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_prefixes(tokens):
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "rtk":  # RTK rewriter (optional): rtk <cmd>, rtk proxy <cmd>
            i += 1
            if i < len(tokens) and tokens[i] == "proxy":
                i += 1
            continue
        if token in SHELL_PREFIX_SKIP or re.match(r"^[A-Za-z_]\w*=", token):
            i += 1
            continue
        break
    return tokens[i:]


def _redirect_target(tokens, index):
    """Target of a write redirection at tokens[index], otherwise None."""
    token = tokens[index]
    if ">&" in token or token.startswith("<"):  # 2>&1, >&2, <, <<, <<<
        return None
    match = REDIRECT_TOKEN_RE.match(token)
    if not match:
        return None
    target = match.group(3)
    if target.startswith("&"):
        return None
    if target:
        return target
    if index + 1 < len(tokens):
        return tokens[index + 1]
    return None


def _non_flag(args):
    return [a for a in args if not a.startswith("-")]


def _git_reason(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in GIT_GLOBAL_OPTS_WITH_ARG else 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    if sub in GIT_MUTATING_SUBCOMMANDS:
        return "git " + sub
    if sub == "tag":
        if not rest or "-l" in rest or "--list" in rest:
            return None
        return "git tag"
    if sub == "stash":
        if rest and rest[0] in ("list", "show"):
            return None
        return "git stash"
    if sub == "checkout" and ("-b" in rest or "-B" in rest):
        return "git checkout -b"
    if sub == "switch" and ("-c" in rest or "-C" in rest):
        return "git switch -c"
    return None


def _segment_reason(tokens, assigns):
    tokens = _strip_prefixes(tokens)
    if not tokens:
        return None
    for index in range(len(tokens)):
        target = _redirect_target(tokens, index)
        if target is not None and not is_tmp_path(target, assigns):
            return "redirection to " + target
    prog = os.path.basename(tokens[0])
    args = tokens[1:]
    if prog == "sed" and any(re.match(r"^-[a-zA-Z]*i|^--in-place", a) for a in args):
        return "sed -i"
    if prog == "perl" and any(re.match(r"^-[a-zA-Z]*i", a) for a in args):
        return "perl -i"
    if prog == "tee":
        targets = [a for a in _non_flag(args) if not is_tmp_path(a, assigns)]
        if targets:
            return "tee " + targets[0]
    if prog in ("cp", "ln"):
        paths = _non_flag(args)
        if paths and not is_tmp_path(paths[-1], assigns):
            return prog + " to " + paths[-1]
    if prog in ("mv", "rm", "rmdir", "mkdir", "touch"):
        targets = [a for a in _non_flag(args) if not is_tmp_path(a, assigns)]
        if targets:
            return prog + " " + targets[0]
    if prog in ("chmod", "chown", "chgrp"):
        targets = [a for a in _non_flag(args)[1:] if not is_tmp_path(a, assigns)]
        if targets:
            return prog + " " + targets[0]
    if prog == "git":
        return _git_reason(args)
    if prog in PKG_MANAGERS and args and args[0] in PKG_MUTATING:
        return prog + " " + args[0]
    if prog in ("pip", "pip3") and args and args[0] in ("install", "uninstall"):
        return prog + " " + args[0]
    if prog in ("python", "python3") and args[:2] == ["-m", "pip"] and len(args) > 2 \
            and args[2] in ("install", "uninstall"):
        return "pip " + args[2]
    if prog == "claude" and args and args[0] in ("mcp", "plugin"):
        for a in args[1:4]:
            if a in ("add", "remove", "rm", "disable", "enable"):
                return "claude " + args[0] + " " + a
    return None


def bash_mutation_reason(command):
    if not isinstance(command, str) or not command.strip():
        return None
    if INLINE_WRITE_RE.search(command):
        return "Bash: inline code that writes a file"
    assigns = dict((k, v.strip("\"'")) for k, v in ASSIGN_RE.findall(command))
    normalized = re.sub(r"\\\n\s*", " ", command).replace("\n", " ; ")
    for tokens in _segments(normalized):
        reason = _segment_reason(tokens, assigns)
        if reason:
            return "Bash `" + " ".join(tokens)[:80] + "` (" + reason + ")"
    return None


# --------------------------------------------------------------------------- (B) other tools

def mcp_mutation_reason(name):
    if name in MCP_EXEMPT_TOOLS:
        return None
    parts = name.split("__", 2)
    if len(parts) < 3:
        return None
    server, tool = parts[1], parts[2]
    if server in MCP_EXEMPT_SERVERS:
        return None
    stripped = re.sub(r"^[A-Za-z0-9]+_", "", tool, count=1)  # VPS_getX → getX, figma_get_x → get_x
    if MCP_READ_PREFIX_RE.match(tool) or MCP_READ_PREFIX_RE.match(stripped):
        return None
    if MCP_MUTATION_RE.search(tool):
        return "MCP " + name
    return None


def artifact_mutation_reason(inp):
    action = inp.get("action") if isinstance(inp, dict) else None
    if action is None or action in ARTIFACT_MUTATIONS:
        return "Artifact " + (action or "publish")
    return None


def agent_mutation_reason(inp):
    sub = inp.get("subagent_type") if isinstance(inp, dict) else None
    if sub in READ_ONLY_AGENTS:
        return None
    return "Agent " + (sub or "general-purpose")


def classify_tool_use(name, inp):
    """Mutation reason for a tool_use, or None."""
    if not isinstance(inp, dict):
        inp = {}
    if name in FILE_TOOLS:
        return name + " " + str(inp.get("file_path") or inp.get("notebook_path") or "")
    if name == "Bash":
        return bash_mutation_reason(inp.get("command", ""))
    if name.startswith("mcp__"):
        return mcp_mutation_reason(name)
    if name == "Artifact":
        return artifact_mutation_reason(inp)
    if name == "Agent":
        return agent_mutation_reason(inp)
    return None


def transcript_mutation_reason(transcript, last_user_idx):
    for i in range(last_user_idx + 1, len(transcript)):
        entry = transcript[i]
        candidates = []
        if entry.get("type") == "tool_use":
            candidates.append((
                entry.get("tool_name") or entry.get("name") or "",
                entry.get("input") or entry.get("tool_input") or {},
            ))
        for block in find_tool_uses_in_content(get_content(entry)):
            candidates.append((block.get("name") or block.get("tool_name") or "", block.get("input") or {}))
        for name, inp in candidates:
            reason = classify_tool_use(name, inp)
            if reason:
                return reason
    return None


# --------------------------------------------------------------------------- (A) git fingerprint

def _load_snapshot_module():
    path = os.path.join(HERE, "dod-snapshot.py")
    try:
        spec = importlib.util.spec_from_file_location("dod_snapshot", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def fingerprint_mutation_reason(session_id, cwd):
    snap = _load_snapshot_module()
    if snap is None:
        return None
    sid = snap.safe_session_id(session_id)
    if not sid:
        return None
    try:
        with open(snap.snapshot_path(sid), "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(snapshot, dict) or not snapshot.get("fingerprint"):
        return None
    toplevel, current = snap.compute_fingerprint(cwd or snapshot.get("cwd"))
    if current is None or toplevel != snapshot.get("toplevel"):
        return None
    if current != snapshot["fingerprint"]:
        return "git fingerprint changed (" + toplevel + ")"
    return None


# --------------------------------------------------------------------------- main

def main():
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, OSError):
        sys.exit(0)

    # Loop guard: Claude Code passes stop_hook_active=True when it is already
    # in a forced re-prompt. Let it finish.
    if data.get("stop_hook_active", False):
        sys.exit(0)

    transcript_path = data.get("transcript_path")
    if not transcript_path:
        sys.exit(0)

    time.sleep(0.1)  # let the transcript flush
    transcript = read_transcript(transcript_path)
    if not transcript:
        sys.exit(0)

    last_user_idx = -1
    for i in range(len(transcript) - 1, -1, -1):
        if is_user_entry(transcript[i]):
            last_user_idx = i
            break

    reason = transcript_mutation_reason(transcript, last_user_idx)
    if reason is None:
        reason = fingerprint_mutation_reason(data.get("session_id"), data.get("cwd"))
    if reason is None:
        sys.exit(0)  # turn without mutation, no DoD required

    # Last assistant text of the CURRENT TURN only (after last_user_idx).
    last_text = ""
    for i in range(len(transcript) - 1, last_user_idx, -1):
        if is_assistant_entry(transcript[i]):
            txt = extract_text(get_content(transcript[i]))
            if txt:
                last_text = txt
                break

    if BYPASS_KEYWORD in last_text:
        sys.exit(0)

    has_verified = VERIFIED_MARKER in last_text
    has_not_verified = NOT_VERIFIED_MARKER in last_text
    if has_verified and has_not_verified:
        sys.exit(0)

    missing = []
    if not has_verified:
        missing.append(VERIFIED_MARKER + "** (surfaces named concretely: paths, files, commands)")
    if not has_not_verified:
        missing.append(NOT_VERIFIED_MARKER + ", risks** (what was not tested; if the list is empty, justify in one sentence)")

    output = {
        "decision": "block",
        "reason": (
            "Definition of Done incomplete. This turn changed something "
            "(trigger: " + reason + "). Detection covers Edit/Write/NotebookEdit, "
            "Bash (sed -i, redirections, cp/mv/rm, git commit/push, npm/pip install…), "
            "writing MCP tools, Artifact, subagents and the project's git fingerprint "
            "taken at the prompt. The end of the task must include the DoD block before concluding.\n\n"
            "Missing: " + " + ".join(missing) + ".\n\n"
            "Expected format (see ~/.claude/CLAUDE.md, section 'Definition of Done — required at the end of every task'):\n\n"
            + VERIFIED_MARKER + "**: <concrete surfaces: paths, files, commands, tests that passed>\n"
            + NOT_VERIFIED_MARKER + ", risks**: <what could break and was not tested; "
            "if nothing is at risk, justify in one sentence>\n\n"
            "Emergency bypass (only if the hook is buggy or the rule does not apply): "
            "include the marker " + BYPASS_KEYWORD + " in your answer."
        ),
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()
