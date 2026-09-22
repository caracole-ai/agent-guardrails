#!/usr/bin/env bash
# Tests du hook SubagentStart hooks/agent-rules.py sur des payloads synthétiques.
# Usage : tests/agent-rules-tests.sh   (sortie : une ligne PASS/FAIL par cas + total, exit 1 si échec)
set -u
HOOKS="$(cd "$(dirname "$0")/../hooks" && pwd)"
HOOK="$HOOKS/agent-rules.py"
# agent-rules.py lit son interrupteur dans le dossier parent du sien (~/.claude une fois installé)
OFF_FILE="$(dirname "$HOOKS")/agent-rules.off"
WORK="${TMPDIR:-/tmp}/agent-rules-tests.$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
ok() { pass=$((pass+1)); echo "PASS $1"; }
ko() { fail=$((fail+1)); echo "FAIL $1"; [ -s "$WORK/stderr" ] && sed 's/^/     stderr: /' "$WORK/stderr"; }

# run <script> <agent_type|-> [hook_event_name]   -> stdout dans $OUT, stderr dans $WORK/stderr
run() {
  local script="$1" type="$2" event="${3:-SubagentStart}" payload
  if [ "$type" = "-" ]; then
    payload=$(jq -cn --arg e "$event" '{session_id:"agent-rules-test",cwd:"/tmp",hook_event_name:$e,agent_id:"a1"}')
  else
    payload=$(jq -cn --arg e "$event" --arg t "$type" '{session_id:"agent-rules-test",cwd:"/tmp",hook_event_name:$e,agent_id:"a1",agent_type:$t}')
  fi
  OUT=$(printf '%s' "$payload" | python3 "$script" 2>"$WORK/stderr")
}
# expect_digest <cas> <agent_type|-> <readonly|mutating>
expect_digest() {
  local name="$1" type="$2" kind="$3" event first
  run "$HOOK" "$type"
  event=$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.hookEventName // empty' 2>/dev/null)
  first=$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null | head -1)
  if [ "$event" = "SubagentStart" ] && [ "$first" = "[Règles harnais — $kind]" ] && [ ! -s "$WORK/stderr" ]; then
    ok "$name -> $kind"
  else
    ko "$name -> $kind (got: $(printf '%s' "$OUT" | cut -c1-100))"
  fi
}
# expect_noop <cas> <agent_type|-> [hook_event_name] [regex attendu sur stderr ; vide = stderr vide exigé]
expect_noop() {
  local name="$1" type="$2" event="${3:-SubagentStart}" want_err="${4:-}" good=0
  run "$HOOK" "$type" "$event"
  if [ "$OUT" = "{}" ]; then
    if [ -z "$want_err" ]; then
      [ ! -s "$WORK/stderr" ] && good=1
    else
      grep -q -- "$want_err" "$WORK/stderr" && good=1
    fi
  fi
  if [ "$good" = 1 ]; then ok "$name -> {}"; else ko "$name -> {} (got: $(printf '%s' "$OUT" | cut -c1-100))"; fi
}

echo "== Digests selon agent_type =="
expect_digest explore_readonly Explore readonly
expect_digest plan_readonly Plan readonly
expect_digest claude_code_guide_readonly claude-code-guide readonly
expect_digest feature_dev_reviewer_readonly feature-dev:code-reviewer readonly
expect_digest general_purpose_mutating general-purpose mutating
expect_digest claude_mutating claude mutating
expect_digest type_absent_mutating - mutating
expect_digest type_vide_mutating "" mutating
expect_digest type_inconnu_mutating mon-agent-perso mutating

echo "== Skip, mauvais événement =="
expect_noop fork_skip fork
expect_noop statusline_setup_skip statusline-setup
expect_noop mauvais_event Explore PreToolUse

echo "== Escape hatch, entrées invalides, fichiers cassés =="
touch "$OFF_FILE"
expect_noop off_file Explore SubagentStart 'agent-rules.off'
rm -f "$OFF_FILE"
expect_digest apres_off_file Explore readonly
out=$(printf '' | python3 "$HOOK"); [ "$out" = "{}" ] && ok "stdin vide -> {}" || ko "stdin vide -> $out"
out=$(printf 'not json' | python3 "$HOOK"); [ "$out" = "{}" ] && ok "stdin invalide -> {}" || ko "stdin invalide -> $out"
out=$(printf '[1,2]' | python3 "$HOOK"); [ "$out" = "{}" ] && ok "stdin liste -> {}" || ko "stdin liste -> $out"
# Copie avec JSON de règles cassé
mkdir -p "$WORK/casse" && cp "$HOOK" "$WORK/casse/" && printf '{not json' > "$WORK/casse/agent-rules.json"
run "$WORK/casse/agent-rules.py" Explore
if [ "$OUT" = "{}" ] && grep -q 'erreur interne' "$WORK/stderr"; then ok "json_regles_casse -> {} + stderr"; else ko "json_regles_casse (got: $OUT)"; fi
# Copie sans le dossier des digests
mkdir -p "$WORK/sansdigest" && cp "$HOOK" "$HOOKS/agent-rules.json" "$WORK/sansdigest/"
run "$WORK/sansdigest/agent-rules.py" Explore
if [ "$OUT" = "{}" ] && grep -q 'erreur interne' "$WORK/stderr"; then ok "digest_manquant -> {} + stderr"; else ko "digest_manquant (got: $OUT)"; fi
# Copie avec digest vide
mkdir -p "$WORK/vide/agent-rules" && cp "$HOOK" "$HOOKS/agent-rules.json" "$WORK/vide/" && : > "$WORK/vide/agent-rules/readonly.md"
run "$WORK/vide/agent-rules.py" Explore
if [ "$OUT" = "{}" ] && grep -q 'vide' "$WORK/stderr"; then ok "digest_vide -> {} + stderr"; else ko "digest_vide (got: $OUT)"; fi

echo "== Taille des digests, exécutable, JSON valide =="
for d in readonly mutating; do
  words=$(wc -w < "$HOOKS/agent-rules/$d.md" | tr -d ' ')
  [ "$words" -le 200 ] && ok "digest $d : $words mots (<= 200)" || ko "digest $d : $words mots (> 200)"
  [ "$(head -1 "$HOOKS/agent-rules/$d.md")" = "[Règles harnais — $d]" ] && ok "digest $d : marqueur en première ligne" || ko "digest $d : marqueur absent"
done
[ -x "$HOOK" ] && ok "agent-rules.py exécutable" || ko "agent-rules.py non exécutable"
[ "$(head -1 "$HOOK")" = "#!/usr/bin/env python3" ] && ok "shebang python3" || ko "shebang inattendu"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$HOOKS/agent-rules.json" 2>"$WORK/stderr" && ok "agent-rules.json valide" || ko "agent-rules.json invalide"

echo "== TOTAL : $pass PASS, $fail FAIL =="
[ "$fail" -eq 0 ]
