# codex-runner

`codex-runner` is a small, guarded HTTP service that runs an exact approved prompt through the OpenAI Codex TypeScript SDK in one server-configured local Git repository.

It is intended to be a narrow bridge for a trusted operator-controlled client. It is not a general agent platform, arbitrary shell API, or remote filesystem gateway.

## Architecture

The service has four deliberately separate parts:

1. Fastify handles authenticated HTTP requests and Server-Sent Events (SSE).
2. The repository registry resolves server-side IDs to canonical Git repositories and enforces repository policy.
3. The execution service preserves the request/approval boundary and allows one active run.
4. The filesystem ledger stores immutable requests, mutable state, run summaries, and append-only normalized events.

The production adapter starts `@openai/codex-sdk` with the canonical repository as its working directory. Tests replace that adapter with deterministic fakes and never invoke Codex or use network access.

## Scope

This initial version provides:

- an authenticated repository list with safe metadata;
- immutable exact-prompt requests with SHA-256 hashes;
- a separate hash-bound approval operation;
- asynchronous Codex execution in `workspace-write`;
- explicit per-repository network policy;
- one active execution globally;
- durable run summaries and normalized JSONL events;
- run polling and SSE event replay;
- restart recovery for abandoned runs.

Explicit non-goals include Open WebUI integration, arbitrary client paths, arbitrary command endpoints, general queues, databases, distributed execution, automatic branch or pull-request management, and automatic merging.

## Security boundaries and threat model

The HTTP client is authenticated but is still treated as untrusted input. The client may choose only a configured repository ID and exact prompt. It cannot supply a path, model, sandbox, approval policy, environment variable, network flag, SDK option, or CLI argument.

Important boundaries:

- Bind defaults to `127.0.0.1`. Put a carefully configured authenticated reverse proxy in front only when remote access is required.
- Every `/v1/*` route requires one strong bearer token. `/health` is deliberately unauthenticated and returns no configuration details.
- The expected token and supplied token are hashed and compared in constant time. Authorization headers are redacted from structured logs.
- Repository paths live only in the server-side allowlist. API responses return IDs and display names, never target paths.
- Configured paths are resolved with `realpath`. Each must be the exact Git toplevel, and duplicate IDs or canonical paths are rejected.
- The repository is revalidated during approval and immediately before the SDK starts. A configured clean-worktree policy is checked at both boundaries.
- Codex always receives `sandboxMode: "workspace-write"`, `approvalPolicy: "never"`, and no `skipGitRepoCheck`. Headless approval requests cannot expand permissions; an operation that needs expansion fails.
- Network and Codex web search are disabled for repositories with `networkAccess: false`. For `true`, command network access is enabled only within the normal `workspace-write` sandbox and web search uses cached mode. Treat this as a wider trust boundary.
- Raw reasoning items are ignored. Command output, command text, file paths, MCP arguments/results, and web-search queries are not copied into normalized event records.
- The exact prompt, agent messages, and final response can still contain sensitive repository content. Protect the data directory and API token accordingly.
- The operator's local Codex configuration and installed Codex runtime remain trusted inputs. Managed Codex policy may make the runner more restrictive.

This service does not make hostile prompts safe. Review prompts before approval, keep repositories minimally privileged, keep network off unless required, and run the service as a dedicated operating-system account where practical.

## Requirements

- Node.js 22 or newer and npm. Node.js 22 is the supported CI baseline.
- Codex CLI installed and available to the service account.
- `codex login status` reports an authenticated local session.
- One or more local Git repositories.

No OpenAI API key is required. The SDK wraps the local Codex CLI, and the runner intentionally omits an SDK `env` override so the child process inherits the service account's existing Codex login and environment.

## Setup

Install dependencies and build:

```bash
npm ci
npm run verify
```

Create a repository allowlist outside the repository checkout:

```bash
sudo install -d -m 0700 /etc/codex-runner
sudo install -m 0600 config/repositories.example.json /etc/codex-runner/repositories.json
```

Edit the copied file to identify real local Git repositories. Do not commit that file.

Generate a token with at least 32 random bytes:

```bash
openssl rand -hex 32
```

Export configuration and start the service:

```bash
export CODEX_RUNNER_TOKEN='REPLACE_WITH_RANDOM_TOKEN'
export CODEX_RUNNER_REPOSITORIES_FILE=/etc/codex-runner/repositories.json
export CODEX_RUNNER_DATA_DIR=/var/lib/codex-runner
npm start
```

For development, use `npm run dev`. The application reads process environment variables directly; it does not automatically load `.env` files.

## Configuration

| Variable                         | Required | Default                                              | Description                                                                                                        |
| -------------------------------- | -------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `HOST`                           | No       | `127.0.0.1`                                          | HTTP bind address.                                                                                                 |
| `PORT`                           | No       | `8787`                                               | HTTP port.                                                                                                         |
| `CODEX_RUNNER_TOKEN`             | Yes      | —                                                    | Bearer token containing at least 32 UTF-8 bytes of non-placeholder text. At least 32 random bytes are recommended. |
| `CODEX_RUNNER_REPOSITORIES_FILE` | Yes      | —                                                    | Absolute path to the JSON repository allowlist.                                                                    |
| `CODEX_RUNNER_DATA_DIR`          | No       | `./data` resolved from the process working directory | Local execution ledger directory.                                                                                  |
| `CODEX_MODEL`                    | No       | Local Codex configuration                            | Server-wide model override. Clients cannot set it.                                                                 |
| `LOG_LEVEL`                      | No       | `info`                                               | Pino-compatible level: `fatal`, `error`, `warn`, `info`, `debug`, `trace`, or `silent`.                            |

See [.env.example](.env.example) for placeholder values.

## Repository allowlist

The configuration shape is:

```json
{
  "repositories": [
    {
      "id": "example-repository",
      "displayName": "Example Repository",
      "path": "/srv/projects/example-repository",
      "requireCleanWorktree": true,
      "networkAccess": false
    }
  ]
}
```

Paths must be absolute. Symlinks are resolved, the resolved directory must be a Git repository, and its Git toplevel must exactly equal that directory. A nested directory inside a parent repository is rejected. IDs and canonical paths must be unique.

Clients send only `repositoryId`. Keeping target paths server-side prevents a caller from redirecting Codex toward the runner source, the service account's home directory, another checkout, or any other arbitrary location.

When `requireCleanWorktree` is true, both tracked and untracked changes block approval and execution. The runner never checks out branches, resets files, discards changes, commits, pushes, opens pull requests, or merges. An exact approved Codex prompt may request repository operations, but Codex must perform them within its sandbox and configured permissions.

## API

The examples assume:

```bash
export RUNNER_URL=http://127.0.0.1:8787
export CODEX_RUNNER_TOKEN='REPLACE_WITH_RANDOM_TOKEN'
```

### Health

```bash
curl --fail --silent "$RUNNER_URL/health"
```

### List repositories

```bash
curl --fail --silent \
  -H "Authorization: Bearer $CODEX_RUNNER_TOKEN" \
  "$RUNNER_URL/v1/repositories"
```

### Create an execution request

```bash
curl --fail --silent \
  -H "Authorization: Bearer $CODEX_RUNNER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"repositoryId":"example-repository","prompt":"Inspect the repository and report its status."}' \
  "$RUNNER_URL/v1/execution-requests"
```

The maximum accepted prompt size is 65,536 UTF-8 bytes. The prompt is preserved exactly after JSON decoding; it is not trimmed or rewritten.

### Fetch a request

```bash
curl --fail --silent \
  -H "Authorization: Bearer $CODEX_RUNNER_TOKEN" \
  "$RUNNER_URL/v1/execution-requests/REQUEST_ID"
```

### Approve and start

Use the exact `promptSha256` returned by request creation:

```bash
curl --fail --silent \
  -H "Authorization: Bearer $CODEX_RUNNER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"promptSha256":"PROMPT_SHA256"}' \
  "$RUNNER_URL/v1/execution-requests/REQUEST_ID/approve"
```

A successful approval returns HTTP 202 and a run ID. Repeating approval for an already queued, running, or completed request returns the existing run ID and never launches a duplicate.

### Poll a run

```bash
curl --fail --silent \
  -H "Authorization: Bearer $CODEX_RUNNER_TOKEN" \
  "$RUNNER_URL/v1/runs/RUN_ID"
```

Run statuses are `queued`, `running`, `completed`, `failed`, or `interrupted`.

### Stream events

```bash
curl --no-buffer --fail \
  -H "Authorization: Bearer $CODEX_RUNNER_TOKEN" \
  "$RUNNER_URL/v1/runs/RUN_ID/events"
```

To resume after an observed event:

```bash
curl --no-buffer --fail \
  -H "Authorization: Bearer $CODEX_RUNNER_TOKEN" \
  -H 'Last-Event-ID: 4' \
  "$RUNNER_URL/v1/runs/RUN_ID/events"
```

The stream replays persisted events after the requested ID, follows new events, emits keepalive comments, and closes after terminal completion. Disconnecting a client does not cancel the run.

## Why approval is hash-bound

Creating a request only writes immutable prompt content and separate mutable state. It cannot start Codex. The response includes SHA-256 over the exact UTF-8 prompt.

Approval must present that hash. The runner compares it to the immutable record before it creates a run. A client reviewing one prompt therefore cannot silently approve a different prompt substituted between review and execution. The immutable request file uses exclusive creation, while status changes are written to a separate state file.

## Persistence

The data directory contains:

```text
requests/        immutable request JSON
request-states/ mutable request-state JSON
runs/            mutable run JSON
events/          append-only run JSONL
```

Directories and files are created with restrictive modes. Immutable records use exclusive creation. Mutable JSON uses write, file sync, atomic rename, and directory sync. Event files are append-only and synced after each event.

On startup, runs left in `queued` or `running` are marked `interrupted`; the runner does not pretend that a terminated Codex process completed.

Keep the live data directory on a local filesystem. Atomic rename, exclusive creation, append behavior, locking semantics, and durability guarantees can differ on network filesystems. Runtime data can also contain prompts and model responses, so back it up and retain it according to the operator's data policy. Never place it inside a target repository.

## Development

Available commands:

```bash
npm run dev
npm run build
npm start
npm run typecheck
npm run lint
npm run format:check
npm run format
npm test
npm run verify
```

`npm run verify` runs formatting checks, ESLint, TypeScript type checking, the fake-adapter test suite, and a production build.

## Manual smoke test

Use a disposable repository, never a production checkout:

```bash
SMOKE_ROOT=$(mktemp -d)
git init "$SMOKE_ROOT/repository"
git -C "$SMOKE_ROOT/repository" config user.name operator
git -C "$SMOKE_ROOT/repository" config user.email user@example.invalid
```

Create a temporary allowlist outside this source checkout using the disposable repository's absolute path. Start the runner with a fresh token and a temporary local data directory. Then:

1. Verify `/health` without authentication.
2. Verify `/v1/repositories` with the bearer token and confirm no path is returned.
3. Create a harmless request such as `Report the current Git status without changing files.`
4. Independently review the exact prompt and returned hash.
5. Approve with that hash, poll the run, and stream its events.
6. Confirm the run completed and `git status --porcelain` is still empty.
7. Stop the runner and remove the disposable directory.

This exercises the real locally authenticated SDK. The automated tests use a fake adapter instead and require neither GitHub, Codex login, nor network access.

## Known limitations

- Only one execution can be active across the entire runner.
- There is no Open WebUI participant, Pipe, or approval Action yet.
- There is no cancellation API. The installed SDK exposes `AbortSignal` for `runStreamed()`, but a durable, authenticated cancellation lifecycle is intentionally deferred rather than represented by a fake status-only endpoint.
- The streamed SDK does not return a separate buffered result object. Like the SDK's own buffered implementation, the runner treats the latest completed agent-message item as the final response and records usage from `turn.completed`.
- There is no automatic branch, commit, push, pull-request, or merge management in the runner.
- The filesystem ledger is designed for one local process, not shared or distributed deployment.
- Network-enabled repositories currently receive the SDK's boolean workspace network access control, not a per-domain runner allowlist. Prefer `networkAccess: false`.

The planned next step is a thin Open WebUI Pipe plus an explicit approval Action that calls this API without weakening the request/hash boundary.
