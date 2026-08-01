import { createHash } from "node:crypto";
import { writeFile, rm } from "node:fs/promises";

import type { FastifyInstance } from "fastify";
import { afterEach, describe, expect, it } from "vitest";

import { buildApp } from "../src/app.js";
import type { CodexAdapter } from "../src/codex/codex-adapter.js";
import { MAX_PROMPT_BYTES } from "../src/executions/execution-service.js";
import { ExecutionStore } from "../src/executions/execution-store.js";
import { RepositoryRegistry } from "../src/repositories/repository-registry.js";
import {
  BlockingFakeCodexAdapter,
  FailingFakeCodexAdapter,
  SuccessfulFakeCodexAdapter,
  TEST_TOKEN,
  makeConfig,
  makeGitRepository,
  makeTemporaryDirectory,
  waitForTerminal,
  writeRepositoryConfig,
} from "./helpers.js";

interface Harness {
  root: string;
  repositoryPath: string;
  app: FastifyInstance;
  store: ExecutionStore;
}

const harnesses: Harness[] = [];

afterEach(async () => {
  await Promise.all(
    harnesses.splice(0).map(async (harness) => {
      await harness.app.close();
      await rm(harness.root, { recursive: true });
    }),
  );
});

async function makeHarness(
  codex: CodexAdapter = new SuccessfulFakeCodexAdapter(),
  options: {
    requireCleanWorktree?: boolean;
    logger?: Parameters<typeof buildApp>[0]["logger"];
  } = {},
): Promise<Harness> {
  const root = await makeTemporaryDirectory("codex-runner-api-");
  const repositoryPath = await makeGitRepository(root);
  const repositoriesFile = await writeRepositoryConfig(root, [
    {
      id: "example",
      path: repositoryPath,
      requireCleanWorktree: options.requireCleanWorktree ?? true,
    },
  ]);
  const config = makeConfig(root, repositoriesFile);
  const repositories = await RepositoryRegistry.load(repositoriesFile);
  const store = new ExecutionStore(config.dataDir);
  const app = await buildApp({
    config,
    repositories,
    store,
    codex,
    logger: options.logger ?? false,
  });
  const harness = { root, repositoryPath, app, store };
  harnesses.push(harness);
  return harness;
}

function authorization(token = TEST_TOKEN): Record<string, string> {
  return { authorization: `Bearer ${token}` };
}

async function createRequest(
  app: FastifyInstance,
  prompt = "Inspect the repository and report status.",
): Promise<{ requestId: string; promptSha256: string }> {
  const response = await app.inject({
    method: "POST",
    url: "/v1/execution-requests",
    headers: authorization(),
    payload: { repositoryId: "example", prompt },
  });
  expect(response.statusCode).toBe(201);
  return response.json<{ requestId: string; promptSha256: string }>();
}

async function approveRequest(
  app: FastifyInstance,
  request: { requestId: string; promptSha256: string },
): Promise<{ runId: string }> {
  const response = await app.inject({
    method: "POST",
    url: `/v1/execution-requests/${request.requestId}/approve`,
    headers: authorization(),
    payload: { promptSha256: request.promptSha256 },
  });
  expect(response.statusCode).toBe(202);
  return response.json<{ runId: string }>();
}

describe("HTTP API", () => {
  it("allows unauthenticated health checks", async () => {
    const { app } = await makeHarness();
    const response = await app.inject({ method: "GET", url: "/health" });
    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual({ status: "ok", service: "codex-runner" });
  });

  it("rejects missing and incorrect authentication on v1 routes", async () => {
    const { app } = await makeHarness();
    const missing = await app.inject({
      method: "GET",
      url: "/v1/repositories",
    });
    const incorrect = await app.inject({
      method: "GET",
      url: "/v1/repositories",
      headers: authorization("incorrect-token-value-that-is-long-enough"),
    });
    expect(missing.statusCode).toBe(401);
    expect(incorrect.statusCode).toBe(401);
    expect(missing.json()).toMatchObject({ error: { code: "UNAUTHORIZED" } });
  });

  it("does not write bearer token values to logs", async () => {
    let logs = "";
    const { app } = await makeHarness(new SuccessfulFakeCodexAdapter(), {
      logger: {
        level: "info",
        stream: { write: (message: string) => (logs += message) },
      },
    });
    await app.inject({
      method: "GET",
      url: "/v1/repositories",
      headers: authorization(),
    });
    expect(logs).not.toContain(TEST_TOKEN);
    expect(logs).not.toContain(`Bearer ${TEST_TOKEN}`);
  });

  it("returns only safe repository metadata", async () => {
    const { app, repositoryPath } = await makeHarness();
    const response = await app.inject({
      method: "GET",
      url: "/v1/repositories",
      headers: authorization(),
    });
    expect(response.statusCode).toBe(200);
    expect(response.body).not.toContain(repositoryPath);
    expect(response.json()).toEqual({
      repositories: [
        {
          id: "example",
          displayName: "Example Repository",
          available: true,
          requireCleanWorktree: true,
          networkAccess: false,
        },
      ],
    });
  });

  it("creates an immutable request without executing Codex and hashes the exact prompt", async () => {
    const codex = new SuccessfulFakeCodexAdapter();
    const { app } = await makeHarness(codex);
    const prompt = "  Preserve whitespace.\nUnicode: café 🚀\n";
    const response = await app.inject({
      method: "POST",
      url: "/v1/execution-requests",
      headers: authorization(),
      payload: { repositoryId: "example", prompt },
    });
    const body = response.json<{ requestId: string; promptSha256: string }>();
    expect(response.statusCode).toBe(201);
    expect(body.promptSha256).toBe(
      createHash("sha256").update(prompt, "utf8").digest("hex"),
    );
    expect(codex.calls).toHaveLength(0);

    const fetched = await app.inject({
      method: "GET",
      url: `/v1/execution-requests/${body.requestId}`,
      headers: authorization(),
    });
    expect(fetched.json()).toMatchObject({
      prompt,
      status: "pending_approval",
    });
  });

  it("rejects an incorrect approval hash", async () => {
    const { app } = await makeHarness();
    const request = await createRequest(app);
    const response = await app.inject({
      method: "POST",
      url: `/v1/execution-requests/${request.requestId}/approve`,
      headers: authorization(),
      payload: { promptSha256: "0".repeat(64) },
    });
    expect(response.statusCode).toBe(409);
    expect(response.json()).toMatchObject({
      error: { code: "PROMPT_HASH_MISMATCH" },
    });
  });

  it("makes repeated approval idempotent", async () => {
    const { app, store } = await makeHarness();
    const request = await createRequest(app);
    const first = await approveRequest(app, request);
    const second = await approveRequest(app, request);
    expect(second.runId).toBe(first.runId);
    await waitForTerminal(async () => store.getRun(first.runId));
  });

  it("rejects approval when a required-clean worktree is dirty", async () => {
    const { app, repositoryPath } = await makeHarness();
    const request = await createRequest(app);
    await writeFile(`${repositoryPath}/untracked.txt`, "dirty\n", "utf8");
    const response = await app.inject({
      method: "POST",
      url: `/v1/execution-requests/${request.requestId}/approve`,
      headers: authorization(),
      payload: { promptSha256: request.promptSha256 },
    });
    expect(response.statusCode).toBe(409);
    expect(response.json()).toMatchObject({
      error: { code: "REPOSITORY_DIRTY" },
    });
  });

  it("allows only one active execution", async () => {
    const codex = new BlockingFakeCodexAdapter();
    const { app, store } = await makeHarness(codex);
    const firstRequest = await createRequest(app, "First prompt");
    const secondRequest = await createRequest(app, "Second prompt");
    const firstRun = await approveRequest(app, firstRequest);
    const response = await app.inject({
      method: "POST",
      url: `/v1/execution-requests/${secondRequest.requestId}/approve`,
      headers: authorization(),
      payload: { promptSha256: secondRequest.promptSha256 },
    });
    expect(response.statusCode).toBe(409);
    expect(response.json()).toMatchObject({
      error: { code: "EXECUTION_BUSY" },
    });
    await new Promise((resolve) => setImmediate(resolve));
    codex.release();
    await waitForTerminal(async () => store.getRun(firstRun.runId));
  });

  it("persists successful final response, usage, and ordered normalized events", async () => {
    const codex = new SuccessfulFakeCodexAdapter();
    const { app, store } = await makeHarness(codex);
    const request = await createRequest(app);
    const { runId } = await approveRequest(app, request);
    const run = await waitForTerminal(async () => store.getRun(runId));
    expect(run).toMatchObject({
      status: "completed",
      codexThreadId: "thread-example",
      finalResponse: "Finished safely.",
      usage: { inputTokens: 10, outputTokens: 5 },
    });
    expect(codex.calls[0]).toMatchObject({
      prompt: "Inspect the repository and report status.",
      networkAccess: false,
    });
    const events = await store.readEvents(runId);
    expect(events.map((event) => event.id)).toEqual(
      events.map((_, index) => index + 1),
    );
    expect(events.map((event) => event.type)).toEqual([
      "run.started",
      "codex.thread_started",
      "codex.command_completed",
      "codex.agent_message_completed",
      "codex.token_usage",
      "codex.final_response",
      "run.completed",
    ]);
  });

  it("persists a safe structured execution failure", async () => {
    const { app, store } = await makeHarness(new FailingFakeCodexAdapter());
    const request = await createRequest(app);
    const { runId } = await approveRequest(app, request);
    const run = await waitForTerminal(async () => store.getRun(runId));
    expect(run).toMatchObject({
      status: "failed",
      error: {
        code: "CODEX_EXECUTION_FAILED",
        message: "Codex execution failed",
      },
    });
    expect(JSON.stringify(run)).not.toContain(
      "private internal failure details",
    );
  });

  it("replays persisted events through SSE and closes after terminal completion", async () => {
    const { app, store } = await makeHarness();
    const request = await createRequest(app);
    const { runId } = await approveRequest(app, request);
    await waitForTerminal(async () => store.getRun(runId));
    const response = await app.inject({
      method: "GET",
      url: `/v1/runs/${runId}/events`,
      headers: authorization(),
    });
    expect(response.statusCode).toBe(200);
    expect(response.headers["content-type"]).toContain("text/event-stream");
    expect(response.body).toContain("event: run.started");
    expect(response.body).toContain("event: run.completed");

    const replay = await app.inject({
      method: "GET",
      url: `/v1/runs/${runId}/events`,
      headers: { ...authorization(), "last-event-id": "6" },
    });
    expect(replay.body).not.toContain("event: run.started");
    expect(replay.body).toContain("event: run.completed");

    const alreadyAcknowledged = await app.inject({
      method: "GET",
      url: `/v1/runs/${runId}/events`,
      headers: { ...authorization(), "last-event-id": "999" },
    });
    expect(alreadyAcknowledged.statusCode).toBe(200);
    expect(alreadyAcknowledged.body).toBe("");
  });

  it("rejects malformed bodies and oversized prompts", async () => {
    const { app } = await makeHarness();
    const malformed = await app.inject({
      method: "POST",
      url: "/v1/execution-requests",
      headers: { ...authorization(), "content-type": "application/json" },
      payload: "{not-json",
    });
    expect(malformed.statusCode).toBe(400);
    expect(malformed.json()).toMatchObject({
      error: { code: "MALFORMED_REQUEST" },
    });

    const oversized = await app.inject({
      method: "POST",
      url: "/v1/execution-requests",
      headers: authorization(),
      payload: {
        repositoryId: "example",
        prompt: "x".repeat(MAX_PROMPT_BYTES + 1),
      },
    });
    expect(oversized.statusCode).toBe(413);
    expect(oversized.json()).toMatchObject({
      error: { code: "PROMPT_TOO_LARGE" },
    });
  });
});
