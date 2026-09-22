#!/usr/bin/env python3
"""
Edit check — hook PostToolUse Edit|Write (2026-09-22).

Boucle de rétroaction courte après chaque écriture de fichier : le modèle
reçoit dans le même tour (additionalContext) ce qu'un relecteur outillé
verrait, sans attendre un tour humain.

Deux vérifications (règles dans hooks/edit-check.json, ce script les applique) :
  1. Lint per-fichier avec le linter DU PROJET : la config la plus proche du
     fichier (biome.json, eslint.config.*, [tool.ruff] dans pyproject.toml,
     ruff.toml) ET le binaire local (node_modules/.bin/…, .venv/bin/…, cherché
     en remontant depuis la config — couvre le hoisting npm-workspaces).
     Config sans binaire ou binaire sans config = pas de lint ; rien de global
     n'est supposé sur le PATH. Lint seulement : ni format ni typecheck
     (tsc/vue-tsc = projet entier). Seul l'exit 1 est un résultat ; exit 0 =
     propre ; autre exit, timeout ou binaire cassé = le linter n'a pas pu
     tourner : silence côté modèle, une ligne sur stderr (cas connu : worktree
     .claude/worktrees/* sans node_modules -> eslint exit 2).
  2. Anti-patterns du CLAUDE.md sur le TEXTE AJOUTÉ (Edit.new_string,
     Write.content — jamais le fichier relu : race avec des sous-agents
     parallèles, fausse attribution) : fallback env var silencieux
     (process.env.X ?? / ||, import.meta.env, os.environ.get("X", défaut),
     os.getenv(...) or) et URL locale en dur (localhost, 127.0.0.1, 0.0.0.0).
     Chemins exclus : antipatterns.exclude_path_regex d'edit-check.json (par défaut :
     tests, fichiers *.config.*, ~/.claude/{hooks,agent-guardrails}) ; lignes
     de commentaire et lignes mentionnant NODE_ENV ignorées. Numéro de ligne
     exact pour Write (content = fichier entier) ; pour Edit, la ligne fautive.

Sortie : {} si rien ; sinon {"hookSpecificOutput": {"hookEventName":
"PostToolUse", "additionalContext": "[edit-check] …"}}. Jamais decision=block :
le modèle corrige ou justifie. Toute erreur interne = {} + stderr (fail-open).
Tourne aussi pour les éditions des sous-agents (hooks au niveau du harnais).

Escape hatch (contrôlé par l'humain) :
  touch ~/.claude/edit-check.off   -> no-op, noté sur stderr

Tests : tests/edit-check-tests.sh (faux linters, fixtures jetables, regex).
Pour débrancher : retirer l'entrée PostToolUse « edit-check.py » dans
~/.claude/settings.json ; supprimer edit-check.py et edit-check.json.

Test manuel (attendre 2 anti-patterns ; le lint exige un fichier existant dans un projet outillé) :
  echo '{"hook_event_name":"PostToolUse","tool_name":"Write","cwd":"/tmp","tool_input":{"file_path":"/tmp/x.ts","content":"export const u = process.env.API_URL ?? \\"http://localhost:3000\\"\\n"}}' | ./edit-check.py
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.path.dirname(HERE)
RULES_PATH = os.path.join(HERE, "edit-check.json")
OFF_FILE = os.path.join(CLAUDE_DIR, "edit-check.off")
HOME = os.path.abspath(os.path.expanduser("~"))
EVENT = "PostToolUse"
TOOLS = {"Edit", "Write"}
FINDINGS_EXIT = 1
COMMENT_LINE = re.compile(r"^\s*(//|#|\*|/\*)")
LINE_EXCERPT = 120

# ---------------------------------------------------------------- sorties


def emit_noop():
    sys.stdout.write("{}\n")
    sys.stdout.flush()
    sys.exit(0)


def emit_context(text):
    out = {"hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": text}}
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(0)


# ---------------------------------------------------------------- utilitaires


def load_rules():
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(p, cwd):
    """Chemin absolu normalisé du fichier édité (None si absent)."""
    if not p or not isinstance(p, str):
        return None
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def extension(path):
    return os.path.splitext(path)[1].lstrip(".").lower()


def parents(start):
    """Dossiers de start (inclus) vers la racine ; s'arrête avant $HOME."""
    d = os.path.abspath(start)
    while d != HOME:
        yield d
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def config_matches(dirpath, spec):
    """spec : "nom" ou {"name": ..., "contains": ...}."""
    if isinstance(spec, str):
        name, contains = spec, None
    else:
        name, contains = spec.get("name"), spec.get("contains")
    if not name:
        return False
    path = os.path.join(dirpath, name)
    if not os.path.isfile(path):
        return False
    if not contains:
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return contains in f.read()
    except OSError:
        return False


def find_linter(path, linters):
    """(linter, config_dir) : config la plus proche du fichier, ordre du JSON à égalité."""
    ext = extension(path)
    candidates = [l for l in linters if ext in l.get("extensions", [])]
    if not candidates:
        return None, None
    for d in parents(os.path.dirname(path)):
        for linter in candidates:
            if any(config_matches(d, spec) for spec in linter.get("config_files", [])):
                return linter, d
    return None, None


def find_binary(config_dir, bin_path):
    if not bin_path:
        return None
    for d in parents(config_dir):
        candidate = os.path.join(d, bin_path)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def truncate(lines, max_lines, max_bytes):
    out, size = [], 0
    for line in lines:
        if len(out) >= max_lines or size + len(line) > max_bytes:
            out.append("… (tronqué)")
            break
        out.append(line)
        size += len(line) + 1
    return out


def new_text(tool_name, tool_input):
    """(texte ajouté, numéros de ligne fiables ?)."""
    if tool_name == "Write":
        return str(tool_input.get("content") or ""), True
    return str(tool_input.get("new_string") or ""), False


# ---------------------------------------------------------------- vérifications


def run_lint(path, rules):
    """Lignes de diagnostic du linter du projet ; [] si rien à dire ou impossible."""
    if not os.path.isfile(path):
        return []
    linter, config_dir = find_linter(path, rules.get("linters", []))
    if linter is None:
        return []
    binary = find_binary(config_dir, linter.get("bin_path", ""))
    if binary is None:
        return []
    ident = linter.get("id", os.path.basename(binary))
    timeout_s = float(rules.get("timeout_s", 8))
    cmd = [binary] + list(linter.get("args", [])) + [path]
    try:
        proc = subprocess.run(cmd, cwd=config_dir, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        sys.stderr.write("[edit-check] %s : timeout (%gs), ignoré\n" % (ident, timeout_s))
        return []
    except OSError as exc:
        sys.stderr.write("[edit-check] %s : lancement impossible (%r), ignoré\n" % (ident, exc))
        return []
    if proc.returncode == 0:
        return []
    # Les deux flux : biome met ses diagnostics sur stderr et le résumé sur stdout,
    # eslint et ruff l'inverse.
    text = "\n".join(s for s in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if s)
    if proc.returncode != FINDINGS_EXIT:
        first = text.splitlines()[0] if text else ""
        sys.stderr.write("[edit-check] %s exit %d : %s\n" % (ident, proc.returncode, first[:200]))
        return []
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    body = truncate(lines, int(rules.get("max_lines", 40)), int(rules.get("max_bytes", 3000)))
    return ["%s :" % ident] + ["  " + l for l in body]


def compile_rules(section, ext):
    active = []
    for rule in section.get("rules", []):
        if ext not in rule.get("extensions", []):
            continue
        try:
            rx = re.compile(rule["regex"])
            unless = re.compile(rule["unless_line_regex"]) if rule.get("unless_line_regex") else None
        except (re.error, KeyError) as exc:
            sys.stderr.write("[edit-check] règle %s ignorée : %r\n" % (rule.get("id"), exc))
            continue
        active.append((rule, rx, unless))
    return active


def run_antipatterns(path, text, numbered, rules):
    """Lignes de findings anti-patterns sur le texte ajouté ; [] si rien."""
    section = rules.get("antipatterns", {})
    if not text or any(re.search(rx, path) for rx in section.get("exclude_path_regex", [])):
        return []
    active = compile_rules(section, extension(path))
    if not active:
        return []
    limit = int(rules.get("max_findings", 10))
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule, rx, unless in active:
            if not rx.search(line):
                continue
            if unless is not None and unless.search(line):
                continue
            if rule.get("skip_comment_lines") and COMMENT_LINE.match(line):
                continue
            where = "ligne %d" % lineno if numbered else "texte ajouté"
            findings.append("  %s : %s | `%s`" % (where, rule.get("message", rule.get("id")),
                                                  line.strip()[:LINE_EXCERPT]))
            if len(findings) >= limit:
                findings.append("  … (plafond %d atteint)" % limit)
                return ["anti-patterns CLAUDE.md :"] + findings
    return (["anti-patterns CLAUDE.md :"] + findings) if findings else []


# ---------------------------------------------------------------- main


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        emit_noop()
    if not isinstance(payload, dict):
        emit_noop()
    tool = payload.get("tool_name") or ""
    if payload.get("hook_event_name") != EVENT or tool not in TOOLS:
        emit_noop()
    if os.path.exists(OFF_FILE):
        sys.stderr.write("[edit-check] ~/.claude/edit-check.off présent : vérifications désactivées\n")
        emit_noop()
    try:
        rules = load_rules()
        tool_input = payload.get("tool_input") or {}
        cwd = payload.get("cwd") or os.getcwd()
        path = resolve_path(tool_input.get("file_path"), cwd)
        if not path:
            emit_noop()
        text, numbered = new_text(tool, tool_input)
        lines = run_lint(path, rules) + run_antipatterns(path, text, numbered, rules)
        if lines:
            emit_context("\n".join(["[edit-check] " + path] + lines
                                   + ["Informatif : corriger, ou justifier dans la réponse."]))
    except SystemExit:
        raise
    except Exception as exc:  # fail-open, mais visible
        sys.stderr.write("[edit-check] erreur interne, vérifications ignorées : %r\n" % (exc,))
    emit_noop()


if __name__ == "__main__":
    main()
