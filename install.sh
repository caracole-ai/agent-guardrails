#!/usr/bin/env bash
# agent-guardrails installer.
#
#   ./install.sh                   copy the hooks to ~/.claude/agent-guardrails/ and print
#                                  the settings block to merge into ~/.claude/settings.json
#   ./install.sh --write-settings  same, then merge that block into ~/.claude/settings.json
#                                  after a timestamped backup (never without one)
#
# Existing configuration files in ~/.claude/agent-guardrails/ (*.json, agent-rules/*.md)
# are kept, so your edits survive a reinstall; the hook scripts are always replaced.
# --write-settings refuses (and changes nothing) when settings.json already runs a hook
# with the same script name from another path, and names it.
# Requirements: bash, python3 (3.9+). Nothing is downloaded.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_HOME="$HOME/.claude"
DEST="$CLAUDE_HOME/agent-guardrails"
SETTINGS="$CLAUDE_HOME/settings.json"
EXAMPLE="$SRC/examples/settings.hooks.json"
# Placeholder used in examples/settings.hooks.json, replaced by the absolute install path.
PLACEHOLDER="~/.claude/agent-guardrails"
SCRIPTS="guard.py edit-check.py agent-rules.py dod-snapshot.py dod-check.py"
CONFIGS="guard-rules.json edit-check.json agent-rules.json agent-rules/mutating.md agent-rules/readonly.md"

write_settings=0
for arg in "$@"; do
  case "$arg" in
    --write-settings) write_settings=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "install.sh: python3 is required" >&2; exit 1; }

# ---------------------------------------------------------------- copy
mkdir -p "$DEST/agent-rules"
for f in $SCRIPTS; do
  cp "$SRC/hooks/$f" "$DEST/$f"
  chmod +x "$DEST/$f"
done
for f in $CONFIGS; do
  if [ -e "$DEST/$f" ]; then
    if ! cmp -s "$SRC/hooks/$f" "$DEST/$f"; then
      echo "kept your existing $DEST/$f (the repository version differs: $SRC/hooks/$f)"
    fi
  else
    cp "$SRC/hooks/$f" "$DEST/$f"
  fi
done
echo "installed: $DEST"

# ---------------------------------------------------------------- settings block
BLOCK="$(python3 - "$EXAMPLE" "$PLACEHOLDER" "$DEST" <<'PY'
import json, sys
example, placeholder, dest = sys.argv[1:4]
with open(example, encoding="utf-8") as f:
    block = json.load(f)
for groups in block["hooks"].values():
    for group in groups:
        for hook in group["hooks"]:
            hook["command"] = hook["command"].replace(placeholder, dest)
print(json.dumps(block, indent=2, ensure_ascii=False))
PY
)"

if [ "$write_settings" -eq 0 ]; then
  echo
  echo "Review this block, then merge it into $SETTINGS"
  echo "(or rerun with --write-settings to merge it after a timestamped backup):"
  echo
  printf '%s\n' "$BLOCK"
  exit 0
fi

# ---------------------------------------------------------------- merge
python3 - "$SETTINGS" "$BLOCK" <<'PY'
import json, os, shlex, shutil, sys, time

settings_path, block = sys.argv[1], json.loads(sys.argv[2])["hooks"]


def script_of(command):
    """(basename, absolute path) of the program a hook command runs."""
    try:
        first = shlex.split(command)[0]
    except (ValueError, IndexError):
        first = command.split()[0] if command.split() else ""
    path = os.path.normpath(os.path.expanduser(os.path.expandvars(first)))
    return os.path.basename(path), path


ours = {}
for groups in block.values():
    for group in groups:
        for hook in group["hooks"]:
            name, path = script_of(hook["command"])
            ours[name] = path

if os.path.exists(settings_path):
    with open(settings_path, encoding="utf-8") as f:
        try:
            settings = json.load(f)
        except ValueError as exc:
            sys.exit("install.sh: %s is not valid JSON (%s); nothing changed" % (settings_path, exc))
    if not isinstance(settings, dict):
        sys.exit("install.sh: %s is not a JSON object; nothing changed" % settings_path)
else:
    settings = None

existing = (settings or {}).get("hooks") or {}
if not isinstance(existing, dict):
    sys.exit("install.sh: \"hooks\" in %s is not an object; nothing changed" % settings_path)

# Refuse on a same-name hook at another path; remember the ones already installed here.
conflicts, present = [], set()
for event, groups in existing.items():
    for group in groups or []:
        for hook in (group or {}).get("hooks", []) or []:
            command = hook.get("command") or ""
            if not command:
                continue
            name, path = script_of(command)
            if name not in ours:
                continue
            if path == ours[name]:
                present.add((event, name))
            else:
                conflicts.append("  %s: %s  (this installer would add %s)" % (event, command, ours[name]))
if conflicts:
    sys.exit("install.sh: refusing to merge, %s already runs a hook with the same name "
             "from another path:\n%s\nRemove or rename it, then rerun. Nothing changed."
             % (settings_path, "\n".join(conflicts)))

added = []
merged = dict(existing)
for event, groups in block.items():
    todo = []
    for group in groups:
        hooks = [h for h in group["hooks"] if (event, script_of(h["command"])[0]) not in present]
        if hooks:
            todo.append(dict(group, hooks=hooks))
            added += ["%s: %s" % (event, h["command"]) for h in hooks]
    if todo:
        merged[event] = list(merged.get(event) or []) + todo

if not added:
    print("settings: %s already runs every agent-guardrails hook; nothing changed" % settings_path)
    sys.exit(0)

if settings is None:
    settings = {}
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    print("settings: %s did not exist, creating it (no backup needed)" % settings_path)
else:
    backup = "%s.bak-%s" % (settings_path, time.strftime("%Y%m%d-%H%M%S"))
    n = 1
    while os.path.exists(backup):
        backup = "%s.bak-%s-%d" % (settings_path, time.strftime("%Y%m%d-%H%M%S"), n)
        n += 1
    shutil.copy2(settings_path, backup)
    print("settings: backup written to %s" % backup)

settings["hooks"] = merged
tmp = settings_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, settings_path)
print("settings: merged into %s:" % settings_path)
for line in added:
    print("  + " + line)
PY
