import { rm } from "node:fs/promises";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { ExecutionService } from "../src/executions/execution-service.js";
import { ExecutionStore } from "../src/executions/execution-store.js";
import type {
  ExecutionRequestRecord,
  ExecutionRequestState,
  RunRecord,
} from "../src/executions/execution-types.js";
import { RepositoryRegistry } from "../src/repositories/repository-registry.js";
import {
  SuccessfulFakeCodexAdapter,
  makeGitRepository,
  makeTemporaryDirectory,
  writeRepositoryConfig,
} from "./helpers.js";

const cleanup: string[] = [];
afterEach(async () => {
  await Promise.all(
    cleanup
      .splice(0)
      .map(async (directory) => rm(directory, { recursive: true })),
  );
});

describe("execution ledger", () => {
  it("does not overwrite immutable request content", async () => {
    const root = await makeTemporaryDirectory("codex-runner-store-");
    cleanup.push(root);
    const store = new ExecutionStore(path.join(root, "data"));
    await store.initialize();
    const request: ExecutionRequestRecord = {
      requestId: "00000000-0000-4000-8000-000000000001",
      repositoryId: "example",
      prompt: "Original prompt",
      promptSha256: "a".repeat(64),
      createdAt: new Date().toISOString(),
    };
    const state: ExecutionRequestState = {
      requestId: request.requestId,
      status: "pending_approval",
      updatedAt: request.createdAt,
    };
    await store.createRequest(request, state);
    await expect(
      store.createRequest({ ...request, prompt: "Changed prompt" }, state),
    ).rejects.toMatchObject({ code: "IMMUTABLE_RECORD_EXISTS" });
    await expect(store.getRequest(request.requestId)).resolves.toMatchObject({
      prompt: "Original prompt",
    });
  });

  it("marks queued or running persisted runs interrupted on startup", async () => {
    const root = await makeTemporaryDirectory("codex-runner-recovery-");
    cleanup.push(root);
    const repository = await makeGitRepository(root);
    const configuration = await writeRepositoryConfig(root, [
      { id: "example", path: repository },
    ]);
    const registry = await RepositoryRegistry.load(configuration);
    const store = new ExecutionStore(path.join(root, "data"));
    await store.initialize();
    const requestId = "00000000-0000-4000-8000-000000000002";
    const runId = "00000000-0000-4000-8000-000000000003";
    const now = new Date().toISOString();
    await store.createRequest(
      {
        requestId,
        repositoryId: "example",
        prompt: "Prompt",
        promptSha256: "b".repeat(64),
        createdAt: now,
      },
      { requestId, runId, status: "running", updatedAt: now },
    );
    const run: RunRecord = {
      runId,
      requestId,
      repositoryId: "example",
      status: "running",
      createdAt: now,
      startedAt: now,
    };
    await store.createRun(run);
    await store.appendEvent({
      id: 1,
      runId,
      type: "run.started",
      timestamp: now,
      data: {},
    });

    const service = new ExecutionService(
      store,
      registry,
      new SuccessfulFakeCodexAdapter(),
    );
    await service.initialize();
    await expect(store.getRun(runId)).resolves.toMatchObject({
      status: "interrupted",
      error: { code: "RUNNER_RESTARTED" },
    });
    await expect(store.getRequestState(requestId)).resolves.toMatchObject({
      status: "interrupted",
    });
    expect((await store.readEvents(runId)).at(-1)?.type).toBe(
      "run.interrupted",
    );
  });

  it("repairs a missing persisted terminal event on startup", async () => {
    const root = await makeTemporaryDirectory("codex-runner-repair-");
    cleanup.push(root);
    const repository = await makeGitRepository(root);
    const configuration = await writeRepositoryConfig(root, [
      { id: "example", path: repository },
    ]);
    const registry = await RepositoryRegistry.load(configuration);
    const store = new ExecutionStore(path.join(root, "data"));
    await store.initialize();
    const requestId = "00000000-0000-4000-8000-000000000004";
    const runId = "00000000-0000-4000-8000-000000000005";
    const now = new Date().toISOString();
    await store.createRequest(
      {
        requestId,
        repositoryId: "example",
        prompt: "Prompt",
        promptSha256: "c".repeat(64),
        createdAt: now,
      },
      { requestId, runId, status: "completed", updatedAt: now },
    );
    await store.createRun({
      runId,
      requestId,
      repositoryId: "example",
      status: "completed",
      createdAt: now,
      startedAt: now,
      completedAt: now,
      finalResponse: "Completed response",
    });

    const service = new ExecutionService(
      store,
      registry,
      new SuccessfulFakeCodexAdapter(),
    );
    await service.initialize();
    expect((await store.readEvents(runId)).map((event) => event.type)).toEqual([
      "codex.final_response",
      "run.completed",
    ]);
  });
});
