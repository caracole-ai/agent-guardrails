#!/usr/bin/env python3
"""
Règles harnais — hook SubagentStart (2026-09-22).

Injecte un digest des règles du CLAUDE.md global dans le contexte de chaque
sous-agent au moment de son lancement. Vérifié le 2026-09-21 : un sous-agent
(outil Agent : Explore, Plan, general-purpose, agents de plugins…) ne reçoit
ni ~/.claude/CLAUDE.md ni les directives SessionStart — seulement son system
prompt d'agent. Sans ce hook, le passage des règles dépend de la mémoire du
processus parent.

Choix du digest (règles dans hooks/agent-rules.json, ce script les applique) :
  - agent_type dans readonly_types -> agent-rules/readonly.md
      (efficience de recherche : vérifier à la source, conclusion pas dump,
      s'arrêter dès la réponse, ne rien modifier)
  - agent_type dans skip_types     -> rien (fork : hérite déjà tout le contexte ;
      statusline-setup : tâche cadrée)
  - tout le reste, y compris absent, vide ou inconnu -> agent-rules/mutating.md
      (règles de code du CLAUDE.md + anti-pattern env + bloc DoD attendu)
  agent_type = subagent_type de l'appel Agent (payload SubagentStart).

Sortie : {"hookSpecificOutput": {"hookEventName": "SubagentStart",
          "additionalContext": <digest>}} — le harnais l'affiche au sous-agent.
Ne bloque jamais. Digest manquant ou vide, JSON de règles cassé, erreur
interne : no-op {} et une ligne sur stderr (fail-open, comme guard.py).

Escape hatch (contrôlé par l'humain) :
  touch ~/.claude/agent-rules.off   -> no-op, noté sur stderr

Tests : tests/agent-rules-tests.sh (payloads synthétiques, digest attendu).
Pour débrancher : retirer l'entrée SubagentStart « agent-rules.py » dans
~/.claude/settings.json ; supprimer agent-rules.py, agent-rules.json, agent-rules/.

Test manuel (attendre le digest readonly) :
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


def pick_digest(agent_type, rules):
    """Nom du digest pour un agent_type ; None si l'agent est à ignorer."""
    if agent_type in rules.get("skip_types", []):
        return None
    if agent_type in rules.get("readonly_types", []):
        return "readonly"
    return "mutating"


def read_digest(name, rules):
    rel = rules.get("digests", {}).get(name)
    if not rel:
        raise ValueError("digest %r non déclaré dans agent-rules.json" % name)
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
        sys.stderr.write("[agent-rules] ~/.claude/agent-rules.off présent : injection désactivée\n")
        emit_noop()
    try:
        rules = load_rules()
        agent_type = payload.get("agent_type") or ""
        name = pick_digest(agent_type, rules)
        if name is None:
            emit_noop()
        text = read_digest(name, rules)
        if not text:
            raise ValueError("digest %r vide" % name)
        emit_context(text)
    except SystemExit:
        raise
    except Exception as exc:  # fail-open, mais visible
        sys.stderr.write("[agent-rules] erreur interne, sous-agent lancé sans règles : %r\n" % (exc,))
    emit_noop()


if __name__ == "__main__":
    main()
