# agent-guardrails

[![tests](https://github.com/caracole-ai/agent-guardrails/actions/workflows/tests.yml/badge.svg)](https://github.com/caracole-ai/agent-guardrails/actions/workflows/tests.yml)

Four small hooks for [Claude Code](https://code.claude.com/docs/en/hooks) that keep
autonomous agents from doing irreversible damage, and make them say what they
actually checked. Python 3 standard library only, no network, MIT.

## The problem

Agent fleets run in permission-free mode (`--dangerously-skip-permissions`): no prompt
ever reaches a human before a command runs. `permissions.deny` rules in `settings.json`
still apply in that mode ("Deny rules block in every mode, including
`bypassPermissions`", [permission modes](https://code.claude.com/docs/en/permission-modes)),
but they are static patterns: they cannot ask which branch `git push --force` targets,
where a `cd` left the shell before `rm -rf`, or what runs inside `bash -c "…"`. A
`PreToolUse` hook that returns `permissionDecision: "deny"` prevents the call and can
decide with that context; that is what `guard.py` does, and the other three hooks close
the loop on quality and reporting.

## The four guardrails

**1. `guard.py` (PreToolUse, `Bash|mcp__.*`): refuses irreversible actions.**
Denies recursive `rm` on a protected surface (`/`, system folders, your home, anything
up to `home_max_depth` = 3 levels under home, which covers project folders, external
volumes up to depth 2, prefixes such as `~/.ssh` or `~/.claude/projects`, any `.git`);
`git push --force/--delete/+ref/--mirror` to a protected branch (or to the current branch
when no refspec is given), `git branch -D` of a protected branch, `git reset --hard`,
`git clean -f`, `git checkout .`/`git restore .`, `git stash clear/drop`; `mkfs`, `dd` to
`/dev`, `diskutil erase*`, `shutdown`/`reboot`, `npm publish`, `gh repo delete`,
`gh pr merge`, `docker volume rm/prune`; and MCP tools whose name matches `deny_tools`.
It follows `&&`, `||`, `;`, `|`, wrappers (`sudo`, `env`, `nohup`…), `bash -c`, `eval`
and intermediate `cd`; heredoc bodies are ignored (text, not commands). Every refusal is
appended to `~/.claude/guard.log`. The human can disable it with `touch ~/.claude/guard.off`.
*What it does not do:* it does not analyse `python3 -c "shutil.rmtree(…)"`,
`find -delete` or `xargs rm`; it stops common catastrophic gestures, it does not resist a
determined workaround. On an internal error (including an unreadable `guard-rules.json`)
it **fails open**: the command runs and one line goes to stderr, because a guard that
breaks every command would be worse than none.

**2. `edit-check.py` (PostToolUse, `Edit|Write`): immediate feedback after each write.**
Runs the project's own linter on the edited file (Biome, ESLint or Ruff), only when both
the nearest config file and a local binary (`node_modules/.bin/…`, `.venv/bin/…`) exist;
nothing global is assumed on `PATH`. Then scans the *added text* (never the re-read file)
for two anti-patterns: silent environment-variable fallbacks (`process.env.X ?? …`,
`os.environ.get("X", default)`…) and hard-coded local URLs (`localhost`, `127.0.0.1`,
`0.0.0.0`). Findings reach the model in the same turn through `additionalContext`.
*What it does not do:* it never blocks (informative only), does not format or type-check,
stays silent when the linter cannot run (exit code other than 0/1, timeout), and fails
open on internal errors. Kill switch: `~/.claude/edit-check.off`.

**3. `agent-rules.py` (SubagentStart): gives subagents the rules.**
A subagent started with the Agent tool gets its own system prompt, not your
`~/.claude/CLAUDE.md`. This hook injects a short digest chosen by `agent_type`:
`readonly.md` for research/planning agents, nothing for `skip_types` (`fork`,
`statusline-setup`), `mutating.md` for everything else, including unknown types (the safe
default). *What it does not do:* it does not enforce anything, it only adds context; a
missing or empty digest, a broken JSON or an internal error means no injection (fail-open,
one line on stderr). Kill switch: `~/.claude/agent-rules.off`.

**4. `dod-snapshot.py` + `dod-check.py` (UserPromptSubmit + Stop): Definition of Done.**
At each prompt, `dod-snapshot.py` records a fingerprint of the git state of the project
(status, diff against `HEAD`, size and mtime of untracked files). At the end of the turn,
`dod-check.py` detects a mutation either from that fingerprint or from the tool calls of
the turn (Edit/Write, mutating Bash such as `sed -i`, redirections outside temp folders,
`git commit`, `npm install`…, writing MCP tools, Artifact publishes, non-read-only
subagents). If something changed and the last answer lacks the two markers `**Verified`
and `**Not verified`, it returns `decision: "block"` with the expected format, so the agent
has to name what it verified and what it did not (see `examples/CLAUDE.snippet.md`).
*What it does not do:* mutation detection is heuristic, the fingerprint only covers the
git repository of the session's working directory, and the answer is checked for the
markers, not for the truth of what they say. `stop_hook_active` is honoured (no loop) and
`__bypass_dod__` in the answer lets a turn end. `dod-snapshot.py` never blocks.

Hook messages, the default digests and the DoD markers are in English. The digests and the
rule files are plain text you can rewrite in your language; the DoD markers are matched
literally (case-sensitive) and live in two constants, `VERIFIED_MARKER` and
`NOT_VERIFIED_MARKER`, at the top of `dod-check.py`.

## Installation

```sh
git clone https://github.com/caracole-ai/agent-guardrails && cd agent-guardrails && ./install.sh
```

`install.sh` copies the hooks to `~/.claude/agent-guardrails/` (existing configuration
files there are kept) and prints the `hooks` block to merge into `~/.claude/settings.json`
([`examples/settings.hooks.json`](examples/settings.hooks.json), with absolute paths).
Read it before merging. To let the installer merge it:

```sh
./install.sh --write-settings
```

It writes a timestamped backup (`settings.json.bak-YYYYmmdd-HHMMSS`) before touching
the file, appends to existing hook events without removing anything, skips hooks already
installed at the same path, and refuses (changing nothing) when `settings.json` already
runs a hook with the same script name from another path, which it names. Then add the
Definition of Done rule from [`examples/CLAUDE.snippet.md`](examples/CLAUDE.snippet.md)
to your `CLAUDE.md`. Requirements: `bash`, `python3` 3.9+, `git`.

To uninstall, remove the five entries from `settings.json` and delete
`~/.claude/agent-guardrails/`.

## Configuration

All rules live next to the scripts, in `~/.claude/agent-guardrails/`:

- `guard-rules.json`: `protected_branches`, `home_max_depth`, `volumes_max_depth`,
  `protected_prefixes`, `tmp_prefixes` (where recursive `rm` is fine), the `git` switches,
  `deny_command_regex` (per Bash segment) and `deny_tools` (regex on the MCP tool name; the
  shipped Hostinger and GitHub entries are examples, adapt them to your MCP servers).
- `edit-check.json`: `linters` (config files, extensions, local binary path, arguments),
  `antipatterns.rules` (line regexes with messages) and `antipatterns.exclude_path_regex`
  (tests, `*.config.*`, `~/.claude/hooks`, `~/.claude/agent-guardrails` by default),
  plus timeouts and output caps.
- `agent-rules.json`: `readonly_types`, `skip_types` and the `digests` map.
- `agent-rules/readonly.md`, `agent-rules/mutating.md`: the digests themselves. They are
  examples derived from one `CLAUDE.md`; replace them with your own rules (the tests check
  each stays under 200 words and starts with its `[Harness rules — …]` marker).

Each file has a `_doc` array or a header comment describing its fields.

## Tests

```sh
tests/guard-tests.sh && tests/edit-check-tests.sh && tests/agent-rules-tests.sh && tests/dod-tests.sh
```

Each script prints one PASS/FAIL line per case and exits non-zero on any failure. They
run against the copies in `hooks/` (relative paths; `DOD_SCRIPTS=<dir>` points
`dod-tests.sh` at another copy) and need `bash`, `python3`, `jq` and `git`.

| Script | Cases | Covers |
|---|---:|---|
| `guard-tests.sh` | 115 | rm surfaces, git push/branch/reset/clean/checkout/stash, compound commands, wrappers, `bash -c`, `eval`, `cd`, heredocs, misc commands, MCP deny list, kill switch, invalid input |
| `edit-check-tests.sh` | 70 | linter discovery (config + local binary, hoisting, nearest config), exit codes, timeout, truncation, anti-patterns on added text, exclusions, line numbers, kill switch, invalid input |
| `agent-rules-tests.sh` | 27 | digest per agent type, skips, wrong event, kill switch, broken JSON, missing or empty digest, digest size |
| `dod-tests.sh` | 62 | transcript scan (Edit, Bash, MCP, Artifact, Agent), git fingerprint (tracked, untracked, ignored, subfolder, no repo), robustness |

CI runs the four scripts on Ubuntu and macOS (`.github/workflows/tests.yml`).
Set `EDIT_CHECK_REAL_BIOME=<path>/node_modules/.bin/biome` to add an optional
integration case against a real Biome binary.

## Limits and non-goals

- Not a sandbox: no filesystem or process isolation, no network filtering. Run fully
  unattended agents in a container or VM as well.
- No prompt-injection detection.
- The guard reads command text; code that deletes through an interpreter or a tool it
  does not parse gets through (see above). Every hook fails open on internal errors.
- Built and tested for Claude Code hooks. Other agent harnesses: not tested.
- Optional companion: [RTK](https://github.com/rtk-ai/rtk), a CLI proxy that trims command output to save
  tokens, whose
  `rtk …` / `rtk proxy …` prefixes the guard and the DoD check see through.

## Measure what gets through

The guardrails stop a list of known gestures; they do not tell you what a task cost or
how much work was redone. [`agent-cost-meter`](https://github.com/caracole-ai/agent-cost-meter)
reads Claude Code transcripts and reports cost, tokens, retries and waste per task.

## En français

Quatre hooks pour Claude Code, en Python 3 sans dépendance : `guard.py` refuse les gestes
irréversibles (rm récursif sur une surface protégée, force-push sur `main`, `git reset
--hard`, outils MCP destructifs…) même en mode sans permission ; `edit-check.py` renvoie au
modèle, dans le même tour, le lint du projet et deux anti-patterns sur le texte ajouté ;
`agent-rules.py` injecte un digest de règles à chaque sous-agent ; `dod-snapshot.py` et
`dod-check.py` bloquent la fin d'un tour qui a modifié quelque chose sans le bloc
**Verified** / **Not verified, risks**. Tous laissent passer en cas d'erreur interne
(fail-open) et ne remplacent ni un bac à sable ni un filtrage réseau. Installation :
`./install.sh`, puis relire le bloc imprimé (ou `./install.sh --write-settings`, avec
sauvegarde horodatée de `settings.json`). Les messages des hooks, les digests et les
marqueurs de la Definition of Done sont en anglais : écrivez le bloc avec `**Verified` et
`**Not verified` tels quels (casse comprise). Pour le rédiger en français, changez les
constantes `VERIFIED_MARKER` et `NOT_VERIFIED_MARKER` en tête de `dod-check.py` (par exemple
`**Vérifié` et `**Non vérifié`) et le snippet de `examples/CLAUDE.snippet.md` en conséquence.
