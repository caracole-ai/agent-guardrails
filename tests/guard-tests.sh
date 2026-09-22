#!/usr/bin/env bash
# Tests of the guard hooks/guard.py on synthetic payloads.
# Usage: tests/guard-tests.sh   (output: one PASS/FAIL line per case + total, exit 1 on failure)
set -u
HOOKS="$(cd "$(dirname "$0")/../hooks" && pwd)"
GUARD="$HOOKS/guard.py"
# guard.py reads its kill switch in the parent of its own folder (~/.claude once installed)
OFF_FILE="$(dirname "$HOOKS")/guard.off"
WORK="${TMPDIR:-/tmp}/guard-tests.$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
# check <deny|allow> <tool_name> <json tool_input> [cwd]
check() {
  local expect="$1" tool="$2" input="$3" cwd="${4:-$WORK}"
  local payload out decision
  payload=$(jq -cn --arg t "$tool" --argjson i "$input" --arg c "$cwd" \
    '{session_id:"guard-test",cwd:$c,hook_event_name:"PreToolUse",tool_name:$t,tool_input:$i}')
  out=$(printf '%s' "$payload" | python3 "$GUARD" 2>"$WORK/stderr")
  if printf '%s' "$out" | grep -q '"permissionDecision": *"deny"'; then decision=deny; else decision=allow; fi
  if [ "$decision" = "$expect" ]; then
    pass=$((pass+1)); printf 'PASS %-5s %s %s\n' "$expect" "$tool" "$(printf '%s' "$input" | cut -c1-90)"
  else
    fail=$((fail+1)); printf 'FAIL %-5s (got %s) %s %s\n' "$expect" "$decision" "$tool" "$input"
    [ -s "$WORK/stderr" ] && sed 's/^/     stderr: /' "$WORK/stderr"
  fi
}
bash_check() { check "$1" Bash "$(jq -cn --arg c "$2" '{command:$c}')" "${3:-$WORK}"; }

# Temporary git repositories: one on main, one on feat
git init -q -b main "$WORK/repo-main" && git -C "$WORK/repo-main" commit -q --allow-empty -m init
git init -q -b feat "$WORK/repo-feat" && git -C "$WORK/repo-feat" commit -q --allow-empty -m init
PROJ="$HOME/work/projects/myproject"   # depth 3 under home

echo "== Bash: rm =="
bash_check allow 'ls -la'
bash_check allow 'rm file.txt'
bash_check allow 'rm -rf node_modules'
bash_check allow 'rm -rf ./dist'
bash_check allow "rm -rf $PROJ/.nuxt"
bash_check allow "rm -rf $PROJ/.output/*"
bash_check allow 'rm -rf /private/tmp/claude-501/x/y'
bash_check allow 'rm -rf /tmp/foo'
bash_check allow 'rm -rf $TMPDIR/foo'
bash_check allow 'rm -rf /Volumes/SSD/cache/foo/bar'
bash_check allow "rm -rf $HOME/Library/Caches/foo/bar"
bash_check allow 'echo "rm -rf /" > /tmp/x'
bash_check allow "$(printf 'cat > /tmp/a.sh <<'"'"'EOF'"'"'\nrm -rf /\nEOF')"
bash_check deny  'rm -rf /'
bash_check deny  'rm -rf ~'
bash_check deny  'rm -rf $HOME'
bash_check deny  'rm -rf ~/work'
bash_check deny  "rm -rf $PROJ"
bash_check deny  "rm -rf $PROJ/"
bash_check deny  'rm -rf ~/.claude'
bash_check deny  'rm -rf ~/.claude/projects/foo/memory'
bash_check deny  'rm -rf ~/.ssh'
bash_check deny  'rm -rf /Volumes/SSD'
bash_check deny  'rm -rf /Volumes/SSD/docker'
bash_check deny  'rm -rf ~/.ssh/a/b/c'
bash_check deny  'rm -rf .git'
bash_check deny  'rm -rf *' "$PROJ"
bash_check deny  'rm -rf .' "$PROJ"
bash_check allow 'rm -rf *' "$PROJ/.nuxt"
bash_check deny  'rm -r ~/Documents'
bash_check deny  'rm -Rf ~/Documents'
bash_check deny  'rm --recursive --force ~/Documents'
bash_check deny  'cd ~/work && rm -rf projects'
bash_check deny  'bash -c "rm -rf ~/work"'
bash_check deny  'sudo rm -rf /Users'
bash_check deny  'eval "rm -rf ~/work"'
bash_check deny  'ls; rm -rf ~/work'
bash_check deny  'rm -rf /tmp'
bash_check deny  'rm -rf /usr/local'
bash_check allow 'rm -rf /opt/homebrew/lib/node_modules/foo'

echo "== Bash: git =="
bash_check allow 'git push'
bash_check allow 'git push origin feat'
bash_check allow 'git push --force origin feat'
bash_check allow 'git push --force-with-lease origin feat'
bash_check deny  'git push --force origin main'
bash_check deny  'git push -f origin master'
bash_check deny  'git push origin +main'
bash_check deny  'git push origin --delete main'
bash_check deny  'git push origin :main'
bash_check deny  'git push --force origin HEAD:main'
bash_check deny  'git push --mirror origin'
bash_check deny  'rtk git push --force origin main'
bash_check deny  'git push --force' "$WORK/repo-main"
bash_check allow 'git push --force' "$WORK/repo-feat"
bash_check deny  "git -C $WORK/repo-main push --force-with-lease"
bash_check allow "git -C $WORK/repo-feat push --force-with-lease"
bash_check deny  'git push --force' "$WORK"
bash_check allow 'git branch -D feat'
bash_check deny  'git branch -D main'
bash_check deny  'git branch --delete --force master'
bash_check allow 'git branch -d main'
bash_check deny  'git reset --hard HEAD~1'
bash_check allow 'git reset HEAD~1'
bash_check allow 'git reset --soft HEAD~1'
bash_check deny  'git clean -fdx'
bash_check deny  'git clean -f'
bash_check allow 'git clean -n'
bash_check deny  'git checkout -- .'
bash_check deny  'git checkout .'
bash_check allow 'git checkout -- src/a.ts'
bash_check allow 'git checkout -b feat'
bash_check allow 'git checkout main'
bash_check deny  'git restore .'
bash_check allow 'git restore --staged .'
bash_check allow 'git restore src/a.ts'
bash_check deny  'git stash clear'
bash_check deny  'git stash drop'
bash_check allow 'git stash'
bash_check allow 'git stash pop'
bash_check allow 'git commit --amend --no-edit'
bash_check allow 'git status && git diff'

echo "== Bash: misc =="
bash_check deny  'mkfs.ext4 /dev/sdb'
bash_check deny  'dd if=/dev/zero of=/dev/disk2 bs=1m'
bash_check deny  'diskutil eraseDisk JHFS+ X disk2'
bash_check deny  'sudo shutdown -h now'
bash_check deny  'npm publish'
bash_check deny  'gh repo delete foo/bar --yes'
bash_check deny  'gh pr merge 12 --squash'
bash_check deny  'docker volume prune -f'
bash_check deny  'docker system prune -af --volumes'
bash_check allow 'docker system prune -f'
bash_check allow 'gh pr view 12'
bash_check allow 'npm run build && npm test'
bash_check allow 'cat /etc/hosts | grep local'

echo "== MCP =="
check deny  mcp__hostinger__VPS_recreateVirtualMachineV1 '{}'
check deny  mcp__hostinger__VPS_setRootPasswordV1 '{}'
check deny  mcp__hostinger__VPS_stopVirtualMachineV1 '{}'
check deny  mcp__hostinger__DNS_resetDNSRecordsV1 '{}'
check deny  mcp__hostinger__DNS_deleteDNSRecordsV1 '{}'
check deny  mcp__hostinger__domains_purchaseNewDomainV1 '{}'
check deny  mcp__hostinger__billing_createServiceOrderV1 '{}'
check deny  mcp__plugin_github_github__delete_repository '{}'
check deny  mcp__plugin_github_github__merge_pull_request '{}'
check allow mcp__hostinger__VPS_getVirtualMachinesV1 '{}'
check allow mcp__hostinger__VPS_restartVirtualMachineV1 '{}'
check allow mcp__hostinger__DNS_updateDNSRecordsV1 '{}'
check allow mcp__hostinger__hosting_deployStaticWebsite '{}'
check allow mcp__plugin_github_github__push_files '{}'
check allow mcp__jcodemunch__get_symbol '{}'

echo "== Other tools, escape hatch, invalid input =="
check allow Edit '{"file_path":"/x","old_string":"a","new_string":"b"}'
check allow Read '{"file_path":"/etc/hosts"}'
touch "$OFF_FILE"
bash_check allow 'rm -rf ~'
rm -f "$OFF_FILE"
bash_check deny  'rm -rf ~'
out=$(printf '' | python3 "$GUARD"); [ "$out" = "{}" ] && { pass=$((pass+1)); echo "PASS empty stdin -> {}"; } || { fail=$((fail+1)); echo "FAIL empty stdin -> $out"; }
out=$(printf 'not json' | python3 "$GUARD"); [ "$out" = "{}" ] && { pass=$((pass+1)); echo "PASS invalid stdin -> {}"; } || { fail=$((fail+1)); echo "FAIL invalid stdin -> $out"; }

echo "== TOTAL: $pass PASS, $fail FAIL =="
[ "$fail" -eq 0 ]
