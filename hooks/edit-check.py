#!/usr/bin/env python3
"""
Edit check: PostToolUse hook for Edit|Write (2026-09-22).

Short feedback loop after each file write: within the same turn
(additionalContext), the model gets what a reviewer with tooling would see,
without waiting for a human turn.

Two checks (rules in hooks/edit-check.json, this script applies them):
  1. Per-file lint with the PROJECT's own linter: the config nearest to the
     file (biome.json, eslint.config.*, [tool.ruff] in pyproject.toml,
     ruff.toml) AND the local binary (node_modules/.bin/…, .venv/bin/…, looked
     up walking upwards from the config, which covers npm-workspaces hoisting).
     Config without binary or binary without config = no lint; nothing global
     is assumed on PATH. Lint only: no formatting, no type-check
     (tsc/vue-tsc = whole project). Only exit 1 is a result; exit 0 = clean;
     any other exit, a timeout or a broken binary = the linter could not run:
     silent towards the model, one line on stderr (known case: worktree
     .claude/worktrees/* without node_modules -> eslint exit 2).
  2. CLAUDE.md anti-patterns on the ADDED TEXT (Edit.new_string,
     Write.content; never the re-read file: races with parallel subagents,
     wrong attribution): silent env var fallback
     (process.env.X ?? / ||, import.meta.env, os.environ.get("X", default),
     os.getenv(...) or) and hard-coded local URL (localhost, 127.0.0.1, 0.0.0.0).
     Excluded paths: antipatterns.exclude_path_regex in edit-check.json (by default:
     tests, *.config.* files, ~/.claude/{hooks,agent-guardrails}); comment
     lines and lines mentioning NODE_ENV are ignored. Exact line number for
     Write (content = whole file); for Edit, the offending line.

Output: {} if nothing; otherwise {"hookSpecificOutput": {"hookEventName":
"PostToolUse", "additionalContext": "[edit-check] …"}}. Never decision=block:
the model fixes or justifies. Any internal error = {} + stderr (fail-open).
Also runs for subagent edits (hooks work at harness level).

Escape hatch (controlled by the human):
  touch ~/.claude/edit-check.off   -> no-op, noted on stderr

Tests: tests/edit-check-tests.sh (fake linters, throwaway fixtures, regexes).
To unplug: remove the PostToolUse entry "edit-check.py" from
~/.claude/settings.json; delete edit-check.py and edit-check.json.

Manual test (expect 2 anti-patterns; lint needs an existing file in a project with tooling):
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

# ---------------------------------------------------------------- outputs


def emit_noop():
    sys.stdout.write("{}\n")
    sys.stdout.flush()
    sys.exit(0)


def emit_context(text):
    out = {"hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": text}}
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(0)


# ---------------------------------------------------------------- helpers


def load_rules():
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_path(p, cwd):
    """Normalised absolute path of the edited file (None if missing)."""
    if not p or not isinstance(p, str):
        return None
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.normpath(p)


def extension(path):
    return os.path.splitext(path)[1].lstrip(".").lower()


def parents(start):
    """Folders from start (included) up to the root; stops before $HOME."""
    d = os.path.abspath(start)
    while d != HOME:
        yield d
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def config_matches(dirpath, spec):
    """spec: "name" or {"name": ..., "contains": ...}."""
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
    """(linter, config_dir): config nearest to the file, JSON order breaks ties."""
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
            out.append("… (truncated)")
            break
        out.append(line)
        size += len(line) + 1
    return out


def new_text(tool_name, tool_input):
    """(added text, are line numbers reliable?)."""
    if tool_name == "Write":
        return str(tool_input.get("content") or ""), True
    return str(tool_input.get("new_string") or ""), False


# ---------------------------------------------------------------- checks


def run_lint(path, rules):
    """Diagnostic lines from the project linter; [] if nothing to report or it could not run."""
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
        sys.stderr.write("[edit-check] %s: timeout (%gs), ignored\n" % (ident, timeout_s))
        return []
    except OSError as exc:
        sys.stderr.write("[edit-check] %s: could not start (%r), ignored\n" % (ident, exc))
        return []
    if proc.returncode == 0:
        return []
    # Both streams: biome writes its diagnostics to stderr and the summary to stdout,
    # eslint and ruff the other way round.
    text = "\n".join(s for s in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if s)
    if proc.returncode != FINDINGS_EXIT:
        first = text.splitlines()[0] if text else ""
        sys.stderr.write("[edit-check] %s exit %d: %s\n" % (ident, proc.returncode, first[:200]))
        return []
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    body = truncate(lines, int(rules.get("max_lines", 40)), int(rules.get("max_bytes", 3000)))
    return ["%s:" % ident] + ["  " + l for l in body]


def compile_rules(section, ext):
    active = []
    for rule in section.get("rules", []):
        if ext not in rule.get("extensions", []):
            continue
        try:
            rx = re.compile(rule["regex"])
            unless = re.compile(rule["unless_line_regex"]) if rule.get("unless_line_regex") else None
        except (re.error, KeyError) as exc:
            sys.stderr.write("[edit-check] rule %s ignored: %r\n" % (rule.get("id"), exc))
            continue
        active.append((rule, rx, unless))
    return active


def run_antipatterns(path, text, numbered, rules):
    """Anti-pattern finding lines on the added text; [] if none."""
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
            where = "line %d" % lineno if numbered else "added text"
            findings.append("  %s: %s | `%s`" % (where, rule.get("message", rule.get("id")),
                                                  line.strip()[:LINE_EXCERPT]))
            if len(findings) >= limit:
                findings.append("  … (cap of %d reached)" % limit)
                return ["CLAUDE.md anti-patterns:"] + findings
    return (["CLAUDE.md anti-patterns:"] + findings) if findings else []


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
        sys.stderr.write("[edit-check] ~/.claude/edit-check.off present: checks disabled\n")
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
                                   + ["Informative: fix it, or justify it in your answer."]))
    except SystemExit:
        raise
    except Exception as exc:  # fail-open, but visible
        sys.stderr.write("[edit-check] internal error, checks skipped: %r\n" % (exc,))
    emit_noop()


if __name__ == "__main__":
    main()
