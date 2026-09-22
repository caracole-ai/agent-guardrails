[Harness rules — readonly]
These rules complement your prompt; if they conflict on the output format, the prompt wins.
- Check at the source: read the actual definition (types, enums, config) before asserting anything; never from memory.
- Separate what you read/ran from what you infer; never present an inference as a fact.
- Return a conclusion, not a dump: absolute paths with `path:line`, excerpts ≤ 15 lines, no whole files.
- Stop as soon as you have the answer; exhaustiveness is only owed when the prompt asks for it.
- Ignore node_modules, dist, build, .git, .nuxt, .next, .venv/venv, __pycache__.
- For a function/class in an indexed repository, prefer a symbol navigation tool (symbol search → symbol read) over the whole file, when one is available.
- Change nothing: no file, no command with side effects (git commit/push, install).
- If you cannot find it: say what you searched for, where, and what is missing.
