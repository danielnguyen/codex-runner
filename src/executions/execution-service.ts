import { createHash, randomUUID, timingSafeEqual } from "node:crypto";

import type {
  CodexAdapter,
  CodexProgressEvent,
} from "../codex/codex-adapter.js";
import { RunnerError, safeExecutionError } from "../errors.js";
import type { RepositoryRegistry } from "../repositories/repository-registry.js";
import type { ExecutionStore } from "./execution-store.js";
import type {
  ExecutionRequestRecord,
  ExecutionRequestState,
  ExecutionRequestView,
  RunEvent,
  RunRecord,
} from "./execution-types.js";

export const MAX_PROMPT_BYTES = 64 * 1024;

interface ServiceLogger {
  info(attributes: Record<string, unknown>, message: string): void;
  error(attributes: Record<string, unknown>, message: string): void;
}

const noOpLogger: ServiceLogger = {
  info: () => undefined,
  error: () => undefined,
};

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function hashesMatch(expected: string, supplied: string): boolean {
  if (!/^[a-f0-9]{64}$/i.test(supplied)) {
    return false;
  }
  return timingSafeEqual(
    Buffer.from(expected, "hex"),
    Buffer.from(supplied, "hex"),
  );
}

export class ExecutionService {
  private activeRunId: string | undefined;
  private lockTail: Promise<void> = Promise.resolve();

  constructor(
    private readonly store: ExecutionStore,
    private readonly repositories: RepositoryRegistry,
    private readonly codex: CodexAdapter,
    private readonly logger: ServiceLogger = noOpLogger,
  ) {}

  async initialize(): Promise<void> {
    await this.store.initialize();
    const runs = await this.store.listRuns();
    for (const run of runs) {
      const existingEvents = await this.store.readEvents(run.runId);
      if (run.status !== "queued" && run.status !== "running") {
        const terminalType = `run.${run.status}`;
        if (!existingEvents.some((event) => event.type === terminalType)) {
          let nextEventId = (existingEvents.at(-1)?.id ?? 0) + 1;
          if (
            run.status === "completed" &&
            run.finalResponse !== undefined &&
            !existingEvents.some(
              (event) => event.type === "codex.final_response",
            )
          ) {
            await this.store.appendEvent({
              id: nextEventId++,
              runId: run.runId,
              type: "codex.final_response",
              timestamp: run.completedAt ?? new Date().toISOString(),
              data: { response: run.finalResponse },
            });
          }
          await this.store.appendEvent({
            id: nextEventId,
            runId: run.runId,
            type: terminalType,
            timestamp: run.completedAt ?? new Date().toISOString(),
            data: run.error === undefined ? {} : { code: run.error.code },
          });
          this.logger.info(
            { runId: run.runId, status: run.status },
            "Repaired missing terminal event",
          );
        }
        continue;
      }
      const timestamp = new Date().toISOString();
      const interrupted: RunRecord = {
        ...run,
        status: "interrupted",
        completedAt: timestamp,
        error: {
          code: "RUNNER_RESTARTED",
          message: "The runner restarted before the execution completed",
        },
      };
      await this.store.updateRun(interrupted);
      await this.store.appendEvent({
        id: (existingEvents.at(-1)?.id ?? 0) + 1,
        runId: run.runId,
        type: "run.interrupted",
        timestamp,
        data: { code: "RUNNER_RESTARTED" },
      });
      const state = await this.store.getRequestState(run.requestId);
      await this.store.updateRequestState({
        ...state,
        status: "interrupted",
        updatedAt: timestamp,
        runId: run.runId,
      });
      this.logger.info(
        { runId: run.runId, requestId: run.requestId, status: "interrupted" },
        "Recovered abandoned run",
      );
    }
  }

  async createRequest(
    repositoryId: string,
    prompt: string,
  ): Promise<ExecutionRequestView> {
    this.repositories.get(repositoryId);
    const promptBytes = Buffer.byteLength(prompt, "utf8");
    if (promptBytes === 0) {
      throw new RunnerError(400, "EMPTY_PROMPT", "Prompt must not be empty");
    }
    if (promptBytes > MAX_PROMPT_BYTES) {
      throw new RunnerError(
        413,
        "PROMPT_TOO_LARGE",
        `Prompt must not exceed ${MAX_PROMPT_BYTES} UTF-8 bytes`,
      );
    }

    const requestId = randomUUID();
    const createdAt = new Date().toISOString();
    const request: ExecutionRequestRecord = {
      requestId,
      repositoryId,
      prompt,
      promptSha256: sha256(prompt),
      createdAt,
    };
    const state: ExecutionRequestState = {
      requestId,
      status: "pending_approval",
      updatedAt: createdAt,
    };
    await this.store.createRequest(request, state);
    this.logger.info(
      { requestId, repositoryId, status: state.status },
      "Execution request created",
    );
    return { ...request, status: state.status };
  }

  async getRequest(requestId: string): Promise<ExecutionRequestView> {
    const [request, state] = await Promise.all([
      this.store.getRequest(requestId),
      this.store.getRequestState(requestId),
    ]);
    const view: ExecutionRequestView = { ...request, status: state.status };
    if (state.runId !== undefined) {
      view.runId = state.runId;
    }
    return view;
  }

  async approve(
    requestId: string,
    suppliedPromptSha256: string,
  ): Promise<{ requestId: string; runId: string; status: string }> {
    return this.withLock(async () => {
      const [request, state] = await Promise.all([
        this.store.getRequest(requestId),
        this.store.getRequestState(requestId),
      ]);
      if (!hashesMatch(request.promptSha256, suppliedPromptSha256)) {
        throw new RunnerError(
          409,
          "PROMPT_HASH_MISMATCH",
          "The approval hash does not match the immutable prompt",
        );
      }

      if (
        state.runId !== undefined &&
        ["queued", "running", "completed"].includes(state.status)
      ) {
        return {
          requestId,
          runId: state.runId,
          status: state.status,
        };
      }
      if (state.status !== "pending_approval") {
        throw new RunnerError(
          409,
          "REQUEST_NOT_APPROVABLE",
          "The execution request is in a terminal state",
        );
      }
      if (this.activeRunId !== undefined) {
        throw new RunnerError(
          409,
          "EXECUTION_BUSY",
          "Another execution is already active",
        );
      }

      const repository = await this.repositories.revalidate(
        request.repositoryId,
      );
      await this.repositories.requireClean(repository);

      const runId = randomUUID();
      const createdAt = new Date().toISOString();
      const run: RunRecord = {
        runId,
        requestId,
        repositoryId: request.repositoryId,
        status: "queued",
        createdAt,
      };
      await this.store.createRun(run);
      await this.store.updateRequestState({
        requestId,
        runId,
        status: "queued",
        updatedAt: createdAt,
      });
      this.activeRunId = runId;
      this.logger.info(
        {
          runId,
          requestId,
          repositoryId: request.repositoryId,
          status: "queued",
        },
        "Execution approved",
      );
      setImmediate(() => {
        void this.executeRun(run, request);
      });
      return { requestId, runId, status: "queued" };
    });
  }

  getRun(runId: string): Promise<RunRecord> {
    return this.store.getRun(runId);
  }

  getEvents(runId: string, afterId = 0): Promise<RunEvent[]> {
    return this.store.readEvents(runId, afterId);
  }

  private async executeRun(
    initialRun: RunRecord,
    request: ExecutionRequestRecord,
  ): Promise<void> {
    let run = initialRun;
    let nextEventId = 1;
    let finalResponse = "";
    try {
      const repository = await this.repositories.revalidate(
        request.repositoryId,
      );
      await this.repositories.requireClean(repository);
      const startedAt = new Date().toISOString();
      run = { ...run, status: "running", startedAt };
      await this.store.updateRun(run);
      await this.store.updateRequestState({
        requestId: request.requestId,
        runId: run.runId,
        status: "running",
        updatedAt: startedAt,
      });
      await this.appendEvent(run.runId, nextEventId++, "run.started", {});
      this.logger.info(
        { runId: run.runId, requestId: request.requestId, status: "running" },
        "Execution started",
      );

      for await (const event of this.codex.execute({
        prompt: request.prompt,
        workingDirectory: repository.canonicalPath,
        networkAccess: repository.networkAccess,
      })) {
        const normalized = this.normalizeProgress(event);
        if (event.type === "thread_started") {
          run = { ...run, codexThreadId: event.threadId };
          await this.store.updateRun(run);
        } else if (event.type === "agent_message_completed") {
          finalResponse = event.text;
        } else if (event.type === "token_usage") {
          run = { ...run, usage: event.usage };
          await this.store.updateRun(run);
        }
        await this.appendEvent(
          run.runId,
          nextEventId++,
          normalized.type,
          normalized.data,
        );
      }

      const completedAt = new Date().toISOString();
      run = { ...run, status: "completed", completedAt, finalResponse };
      await this.store.updateRun(run);
      await this.appendEvent(run.runId, nextEventId++, "codex.final_response", {
        response: finalResponse,
      });
      await this.appendEvent(run.runId, nextEventId, "run.completed", {});
      await this.store.updateRequestState({
        requestId: request.requestId,
        runId: run.runId,
        status: "completed",
        updatedAt: completedAt,
      });
      this.logger.info(
        { runId: run.runId, requestId: request.requestId, status: "completed" },
        "Execution completed",
      );
    } catch (error) {
      const completedAt = new Date().toISOString();
      const safeError = safeExecutionError();
      run = { ...run, status: "failed", completedAt, error: safeError };
      await this.store.updateRun(run).catch(() => undefined);
      await this.appendEvent(run.runId, nextEventId, "run.failed", {
        code: safeError.code,
      }).catch(() => undefined);
      await this.store
        .updateRequestState({
          requestId: request.requestId,
          runId: run.runId,
          status: "failed",
          updatedAt: completedAt,
        })
        .catch(() => undefined);
      this.logger.error(
        {
          runId: run.runId,
          requestId: request.requestId,
          status: "failed",
          err: error,
        },
        "Execution failed",
      );
    } finally {
      await this.withLock(async () => {
        if (this.activeRunId === run.runId) {
          this.activeRunId = undefined;
        }
      });
    }
  }

  private normalizeProgress(event: CodexProgressEvent): {
    type: string;
    data: Record<string, unknown>;
  } {
    switch (event.type) {
      case "thread_started":
        return {
          type: "codex.thread_started",
          data: { threadId: event.threadId },
        };
      case "agent_message_completed":
        return {
          type: "codex.agent_message_completed",
          data: { text: event.text },
        };
      case "command_completed":
        return {
          type: "codex.command_completed",
          data: {
            itemId: event.itemId,
            status: event.status,
            ...(event.exitCode === undefined
              ? {}
              : { exitCode: event.exitCode }),
          },
        };
      case "file_change_completed":
        return {
          type: "codex.file_change_completed",
          data: {
            itemId: event.itemId,
            status: event.status,
            changeCount: event.changeCount,
            changeKinds: event.changeKinds,
          },
        };
      case "tool_completed":
        return {
          type: "codex.tool_completed",
          data: {
            itemId: event.itemId,
            toolType: event.toolType,
            status: event.status,
          },
        };
      case "token_usage":
        return { type: "codex.token_usage", data: { usage: event.usage } };
    }
  }

  private async appendEvent(
    runId: string,
    id: number,
    type: string,
    data: Record<string, unknown>,
  ): Promise<void> {
    await this.store.appendEvent({
      id,
      runId,
      type,
      timestamp: new Date().toISOString(),
      data,
    });
  }

  private async withLock<T>(operation: () => Promise<T>): Promise<T> {
    let release: (() => void) | undefined;
    const previous = this.lockTail;
    this.lockTail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await operation();
    } finally {
      release?.();
    }
  }
}
