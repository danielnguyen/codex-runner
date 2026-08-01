# Repository instructions

- Preserve the separate request and approval boundary. Creating a request must never execute it.
- Accept repository IDs only; never add client-supplied filesystem paths or arbitrary command APIs.
- Never expose tokens, authorization headers, credentials, internal paths, raw reasoning, or stack traces.
- Keep Codex in `workspace-write`; never weaken it to full filesystem access.
- Make network access an explicit server-side repository setting and fail closed.
- Add or update tests for every security-sensitive behavior change.
- Keep runtime records and real operator configuration out of Git.
- Run `npm run verify` before reporting completion.
