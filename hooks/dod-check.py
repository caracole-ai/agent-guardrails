#!/usr/bin/env python3
"""
Definition of Done — hook Stop, niveau 2 (v2, 2026-09-18).

Bloque la fin de tour si le tour courant a modifié quelque chose alors que la
dernière réponse assistant ne contient pas les marqueurs **Vérifié** et
**Non vérifié** (Definition of Done, voir ~/.claude/CLAUDE.md).

Détection des mutations, (A) ou (B) :
  (A) empreinte git du projet prise au prompt par dod-snapshot.py
      (hook UserPromptSubmit) et recomparée ici : couvre toute édition dans le
      dépôt de travail, quel que soit le chemin (Bash, MCP, sous-agents).
  (B) scan des tool_use du tour courant dans le transcript :
      - Edit / Write / NotebookEdit ;
      - Bash : sed -i, perl -i, redirections > >> hors tmp/scratchpad, tee,
        cp/mv/rm/mkdir/touch/chmod/chown/ln hors tmp, git commit/merge/rebase/
        reset/push/tag/stash/cherry-pick/am/apply/rm/mv/checkout -b/switch -c,
        npm/pnpm/yarn/bun install|add|remove|uninstall, pip install, claude
        mcp|plugin add|remove|disable|enable, code inline python/node qui écrit
        un fichier (open(…,'w'), writeFile, fs.write…) ;
      - outils MCP d'écriture (nom contenant create/update/delete/push/set/… ;
        exceptions, à titre d'exemple : les indexeurs jcodemunch index_folder/
        index_repo et les serveurs navigateur claude-in-chrome / chrome-devtools) ;
      - Artifact : action absente (publish) ou publish/write_db/upload_asset/
        delete_asset/copy_from/delete/reply/resolve ;
      - Agent : tout subagent_type hors lecture seule (Explore, Plan,
        claude-code-guide, feature-dev:code-*, statusline-setup).

Bypass d'urgence : inclure __bypass_dod__ dans la réponse.
Anti-boucle : respecte stop_hook_active (laisse Claude finir s'il est déjà en re-prompt forcé).

Pour débrancher : retirer les deux entrées de hooks (UserPromptSubmit et Stop)
dans ~/.claude/settings.json puis supprimer dod-snapshot.py et dod-check.py.
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

FILE_TOOLS = {"Edit", "Write", "NotebookEdit"}
READ_ONLY_AGENTS = {
    "Explore", "Plan", "claude-code-guide", "feature-dev:code-explorer",
    "feature-dev:code-architect", "feature-dev:code-reviewer", "statusline-setup",
}
ARTIFACT_MUTATIONS = {
    "publish", "write_db", "upload_asset", "delete_asset", "copy_from", "delete", "reply", "resolve",
}
# Exemple : outils MCP qui n'écrivent que dans leur propre cache (indexeur jcodemunch).
MCP_EXEMPT_TOOLS = {"mcp__jcodemunch__index_folder", "mcp__jcodemunch__index_repo"}
MCP_EXEMPT_SERVERS = {"claude-in-chrome", "chrome-devtools"}  # navigation, pas des écritures
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
    """Lit le JSONL en ignorant les lignes mal formées."""
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
    """Vrai prompt utilisateur (pas un tool_result).

    Claude Code utilise `type=user` pour deux choses : les prompts humains
    (content=string ou bloc text) ET les tool_results renvoyés au modèle
    (content=[{type:tool_result,...}]). Si on confond les deux, le hook
    détecte le tool_result comme délimiteur de tour et rate les mutations qui
    ont eu lieu *avant* le tool_result mais après le vrai prompt humain.
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
    """Découpe en segments (listes de tokens) sur && || ; | ( ) — tolérant."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:  # quotes déséquilibrées (heredoc avec apostrophes…)
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
        if token == "rtk":  # réécriveur RTK (optionnel) : rtk <cmd>, rtk proxy <cmd>
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
    """Cible d'une redirection d'écriture en tokens[index], sinon None."""
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
            return "redirection vers " + target
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
            return prog + " vers " + paths[-1]
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
        return "Bash : code inline qui écrit un fichier"
    assigns = dict((k, v.strip("\"'")) for k, v in ASSIGN_RE.findall(command))
    normalized = re.sub(r"\\\n\s*", " ", command).replace("\n", " ; ")
    for tokens in _segments(normalized):
        reason = _segment_reason(tokens, assigns)
        if reason:
            return "Bash `" + " ".join(tokens)[:80] + "` (" + reason + ")"
    return None


# --------------------------------------------------------------------------- (B) autres outils

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
    """Raison de mutation pour un tool_use, ou None."""
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


# --------------------------------------------------------------------------- (A) empreinte git

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
        return "empreinte git modifiée (" + toplevel + ")"
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

    # Anti-boucle : Claude Code passe stop_hook_active=True quand il est déjà
    # en re-prompt forcé. On le laisse finir.
    if data.get("stop_hook_active", False):
        sys.exit(0)

    transcript_path = data.get("transcript_path")
    if not transcript_path:
        sys.exit(0)

    time.sleep(0.1)  # laisser le transcript flusher
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
        sys.exit(0)  # tour sans mutation, DoD non requise

    # Dernier texte assistant DU TOUR COURANT uniquement (après last_user_idx).
    last_text = ""
    for i in range(len(transcript) - 1, last_user_idx, -1):
        if is_assistant_entry(transcript[i]):
            txt = extract_text(get_content(transcript[i]))
            if txt:
                last_text = txt
                break

    if BYPASS_KEYWORD in last_text:
        sys.exit(0)

    # Marqueurs lenients : `**Vérifié` couvre `**Vérifié**`, `**Vérifié et testé**`…
    # `**Non vérifié` couvre `**Non vérifié, risque**` (format canonique CLAUDE.md).
    has_verifie = "**Vérifié" in last_text or "**Verifie" in last_text
    has_non_verifie = "**Non vérifié" in last_text or "**Non verifie" in last_text
    if has_verifie and has_non_verifie:
        sys.exit(0)

    missing = []
    if not has_verifie:
        missing.append("**Vérifié** (surfaces nommées concrètement : chemins, fichiers, commandes)")
    if not has_non_verifie:
        missing.append("**Non vérifié, risque** (ce qui n'a pas été testé ; si liste vide, justifier en 1 phrase)")

    output = {
        "decision": "block",
        "reason": (
            "Definition of Done incomplète. Ce tour a modifié quelque chose "
            "(déclencheur : " + reason + "). La détection couvre Edit/Write/NotebookEdit, "
            "Bash (sed -i, redirections, cp/mv/rm, git commit/push, npm/pip install…), "
            "les outils MCP d'écriture, Artifact, les sous-agents et l'empreinte git du projet "
            "prise au prompt. La fin de tâche doit inclure le bloc DoD avant de conclure.\n\n"
            "Manquant : " + " + ".join(missing) + ".\n\n"
            "Format attendu (cf. ~/.claude/CLAUDE.md, section 'Definition of Done — fin de tâche obligatoire') :\n\n"
            "**Vérifié** : <surfaces concrètes — chemins, fichiers, commandes, tests passés>\n"
            "**Non vérifié, risque** : <ce qui pourrait casser et n'a pas été testé ; "
            "si rien à risque, justifier en 1 phrase>\n\n"
            "Bypass d'urgence (uniquement si le hook bug ou si la règle ne s'applique pas) : "
            "inclure le marqueur __bypass_dod__ dans ta réponse."
        ),
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()
