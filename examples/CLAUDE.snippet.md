<!--
Paste this section into ~/.claude/CLAUDE.md (or a project CLAUDE.md).
dod-check.py (Stop hook) blocks the end of a turn that changed something unless the
last assistant message contains BOTH literal markers below, in bold:
  **Verified          (any bold text starting with **Verified, e.g. **Verified**)
  **Not verified      (canonical form: **Not verified, risks**)
The markers are matched as case-sensitive plain substrings, in English: keep them as
written, even if the rest of your CLAUDE.md is in another language (or change
VERIFIED_MARKER / NOT_VERIFIED_MARKER in dod-check.py). Emergency bypass: include
__bypass_dod__.
-->

## Definition of Done — required at the end of every task

**Before the task** (non-trivial): state in one sentence the observable success
criterion ("after this, X will produce Y when we do Z"). If no verifiable criterion
can be stated, rephrase the request before writing code.

**Before declaring it done**, output explicitly:

- **Verified**: the surfaces you actually checked, named concretely
  (paths, commands, files, tests that passed).
- **Not verified, risks**: what could break and was not tested.
  If the list is empty, justify in one sentence why nothing else is at stake;
  otherwise the list is incomplete.

Avoid vague claims such as "docs updated", "tests OK", "it compiles", "done":
always name the surfaces. A Stop hook blocks the end of the turn without this block
as soon as a mutation is detected (Edit/Write, Bash, MCP, Artifact, subagents,
git fingerprint).
