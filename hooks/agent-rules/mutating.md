[Harness rules — mutating]
These rules complement your prompt; if they conflict on the output format, the prompt wins.
- Coherence first: before writing, read how the codebase already solves a similar problem (one sibling + the call site) and follow its patterns (naming, structure, flow).
- No hardcoded values: import from the source of truth (enum, config, constant); if it does not exist, create it.
- Check types and enums at the source before using them, never from memory.
- Surgical changes: every changed line traces back to the request, no adjacent improvements; pre-existing dead code = mention it, do not delete it; imports orphaned by your changes = clean them up.
- Silent server-side env var fallback (`process.env.X ?? "…"`) = anti-pattern: throw at boot (`assertEnv`) or gate on `NODE_ENV !== 'production'`.
- End your report with **Verified** (concrete surfaces: paths, commands, tests that passed) and **Not verified, risks** (what was not tested; if empty, justify in one sentence). Expected by the parent process.
