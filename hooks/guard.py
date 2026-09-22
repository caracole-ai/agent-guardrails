#!/usr/bin/env python3
"""
Garde-fou PreToolUse — refuse les actions irréversibles, même en bypass.

Pourquoi un hook : les sessions tournent en --dangerously-skip-permissions,
sans aucune invite. Les règles permissions.deny de settings.json restent
appliquées dans ce mode (doc permission-modes : « Deny rules block in every
mode, including bypassPermissions », relu le 2026-09-22), mais ce sont des
motifs statiques. Ce hook décide avec le contexte (branche courante, cwd après
cd, profondeur sous le home, bash -c / eval) et renvoie permissionDecision=deny,
qui empêche l'appel (doc hooks, PreToolUse decision control). Un autre hook
PreToolUse qui réécrit les commandes (ex. RTK) tourne en parallèle et voit la
commande d'origine, comme celui-ci.

Ce qu'il refuse (règles dans hooks/guard-rules.json, ce script les applique) :
  - Bash : rm récursif sur une surface protégée (racine, home, dossier de
    profondeur <= 3 sous le home = tout projet, volume externe de profondeur
    <= 2, préfixes protégés comme ~/.ssh ou ~/.claude/projects, .git) ;
    git push --force / --delete / +ref vers une branche protégée, git push
    --mirror, git branch -D d'une branche protégée, git reset --hard,
    git clean -f, git checkout/restore de tout l'arbre, git stash clear/drop ;
    mkfs, dd vers /dev, diskutil erase, shutdown/reboot, npm publish,
    gh repo delete, gh pr merge, docker volume rm/prune.
  - Outils MCP : noms matchant deny_tools (exemples fournis : Hostinger destructif,
    GitHub delete_repository / merge_pull_request / delete_file).
  Les commandes composées (&&, ||, ;, |), les wrappers (sudo, env, et le réécriveur
  RTK s'il est installé : rtk, rtk proxy),
  bash -c / sh -c / eval et les cd intermédiaires sont suivis. Les corps de
  heredoc sont ignorés (du texte, pas des commandes).

Limites assumées : un `python3 -c "shutil.rmtree(...)"`, un `find -delete`
ou un `xargs rm` ne sont pas analysés. Le but est de stopper les gestes
autonomes catastrophiques courants, pas de résister à un contournement.

Escape hatch (contrôlé par l'humain, pas par le modèle) :
  touch ~/.claude/guard.off   -> le hook laisse tout passer et le note sur stderr
Sinon l'humain exécute la commande lui-même avec `! <cmd>` dans le prompt.

En cas d'erreur interne le hook laisse passer (fail-open) et écrit sur stderr :
un garde qui casse toutes les commandes en bypass serait pire que pas de garde.

Journal des refus : ~/.claude/guard.log (une ligne JSON par refus).
Tests : tests/guard-tests.sh (payloads synthétiques, deny/allow attendus).
Débrancher : retirer l'entrée PreToolUse
« guard.py » dans ~/.claude/settings.json.
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

# ---------------------------------------------------------------- sorties


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
        "Refusé par le garde-fou (~/.claude/hooks/guard.py) : " + reason + ". "
        "Action irréversible ou surface protégée : elle n'est pas exécutée, même en bypass. "
        "Ne contourne pas (ni variante de la commande, ni autre outil) : explique à l'humain "
        "ce que tu voulais faire et laisse-le l'exécuter lui-même (`! <commande>`), "
        "ou lui demander de créer ~/.claude/guard.off le temps de l'opération."
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


# ---------------------------------------------------------------- utilitaires


def load_rules():
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def expand_path(p):
    """Expand ~, $HOME, $TMPDIR ; ne touche pas aux autres variables."""
    p = p.strip().strip("'\"")
    if p == "~" or p.startswith("~/"):
        p = HOME + p[1:]
    p = p.replace("${HOME}", HOME).replace("$HOME", HOME)
    tmpdir = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    p = p.replace("${TMPDIR}", tmpdir).replace("$TMPDIR", tmpdir)
    return p


def resolve(p, cwd):
    """Chemin absolu normalisé ; `dir/*` et `*` sont ramenés au dossier."""
    p = expand_path(p)
    if p in ("*", "./*"):
        p = "."
    elif p.endswith("/*"):
        p = p[:-2] or "/"
    if not os.path.isabs(p):
        p = os.path.join(cwd or HOME, p)
    return os.path.normpath(p)


def protected_reason(path, rules):
    """None si le rm récursif est toléré, sinon la raison du refus."""
    path = os.path.normpath(path)
    if os.path.basename(path) == ".git":
        return "cible .git (historique du dépôt)"
    for pre in rules.get("tmp_prefixes", []):
        pre = expand_path(pre).rstrip("/")
        if path == pre:
            return "dossier temporaire racine lui-même"
        if path.startswith(pre + "/"):
            return None
    if path == "/":
        return "racine du disque"
    if path in SYSTEM_TOPS:
        return "dossier système"
    if path == HOME:
        return "dossier home"
    for pre in rules.get("protected_prefixes", []):
        pre = os.path.normpath(expand_path(pre))
        if path == pre or path.startswith(pre + "/"):
            return "sous %s (préfixe protégé)" % pre
    if path.startswith(HOME + "/"):
        depth = len(path[len(HOME) + 1:].split("/"))
        limit = int(rules.get("home_max_depth", 3))
        if depth <= limit:
            return "profondeur %d sous le home (tout ce qui est à <= %d niveaux est protégé : dossiers de projets inclus)" % (depth, limit)
        return None
    if path.startswith("/Volumes/"):
        depth = len(path[len("/Volumes/"):].split("/"))
        limit = int(rules.get("volumes_max_depth", 2))
        if depth <= limit:
            return "profondeur %d sur un volume externe (max protégé %d)" % (depth, limit)
        return None
    depth = len(path.strip("/").split("/"))
    if depth <= 2:
        return "chemin système peu profond"
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
    """Retire rtk, rtk proxy (réécriveur RTK, optionnel), sudo, env VAR=x, time… pour atteindre la vraie commande."""
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


# ---------------------------------------------------------------- vérifications


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
            return "rm récursif sur %s : %s" % (path, why)
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
            return "git push %s sur %s" % (kind, hit[0]) if hit else None
        cur = current_branch(repo_dir)
        if cur is None:
            return "git push %s sans refspec, branche courante inconnue" % kind
        if cur in protected:
            return "git push %s sur la branche courante %s" % (kind, cur)
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
        return "git reset --hard (perte du travail non commité)"

    if sub == "clean" and gitrules.get("deny_clean_force", True):
        if "--force" in rest or any(t.startswith("-") and not t.startswith("--") and "f" in t for t in rest):
            return "git clean -f (suppression des fichiers non suivis)"
        return None

    if sub in ("checkout", "restore") and gitrules.get("deny_checkout_all", True):
        if sub == "restore" and ("--staged" in rest or "-S" in rest) and "--worktree" not in rest and "-W" not in rest:
            return None
        if any(p in (".", "./", "*", ":/", ":/.") for p in positional):
            return "git %s de tout l'arbre (perte des modifications locales)" % sub
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
                return "commande interdite (%s)" % seg_str[:120]
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
            return "outil %s dans la liste deny_tools" % tool_name
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
        sys.stderr.write("[guard] ~/.claude/guard.off présent : garde-fou désactivé\n")
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
    except Exception as exc:  # fail-open, mais visible
        sys.stderr.write("[guard] erreur interne, commande laissée passer : %r\n" % (exc,))
    emit_allow()


if __name__ == "__main__":
    main()
