#!/usr/bin/env bash
# Tests du hook PostToolUse hooks/edit-check.py : faux linters, fixtures jetables, anti-patterns.
# Usage : tests/edit-check-tests.sh   (sortie : une ligne PASS/FAIL par cas + total, exit 1 si échec)
set -u
HOOKS="$(cd "$(dirname "$0")/../hooks" && pwd)"
HOOK="$HOOKS/edit-check.py"
# edit-check.py lit son interrupteur dans le dossier parent du sien (~/.claude une fois installé)
OFF_FILE="$(dirname "$HOOKS")/edit-check.off"
# pwd normalise le chemin ($TMPDIR finit souvent par « / », le hook normalise le sien)
WORK="$(cd "${TMPDIR:-/tmp}" && pwd)/edit-check-tests.$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

# Copie du hook avec un JSON à timeout court (le script lit edit-check.json à côté de lui).
mkdir -p "$WORK/hook" && cp "$HOOK" "$WORK/hook/"
python3 - "$HOOKS/edit-check.json" "$WORK/hook/edit-check.json" <<'PY'
import json, sys
rules = json.load(open(sys.argv[1]))
rules["timeout_s"] = 1
json.dump(rules, open(sys.argv[2], "w"))
PY
RUN="$WORK/hook/edit-check.py"

pass=0; fail=0
ok() { pass=$((pass+1)); echo "PASS $1"; }
ko() { fail=$((fail+1)); echo "FAIL $1"; [ -s "$WORK/stderr" ] && sed 's/^/     stderr: /' "$WORK/stderr"; }

# mkbin <dir> <name> <exit> [message]  -> faux linter : note $PWD et un témoin, imprime message, sort <exit>
mkbin() {
  local dir="$1" name="$2" code="$3" msg="${4:-}"
  mkdir -p "$dir"
  printf '#!/bin/sh\nprintf "%%s\\n" "$PWD" > "%s/cwd-%s"\n: > "%s/ran-%s"\n[ -n "%s" ] && printf "%%s\\n" "%s"\nexit %s\n' \
    "$WORK" "$name" "$WORK" "$name" "$msg" "$msg" "$code" > "$dir/$name"
  chmod +x "$dir/$name"
}
# run <tool_name> <file_path> <texte> [cwd] [old_string]  -> $OUT (stdout), $CTX (additionalContext), $WORK/stderr
run() {
  local tool="$1" path="$2" text="$3" cwd="${4:-$WORK}" old="${5:-OLD}" payload
  rm -f "$WORK"/ran-* "$WORK"/cwd-*
  if [ "$tool" = "Write" ]; then
    payload=$(jq -cn --arg t "$tool" --arg p "$path" --arg x "$text" --arg c "$cwd" \
      '{session_id:"edit-check-test",cwd:$c,hook_event_name:"PostToolUse",tool_name:$t,tool_input:{file_path:$p,content:$x},tool_response:{}}')
  else
    payload=$(jq -cn --arg t "$tool" --arg p "$path" --arg x "$text" --arg c "$cwd" --arg o "$old" \
      '{session_id:"edit-check-test",cwd:$c,hook_event_name:"PostToolUse",tool_name:$t,tool_input:{file_path:$p,old_string:$o,new_string:$x},tool_response:{}}')
  fi
  OUT=$(printf '%s' "$payload" | python3 "$RUN" 2>"$WORK/stderr")
  CTX=$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null)
}
# expect_noop <cas> [regex attendu sur stderr ; vide = stderr vide exigé]
expect_noop() {
  local name="$1" want="${2:-}" good=0
  if [ "$OUT" = "{}" ]; then
    if [ -z "$want" ]; then [ ! -s "$WORK/stderr" ] && good=1; else grep -q -- "$want" "$WORK/stderr" && good=1; fi
  fi
  if [ "$good" = 1 ]; then ok "$name -> {}"; else ko "$name -> {} (got: ${OUT:0:140})"; fi
}
# expect_ctx <cas> <regex attendu dans additionalContext>
expect_ctx() {
  local name="$1" want="$2"
  if printf '%s' "$CTX" | grep -q -- "$want" && [ ! -s "$WORK/stderr" ]; then ok "$name -> contexte ~ '$want'"; else ko "$name -> contexte ~ '$want' (got: ${OUT:0:160})"; fi
}
# expect_ap <cas> <nombre de findings anti-patterns attendu>
expect_ap() {
  local name="$1" n="$2" got
  got=$(printf '%s' "$CTX" | grep -c -E '^  (ligne [0-9]+|texte ajouté) :')
  if [ "$got" = "$n" ] && [ ! -s "$WORK/stderr" ]; then ok "$name -> $n anti-pattern(s)"; else ko "$name -> $n attendu, $got trouvé(s) (got: ${OUT:0:160})"; fi
}
ran() { [ -e "$WORK/ran-$1" ]; }
expect_ran()     { if ran "$1"; then ok "$2 : $1 lancé"; else ko "$2 : $1 non lancé"; fi; }
expect_not_ran() { if ran "$1"; then ko "$2 : $1 lancé à tort"; else ok "$2 : $1 non lancé"; fi; }
# expect_cwd <linter> <dossier attendu> <cas>
expect_cwd() {
  local want; want=$(cd "$2" && pwd -P)
  if [ "$(cat "$WORK/cwd-$1" 2>/dev/null)" = "$want" ]; then ok "$3 : cwd = $(basename "$2")"; else ko "$3 : cwd = $(cat "$WORK/cwd-$1" 2>/dev/null) (attendu $want)"; fi
}

# ---- fixtures
B="$WORK/proj-biome";  mkdir -p "$B/apps/web"; echo '{}' > "$B/biome.json"; mkbin "$B/node_modules/.bin" biome 0
printf 'const a = 1\n' > "$B/apps/web/x.vue"; printf 'const b = 2\n' > "$B/apps/web/y.ts"; printf '# doc\n' > "$B/apps/web/z.md"
E="$WORK/proj-eslint"; mkdir -p "$E/frontend/src"; : > "$E/frontend/eslint.config.mjs"
mkbin "$E/node_modules/.bin" eslint 1 "src/a.tsx 3:5 error React Hook useState is called conditionally react-hooks/rules-of-hooks"
printf 'x\n' > "$E/frontend/src/a.tsx"
L="$WORK/proj-noconfig"; mkdir -p "$L"; mkbin "$L/node_modules/.bin" eslint 1 "ne doit pas tourner"; printf 'x\n' > "$L/app.vue"
R0="$WORK/proj-py-plain"; mkdir -p "$R0"; printf '[project]\nname = "x"\n' > "$R0/pyproject.toml"; mkbin "$R0/.venv/bin" ruff 1 "ne doit pas tourner"; printf 'x = 1\n' > "$R0/m.py"
R="$WORK/proj-ruff"; mkdir -p "$R/pkg"; printf '[project]\nname = "x"\n\n[tool.ruff]\nline-length = 100\n' > "$R/pyproject.toml"
mkbin "$R/.venv/bin" ruff 1 "pkg/m.py:1:8: F401 'os' imported but unused"; printf 'import os\n' > "$R/pkg/m.py"
R2="$WORK/proj-rufftoml"; mkdir -p "$R2"; : > "$R2/ruff.toml"; mkbin "$R2/.venv/bin" ruff 1 "m.py:1:8: F401 'os' imported but unused"; printf 'import os\n' > "$R2/m.py"
E2="$WORK/proj-fatal"; mkdir -p "$E2"; : > "$E2/eslint.config.js"; mkbin "$E2/node_modules/.bin" eslint 2 "Oops! Something went wrong!"; printf 'x\n' > "$E2/a.js"
E3="$WORK/proj-broken"; mkdir -p "$E3/node_modules/.bin"; : > "$E3/eslint.config.js"; printf '#!/nonexistent/interp\n' > "$E3/node_modules/.bin/eslint"; chmod +x "$E3/node_modules/.bin/eslint"; printf 'x\n' > "$E3/a.js"
S="$WORK/proj-slow"; mkdir -p "$S/node_modules/.bin"; echo '{}' > "$S/biome.json"; printf '#!/bin/sh\nsleep 30\n' > "$S/node_modules/.bin/biome"; chmod +x "$S/node_modules/.bin/biome"; printf 'x\n' > "$S/a.ts"
P="$WORK/proj-both"; mkdir -p "$P"; echo '{}' > "$P/biome.json"; : > "$P/eslint.config.mjs"; mkbin "$P/node_modules/.bin" biome 0; mkbin "$P/node_modules/.bin" eslint 0; printf 'x\n' > "$P/x.ts"
N="$WORK/proj-nested"; mkdir -p "$N/sub"; echo '{}' > "$N/biome.json"; : > "$N/sub/eslint.config.mjs"; mkbin "$N/node_modules/.bin" biome 0; mkbin "$N/node_modules/.bin" eslint 0; printf 'x\n' > "$N/sub/x.ts"
T="$WORK/proj-long"; mkdir -p "$T/node_modules/.bin"; echo '{}' > "$T/biome.json"
printf '#!/bin/sh\ni=0\nwhile [ $i -lt 200 ]; do echo "a.ts:$i:1 lint/x/rule$i message"; i=$((i+1)); done\nexit 1\n' > "$T/node_modules/.bin/biome"; chmod +x "$T/node_modules/.bin/biome"; printf 'x\n' > "$T/a.ts"
A="$WORK/ap"; mkdir -p "$A/src" "$A/__tests__" "$WORK/loose"; printf 'x\n' > "$WORK/loose/x.ts"

echo "== Lint : détection config + binaire, cwd, codes de sortie =="
run Write "$B/apps/web/x.vue" 'const a = 1'
expect_noop biome_exit0_vide; expect_ran biome biome_exit0_vide; expect_cwd biome "$B" biome_monorepo_cwd_racine
mkbin "$B/node_modules/.bin" biome 1 "apps/web/x.vue:1:1 lint/suspicious/noDebugger This is an unexpected use of the debugger statement."
run Write "$B/apps/web/x.vue" 'debugger'
expect_ctx biome_exit1_findings 'noDebugger'; expect_ctx biome_exit1_entete "^\[edit-check\] $B/apps/web/x.vue"; expect_ctx biome_exit1_section '^biome :'; expect_ctx biome_exit1_pied '^Informatif'
run Write "$E/frontend/src/a.tsx" 'x'
expect_ctx eslint_bin_hoiste 'rules-of-hooks'; expect_cwd eslint "$E/frontend" eslint_bin_hoiste
run Write "$L/app.vue" 'x'
expect_noop bin_sans_config; expect_not_ran eslint bin_sans_config
run Write "$R0/m.py" 'x = 1'
expect_noop ruff_pyproject_sans_tool_ruff; expect_not_ran ruff ruff_pyproject_sans_tool_ruff
run Write "$R/pkg/m.py" 'import os'
expect_ctx ruff_tool_ruff_venv 'F401'; expect_cwd ruff "$R" ruff_tool_ruff_venv
run Write "$R2/m.py" 'import os'
expect_ctx ruff_toml 'F401'
run Write "$E2/a.js" 'x'
expect_noop exit2_fatal 'eslint exit 2'
run Write "$E3/a.js" 'x'
expect_noop bin_casse 'lancement impossible'
t0=$(date +%s); run Write "$S/a.ts" 'x'; t1=$(date +%s)
expect_noop timeout 'timeout'
[ $((t1 - t0)) -le 3 ] && ok "timeout : durée $((t1 - t0)) s (<= 3)" || ko "timeout : durée $((t1 - t0)) s (> 3)"
run Write "$B/apps/web/z.md" '# doc'
expect_noop extension_hors_liste; expect_not_ran biome extension_hors_liste
run Write "$B/apps/web/nope.ts" 'const c = 3'
expect_noop fichier_inexistant; expect_not_ran biome fichier_inexistant
run Write "$WORK/loose/x.ts" 'x'
expect_noop hors_projet
mkbin "$B/node_modules/.bin" biome 0
run Write "apps/web/y.ts" 'const b = 2' "$B"
expect_noop file_path_relatif; expect_ran biome file_path_relatif
run Write "$P/x.ts" 'x'
expect_ran biome priorite_biome_sur_eslint; expect_not_ran eslint priorite_biome_sur_eslint
run Write "$N/sub/x.ts" 'x'
expect_ran eslint config_plus_proche; expect_not_ran biome config_plus_proche
run Write "$T/a.ts" 'x'
expect_ctx troncature 'tronqué'
n=$(printf '%s' "$CTX" | grep -c '^  a.ts:'); [ "$n" -le 40 ] && ok "troncature : $n lignes de lint (<= 40)" || ko "troncature : $n lignes (> 40)"
run NotebookEdit "$B/apps/web/y.ts" 'process.env.X ?? "y"'
expect_noop notebookedit_ignore

echo "== Anti-patterns : texte ajouté seulement, exclusions, numéros de ligne =="
run Write "$A/src/config.ts" $'import x from "y"\n\nexport const u = process.env.API_URL ?? "http://localhost:3000"\n'
expect_ap env_ts_nullish_url 2; expect_ctx write_numero_ligne '^  ligne 3 :'; expect_ctx section_ap '^anti-patterns CLAUDE.md :'
run Write "$A/src/port.ts" 'const p = process.env["PORT"] || 3000'
expect_ap env_bracket_or 1
run Write "$A/src/env.ts" 'const env = process.env.NODE_ENV ?? "development"'
expect_ap env_node_env_exclu 0; expect_noop env_node_env_exclu_noop
run Write "$A/src/gate.ts" 'const u = process.env.URL ?? (process.env.NODE_ENV !== "production" ? "http://localhost:3000" : undefined)'
expect_ap env_gate_meme_ligne 0
run Write "$A/src/vite.ts" 'const b = import.meta.env.VITE_API || "x"'
expect_ap import_meta_env 1
run Write "$A/src/c.vue" $'<script setup>\nconst u = process.env.API ?? "x"\n</script>\n'
expect_ap vue_extension 1
run Write "$A/src/settings.py" 'url = os.environ.get("URL", "http://x")'
expect_ap py_environ_defaut 1
run Write "$A/src/settings.py" 'url = os.environ.get("URL", None)'
expect_ap py_environ_none 0
run Write "$A/src/settings.py" 'url = os.getenv("URL", default=None)'
expect_ap py_getenv_default_none 0
run Write "$A/src/settings.py" 'url = os.getenv("URL") or "x"'
expect_ap py_getenv_or 1
run Write "$A/src/settings.py" 'BASE = "http://127.0.0.1:8000"'
expect_ap url_py_127 1
run Write "$A/src/doc.ts" $'// see http://localhost:3000\n# http://localhost:3000\n * http://127.0.0.1\n/* http://0.0.0.0:80 */\n'
expect_ap url_commentaire 0
run Write "$A/vite.config.ts" 'server: { proxy: "http://localhost:3000" }'
expect_ap url_vite_config_exclu 0
run Write "$A/src/config.test.ts" 'const u = process.env.API_URL ?? "http://localhost:3000"'
expect_ap test_file_exclu 0
run Write "$A/__tests__/x.ts" 'const u = process.env.API_URL ?? "http://localhost:3000"'
expect_ap tests_dir_exclu 0
run Write "$HOME/.claude/hooks/__probe__.py" 'url = os.environ.get("URL", "x")'
expect_ap hooks_dir_exclu 0
run Edit "$A/src/config.ts" 'export const u = cfg.apiUrl' "$WORK" 'export const u = process.env.API_URL ?? "http://localhost:3000"'
expect_ap edit_scanne_new_string_seul 0
run Edit "$A/src/config.ts" 'export const u = process.env.API_URL ?? "x"'
expect_ap edit_texte_ajoute 1; expect_ctx edit_sans_numero '^  texte ajouté :'
many=""; for i in $(seq 1 12); do many="${many}const a$i = process.env.X$i ?? \"$i\"
"; done
run Write "$A/src/many.ts" "$many"
expect_ap max_findings 10; expect_ctx max_findings_plafond 'plafond 10'
mkbin "$B/node_modules/.bin" biome 1 "apps/web/y.ts:1:1 lint/suspicious/noDebugger unexpected debugger"
run Write "$B/apps/web/y.ts" $'debugger\nconst u = process.env.API_URL ?? "x"\n'
expect_ctx combine_lint 'biome :'; expect_ctx combine_ap 'anti-patterns CLAUDE.md :'
n=$(printf '%s' "$OUT" | grep -o 'additionalContext' | wc -l | tr -d ' '); [ "$n" = 1 ] && ok "combine : un seul additionalContext" || ko "combine : $n additionalContext"

echo "== Escape hatch, entrées invalides, intégrité =="
touch "$OFF_FILE"
RUN="$HOOK"; run Write "$A/src/config.ts" 'const u = process.env.API_URL ?? "x"'; RUN="$WORK/hook/edit-check.py"
expect_noop off_file 'edit-check.off'
rm -f "$OFF_FILE"
run Write "$A/src/config.ts" 'const u = process.env.API_URL ?? "x"'
expect_ap apres_off_file 1
out=$(printf '' | python3 "$HOOK"); [ "$out" = "{}" ] && ok "stdin vide -> {}" || ko "stdin vide -> $out"
out=$(printf 'not json' | python3 "$HOOK"); [ "$out" = "{}" ] && ok "stdin invalide -> {}" || ko "stdin invalide -> $out"
out=$(printf '{"hook_event_name":"PostToolUse","tool_name":"Write"}' | python3 "$HOOK" 2>"$WORK/stderr"); [ "$out" = "{}" ] && [ ! -s "$WORK/stderr" ] && ok "tool_input absent -> {}" || ko "tool_input absent -> $out"
out=$(printf '{"hook_event_name":"PreToolUse","tool_name":"Write","tool_input":{"file_path":"/x.ts","content":"process.env.A ?? 1"}}' | python3 "$HOOK"); [ "$out" = "{}" ] && ok "mauvais événement -> {}" || ko "mauvais événement -> $out"
[ -x "$HOOK" ] && ok "edit-check.py exécutable" || ko "edit-check.py non exécutable"
[ "$(head -1 "$HOOK")" = "#!/usr/bin/env python3" ] && ok "shebang python3" || ko "shebang inattendu"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$HOOKS/edit-check.json" 2>"$WORK/stderr" && ok "edit-check.json valide" || ko "edit-check.json invalide"

# Intégration réelle (optionnelle) : un vrai biome sur une fixture jetable.
# EDIT_CHECK_REAL_BIOME=<chemin>/node_modules/.bin/biome pour l'activer.
REAL_BIOME="${EDIT_CHECK_REAL_BIOME:-}"
if [ -n "$REAL_BIOME" ] && [ -x "$REAL_BIOME" ]; then
  echo "== Intégration : biome réel =="
  I="$WORK/proj-real"; mkdir -p "$I"; ln -s "$(dirname "$(dirname "$REAL_BIOME")")" "$I/node_modules"
  printf '{"linter":{"enabled":true,"rules":{"recommended":true}}}\n' > "$I/biome.json"; printf 'debugger;\n' > "$I/dbg.ts"
  run Write "$I/dbg.ts" 'debugger;'
  expect_ctx integration_biome_reel 'noDebugger'
fi

echo "== TOTAL : $pass PASS, $fail FAIL =="
[ "$fail" -eq 0 ]
