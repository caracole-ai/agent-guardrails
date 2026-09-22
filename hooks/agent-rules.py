#!/usr/bin/env python3
"""
Harness rules: SubagentStart hook (2026-09-22).

Injects a digest of the global CLAUDE.md rules into the context of each
subagent when it starts. Checked on 2026-09-21: a subagent (Agent tool:
Explore, Plan, general-purpose, plugin agents…) receives neither
~/.claude/CLAUDE.md nor the SessionStart directives, only its own agent
system prompt. Without this hook, passing the rules on depends on the parent
process remembering to.

Digest choice (rules in hooks/agent-rules.json, this script applies them):
  - agent_type in readonly_types -> agent-rules/readonly.md
      (research efficiency: check at the source, a conclusion not a dump,
      stop once answered, change nothing)
  - agent_type in skip_types     -> nothing (fork: already inherits the whole
      context; statusline-setup: narrowly scoped task)
  - everything else, including missing, empty or unknown -> agent-rules/mutating.md
      (CLAUDE.md coding rules + env anti-pattern + expected DoD block)
  agent_type = subagent_type of the Agent call (SubagentStart payload).

Output: {"hookSpecificOutput": {"hookEventName": "SubagentStart",
         "additionalContext": <digest>}}; the harness shows it to the subagent.
Never blocks. Missing or empty digest, broken rules JSON, internal error:
no-op {} and one line on stderr (fail-open, like guard.py).

Escape hatch (controlled by the human):
  touch ~/.claude/agent-rules.off   -> no-op, noted on stderr

Tests: tests/agent-rules-tests.sh (synthetic payloads, expected digest).
To unplug: remove the SubagentStart entry "agent-rules.py" from
~/.claude/settings.json; delete agent-rules.py, agent-rules.json, agent-rules/.

Manual test (expect the readonly digest):
  echo '{"hook_event_name":"SubagentStart","agent_id":"t","agent_type":"Explore"}' | ./agent-rules.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.path.dirname(HERE)
RULES_PATH = os.path.join(HERE, "agent-rules.json")
OFF_FILE = os.path.join(CLAUDE_DIR, "agent-rules.off")
EVENT = "SubagentStart"

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


def pick_digest(agent_type, rules):
    """Digest name for an agent_type; None if the agent is skipped."""
    if agent_type in rules.get("skip_types", []):
        return None
    if agent_type in rules.get("readonly_types", []):
        return "readonly"
    return "mutating"


def read_digest(name, rules):
    rel = rules.get("digests", {}).get(name)
    if not rel:
        raise ValueError("digest %r not declared in agent-rules.json" % name)
    with open(os.path.join(HERE, rel), "r", encoding="utf-8") as f:
        return f.read().strip()


# ---------------------------------------------------------------- main


def main():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        emit_noop()
    if not isinstance(payload, dict):
        emit_noop()
    if payload.get("hook_event_name") != EVENT:
        emit_noop()
    if os.path.exists(OFF_FILE):
        sys.stderr.write("[agent-rules] ~/.claude/agent-rules.off present: injection disabled\n")
        emit_noop()
    try:
        rules = load_rules()
        agent_type = payload.get("agent_type") or ""
        name = pick_digest(agent_type, rules)
        if name is None:
            emit_noop()
        text = read_digest(name, rules)
        if not text:
            raise ValueError("digest %r is empty" % name)
        emit_context(text)
    except SystemExit:
        raise
    except Exception as exc:  # fail-open, but visible
        sys.stderr.write("[agent-rules] internal error, subagent started without rules: %r\n" % (exc,))
    emit_noop()


if __name__ == "__main__":
    main()
