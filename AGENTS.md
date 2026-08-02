# Repository instructions

- Preserve the separate request and approval boundary. Creating a request must never execute it.
- Accept repository IDs only; never add client-supplied filesystem paths or arbitrary command APIs.
- Never expose tokens, authorization headers, credentials, internal paths, raw reasoning, or stack traces.
- Keep Codex in `workspace-write`; never weaken it to full filesystem access.
- Make network access an explicit server-side repository setting and fail closed.
- Add or update tests for every security-sensitive behavior change.
- Keep runtime records and real operator configuration out of Git.
- Keep `src/` and core tests client-agnostic. Core code must never import an integration.
- Integrations may depend only on the documented HTTP API, and client-specific dependencies must stay inside their integration directory.
- Add integration-local tests for integration changes. Preserve the request/hash approval boundary, and never let an integration auto-approve execution.
- Run `npm run verify` before reporting completion.
