#!/usr/bin/env bash
# Tests of the SubagentStart hook hooks/agent-rules.py on synthetic payloads.
# Usage: tests/agent-rules-tests.sh   (output: one PASS/FAIL line per case + total, exit 1 on failure)
set -u
HOOKS="$(cd "$(dirname "$0")/../hooks" && pwd)"
HOOK="$HOOKS/agent-rules.py"
# agent-rules.py reads its kill switch in the parent of its own folder (~/.claude once installed)
OFF_FILE="$(dirname "$HOOKS")/agent-rules.off"
WORK="${TMPDIR:-/tmp}/agent-rules-tests.$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
ok() { pass=$((pass+1)); echo "PASS $1"; }
ko() { fail=$((fail+1)); echo "FAIL $1"; [ -s "$WORK/stderr" ] && sed 's/^/     stderr: /' "$WORK/stderr"; }

# run <script> <agent_type|-> [hook_event_name]   -> stdout in $OUT, stderr in $WORK/stderr
run() {
  local script="$1" type="$2" event="${3:-SubagentStart}" payload
  if [ "$type" = "-" ]; then
    payload=$(jq -cn --arg e "$event" '{session_id:"agent-rules-test",cwd:"/tmp",hook_event_name:$e,agent_id:"a1"}')
  else
    payload=$(jq -cn --arg e "$event" --arg t "$type" '{session_id:"agent-rules-test",cwd:"/tmp",hook_event_name:$e,agent_id:"a1",agent_type:$t}')
  fi
  OUT=$(printf '%s' "$payload" | python3 "$script" 2>"$WORK/stderr")
}
# expect_digest <case> <agent_type|-> <readonly|mutating>
expect_digest() {
  local name="$1" type="$2" kind="$3" event first
  run "$HOOK" "$type"
  event=$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.hookEventName // empty' 2>/dev/null)
  first=$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null | head -1)
  if [ "$event" = "SubagentStart" ] && [ "$first" = "[Harness rules — $kind]" ] && [ ! -s "$WORK/stderr" ]; then
    ok "$name -> $kind"
  else
    ko "$name -> $kind (got: $(printf '%s' "$OUT" | cut -c1-100))"
  fi
}
# expect_noop <case> <agent_type|-> [hook_event_name] [regex expected on stderr; empty = empty stderr required]
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

echo "== Digest per agent_type =="
expect_digest explore_readonly Explore readonly
expect_digest plan_readonly Plan readonly
expect_digest claude_code_guide_readonly claude-code-guide readonly
expect_digest feature_dev_reviewer_readonly feature-dev:code-reviewer readonly
expect_digest general_purpose_mutating general-purpose mutating
expect_digest claude_mutating claude mutating
expect_digest type_missing_mutating - mutating
expect_digest type_empty_mutating "" mutating
expect_digest type_unknown_mutating my-custom-agent mutating

echo "== Skip, wrong event =="
expect_noop fork_skip fork
expect_noop statusline_setup_skip statusline-setup
expect_noop wrong_event Explore PreToolUse

echo "== Escape hatch, invalid input, broken files =="
touch "$OFF_FILE"
expect_noop off_file Explore SubagentStart 'agent-rules.off'
rm -f "$OFF_FILE"
expect_digest after_off_file Explore readonly
out=$(printf '' | python3 "$HOOK"); [ "$out" = "{}" ] && ok "empty stdin -> {}" || ko "empty stdin -> $out"
out=$(printf 'not json' | python3 "$HOOK"); [ "$out" = "{}" ] && ok "invalid stdin -> {}" || ko "invalid stdin -> $out"
out=$(printf '[1,2]' | python3 "$HOOK"); [ "$out" = "{}" ] && ok "list stdin -> {}" || ko "list stdin -> $out"
# Copy with a broken rules JSON
mkdir -p "$WORK/broken" && cp "$HOOK" "$WORK/broken/" && printf '{not json' > "$WORK/broken/agent-rules.json"
run "$WORK/broken/agent-rules.py" Explore
if [ "$OUT" = "{}" ] && grep -q 'internal error' "$WORK/stderr"; then ok "broken_rules_json -> {} + stderr"; else ko "broken_rules_json (got: $OUT)"; fi
# Copy without the digests folder
mkdir -p "$WORK/nodigest" && cp "$HOOK" "$HOOKS/agent-rules.json" "$WORK/nodigest/"
run "$WORK/nodigest/agent-rules.py" Explore
if [ "$OUT" = "{}" ] && grep -q 'internal error' "$WORK/stderr"; then ok "missing_digest -> {} + stderr"; else ko "missing_digest (got: $OUT)"; fi
# Copy with an empty digest
mkdir -p "$WORK/empty/agent-rules" && cp "$HOOK" "$HOOKS/agent-rules.json" "$WORK/empty/" && : > "$WORK/empty/agent-rules/readonly.md"
run "$WORK/empty/agent-rules.py" Explore
if [ "$OUT" = "{}" ] && grep -q 'empty' "$WORK/stderr"; then ok "empty_digest -> {} + stderr"; else ko "empty_digest (got: $OUT)"; fi

echo "== Digest size, executable, valid JSON =="
for d in readonly mutating; do
  words=$(wc -w < "$HOOKS/agent-rules/$d.md" | tr -d ' ')
  [ "$words" -le 200 ] && ok "digest $d: $words words (<= 200)" || ko "digest $d: $words words (> 200)"
  [ "$(head -1 "$HOOKS/agent-rules/$d.md")" = "[Harness rules — $d]" ] && ok "digest $d: marker on the first line" || ko "digest $d: marker missing"
done
[ -x "$HOOK" ] && ok "agent-rules.py executable" || ko "agent-rules.py not executable"
[ "$(head -1 "$HOOK")" = "#!/usr/bin/env python3" ] && ok "shebang python3" || ko "unexpected shebang"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$HOOKS/agent-rules.json" 2>"$WORK/stderr" && ok "agent-rules.json valid" || ko "agent-rules.json invalid"

echo "== TOTAL: $pass PASS, $fail FAIL =="
[ "$fail" -eq 0 ]
