<!--
Paste this section into ~/.claude/CLAUDE.md (or a project CLAUDE.md).
dod-check.py (Stop hook) blocks the end of a turn that changed something unless the
last assistant message contains BOTH literal markers below, in bold:
  **Vérifié**   (also accepted: **Verifie, or any bold text starting with **Vérifié)
  **Non vérifié (also accepted: **Non verifie; canonical form: **Non vérifié, risque**)
The markers are matched as plain substrings, in French: keep them as written, even if
the rest of your CLAUDE.md is in English. Emergency bypass: include __bypass_dod__.
-->

## Definition of Done — required at the end of every task

**Before the task** (non-trivial): state in one sentence the observable success
criterion ("after this, X will produce Y when we do Z"). If no verifiable criterion
can be stated, rephrase the request before writing code.

**Before declaring it done**, output explicitly:

- **Vérifié** (verified): the surfaces you actually checked, named concretely
  (paths, commands, files, tests that passed).
- **Non vérifié, risque** (not verified, risk): what could break and was not tested.
  If the list is empty, justify in one sentence why nothing else is at stake;
  otherwise the list is incomplete.

Avoid vague claims such as "docs updated", "tests OK", "it compiles", "done":
always name the surfaces. A Stop hook blocks the end of the turn without this block
as soon as a mutation is detected (Edit/Write, Bash, MCP, Artifact, subagents,
git fingerprint).
