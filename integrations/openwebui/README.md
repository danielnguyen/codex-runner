# Open WebUI Pipe

This optional Pipe Function makes codex-runner available as a selectable Open WebUI model named **Codex**. It sends only the current plain-text user message to one administrator-configured repository ID, requires explicit human authorization, follows normalized Server-Sent Events (SSE), and returns the final Codex response as durable assistant content.

The Pipe contains no Codex execution logic. Its dependency direction is:

```text
Open WebUI Pipe -> codex-runner HTTP API -> Codex SDK
```

## Security boundary

Open WebUI owns chat interaction, administrator Valve configuration, access to the Pipe, and the live confirmation dialog. codex-runner remains authoritative for bearer authentication, repository allowlisting and policy, immutable request storage, prompt-hash validation, global concurrency, Codex execution, and durable persistence.

The Pipe fails closed when administrator role information is unavailable. It never accepts a repository ID, runner URL, token, execution option, or arbitrary API path from chat content. A hash and request ID are accepted only in the exact approval-command form described below. It never auto-approves or infers a pending request.

Status updates contain only fixed descriptions and identifiers appear only in the durable result. Raw command text, command output, paths, hidden reasoning, prior chat messages, and authentication material are not emitted.

## Requirements

- Open WebUI 0.11.0 or newer.
- A reachable codex-runner instance configured with an allowlisted repository.
- An administrator-managed codex-runner bearer token.

The Python file uses `httpx` and Pydantic, which are dependencies of current Open WebUI releases. Legacy Open WebUI Pipelines are not used.

## Install and enable

Use Open WebUI's Functions administration page to import [`codex_runner.py`](codex_runner.py). You can paste the file directly or provide its raw GitHub URL from a trusted revision of this public repository:

```text
https://raw.githubusercontent.com/OWNER/codex-runner/REF/integrations/openwebui/codex_runner.py
```

Replace `OWNER` and `REF` with the repository owner and a reviewed tag or commit. Review the code before enabling it, then enable the Function and configure its Valves.

Restrict model access to trusted operators. Keep `ADMIN_ONLY` enabled unless the deployment has another deliberate and tested authorization boundary.

To disable or uninstall the adapter, disable or delete the Function in Open WebUI. This does not delete pending requests or runs already persisted by codex-runner.

## Valves

| Valve                     | Default | Purpose                                                                                                                                                                          |
| ------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `RUNNER_URL`              | empty   | Absolute `http` or `https` base URL for codex-runner, for example `http://runner.example.internal:8787`. User information, query strings, fragments, and redirects are rejected. |
| `RUNNER_TOKEN`            | empty   | codex-runner bearer token. The Valve uses a masked password input.                                                                                                               |
| `REPOSITORY_ID`           | empty   | The single server-side repository ID used by this Pipe instance, for example `example-repository`.                                                                               |
| `ADMIN_ONLY`              | `true`  | Reject users whose Open WebUI role is not `admin`; missing role data also fails closed.                                                                                          |
| `CONNECT_TIMEOUT_SECONDS` | `5`     | Bounded connection timeout.                                                                                                                                                      |
| `API_TIMEOUT_SECONDS`     | `30`    | Bounded timeout for ordinary runner API requests.                                                                                                                                |
| `RUN_TIMEOUT_SECONDS`     | `7200`  | Bounded client wait limit for an approved run; it is not cancellation.                                                                                                           |

Masked Valve input prevents casual visual disclosure but does not encrypt values at rest by itself. Enable Open WebUI Valve encryption (`ENABLE_VALVE_ENCRYPTION`) where supported and protect the Open WebUI database. Never place the runner token in a user prompt, chat transcript, screenshot, or source-controlled configuration.

Open WebUI must be able to reach `RUNNER_URL`. Prefer a private, authenticated network path and TLS when traffic crosses hosts. codex-runner still authenticates every `/v1/*` request.

## Exact-prompt and approval behavior

In direct chat, the Pipe prefers Open WebUI's reserved `__metadata__["user_prompt"]`, which contains the current user message before source wrapping. A direct test/API path without metadata falls back to the latest plain-text user message in `body["messages"]`. Direct-chat prompt text is not altered.

The current Open WebUI Channel dispatch path is different: it stores the authored Channel message, replaces structured model mentions with their display labels, prepends the sender's display name, and supplies that decorated text to the chat-completion pipeline. Consequently, the generic `user_prompt` behavior documented for Pipes does not currently provide raw authored text in this Channel path.

For a Channel invocation, the Pipe uses the supported `__request__` reserved argument. Open WebUI passes through the same FastAPI request accepted by the Channel message-post route, whose JSON body contains the persisted current message. The Pipe cross-checks the Channel route against `chat_id`, `session_id`, and `message_id` metadata. It then recognizes only Open WebUI's structured leading model-mention token for the exact invoked model and removes that routing token and its single separator. It does not heuristically strip arbitrary speaker names, `Codex`, `@Codex`, or text that resembles a prefix. A legitimate prompt beginning with those strings is preserved.

If the raw Channel request, matching metadata, or unambiguous structured routing mention is unavailable, the Pipe returns `CHANNEL_RAW_PROMPT_UNAVAILABLE` before creating or approving any runner request. It never submits the decorated Channel prompt as a substitute.

For normal execution requests, the selected string after authoritative Channel routing removal is preserved exactly. The Pipe does not trim, rewrite, summarize, prepend, append, combine history, include the system prompt, or resolve references such as “execute the plan above.” In a Channel, paste the complete Codex prompt into the message that invokes this Pipe. The maximum is 65,536 UTF-8 bytes. Attachments, sources, images, multimodal message content, and other non-text input are rejected. A prompt containing the configured runner token is also rejected before request creation so the secret cannot enter confirmation or durable output.

The only interpreted message syntax is a complete explicit approval command:

```text
approve <request-id> <prompt-sha256>
```

An optional routing mention is accepted:

```text
@Codex approve <request-id> <prompt-sha256>
```

The `approve` verb and `@Codex` mention are case-insensitive. The request ID must be a complete lowercase UUID, and the hash must contain exactly 64 lowercase hexadecimal characters. Missing or abbreviated identifiers, uppercase hashes, extra instructions, embedded commands, and `approve latest` are rejected. No repository ID or prompt is accepted in this command.

### Direct-chat modal approval

The direct-chat lifecycle is:

1. Validate configuration, role, and the exact prompt.
2. Create an immutable `pending_approval` request through codex-runner.
3. Display the configured repository ID, runner request ID, exact SHA-256, UTF-8 byte count, and full exact prompt in an Open WebUI confirmation dialog.
4. If confirmed, submit exactly the hash returned by request creation to the approval endpoint.
5. Follow the existing run ID through the runner SSE endpoint and return the final run record as one durable Markdown response.

Clicking Cancel is an explicit rejection. The Pipe reports that distinct outcome, does not call approval, and leaves the immutable request pending with no run.

If the confirmation call raises, times out, disconnects, or returns an unsupported result, the Pipe does not describe the outcome as rejection. It returns a durable pending-approval response containing the full exact prompt, identifiers, byte count, and explicit approval command.

### Channel two-message approval

Open WebUI may be unable to deliver an interactive confirmation dialog through a Channel session even though request creation succeeds. This is an Open WebUI client-integration behavior, not a codex-runner core limitation. The Pipe safely falls back after the failed confirmation call.

Use this explicit two-message flow:

1. Type `@`, select **Codex** from Open WebUI's mention list, then enter the complete exact prompt.
2. Review the durable pending response.
3. For approval, type `@`, select **Codex** from the mention list again, then paste only:

   ```text
   approve <request-id> <prompt-sha256>
   ```

Pasting literal `@Codex` text does not create Open WebUI's structured model mention. Selecting Codex from the autocomplete provides the routing mention; the copyable command block intentionally starts with `approve` so it does not duplicate the visible mention.

For both messages, the adapter obtains the persisted authored Channel message from the authoritative request source before normal request creation or approval-command parsing. The structured `@Codex` routing mention is excluded; no sender display name or plain model label is forwarded to codex-runner.

The second invocation does not create another request. The adapter fetches `GET /v1/execution-requests/:requestId` and verifies the returned request ID, configured repository ID, stored prompt, exact stored hash, and pending or idempotently-started status before calling approval with the stored hash. A wrong identifier, hash, repository, missing request, or conflicting status cannot start a run.

No run starts until either the direct-chat modal is positively confirmed or the exact second-message approval command is verified. Sending that command is explicit authorization to start Codex in the configured allowlisted repository.

## Progress, fallback, and cancellation

The Pipe emits only fixed, non-sensitive `status` events. It does not emit assistant message deltas; the return value from `pipe()` is the only durable assistant content.

SSE replay begins with the runner's persisted events and follows the active run to a terminal event. If streaming fails after a run ID exists, the Pipe reports that live updates were interrupted and polls `GET /v1/runs/:runId` within the configured wait limit. It does not create another request, approve again, or start another run.

codex-runner has no durable cancellation API. Closing the browser tab or losing the Channel response path before approval leaves the immutable request pending; it neither approves nor cancels anything. After approval, losing the UI path can interrupt live updates but does not cancel the execution. A client wait timeout likewise does not cancel or locally relabel the run.

## Troubleshooting

- **Runner unreachable:** verify `RUNNER_URL`, routing, TLS trust, and the configured timeouts. No automatic POST retry occurs.
- **401 or 403:** verify the bearer token and, with `ADMIN_ONLY=true`, the caller's Open WebUI role. Do not paste the token into chat.
- **Repository unavailable:** verify the configured `REPOSITORY_ID` exists and that codex-runner can revalidate its server-side path.
- **Dirty worktree:** clean the allowlisted disposable or working repository according to operator policy; the Pipe cannot override this check.
- **Execution already active:** codex-runner permits one active execution globally. Wait for it to finish before approving another request.
- **Browser closed during confirmation:** the confirmation call fails or times out. The Pipe does not approve the request; when delivery remains available, its durable response supplies the explicit approval command.
- **Browser closed after approval:** live UI delivery can stop, but the runner continues. Use its authenticated run endpoint and recorded IDs to inspect the outcome.
- **`CHANNEL_RAW_PROMPT_UNAVAILABLE`:** Open WebUI did not expose a consistent raw Channel message for this invocation. The Pipe refused before request creation or approval; retry from a supported context instead of copying decorated prompt text.

## Local integration tests

From this directory, create an isolated environment and run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pytest
```

The tests use `httpx.MockTransport`; they require no Open WebUI instance, runner process, repository, Codex login, secret, or network access.

## Manual smoke test

Use a disposable allowlisted Git repository and non-production credentials:

1. Import and enable the Function in a compatible Open WebUI instance.
2. Configure its Valves and restrict access.
3. Send a complete harmless read-only prompt.
4. Reject the first confirmation and verify no run is created.
5. Repeat, approve the exact displayed hash and prompt, and observe safe status updates.
6. Verify the returned request ID, run ID, hash, and final response against codex-runner.
7. Verify the disposable target worktree remains clean.

Do not weaken repository policy or bypass confirmation to perform this test.
