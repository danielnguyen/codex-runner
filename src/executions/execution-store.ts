import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import {
  chmod,
  mkdir,
  open,
  readFile,
  readdir,
  rename,
  unlink,
} from "node:fs/promises";
import path from "node:path";

import { RunnerError } from "../errors.js";
import type {
  ExecutionRequestRecord,
  ExecutionRequestState,
  RunEvent,
  RunRecord,
} from "./execution-types.js";

function parseJson<T>(content: string, recordName: string): T {
  try {
    return JSON.parse(content) as T;
  } catch {
    throw new RunnerError(
      500,
      "CORRUPT_EXECUTION_LEDGER",
      `Stored ${recordName} is not valid JSON`,
    );
  }
}

export class ExecutionStore {
  private readonly requestsDir: string;
  private readonly requestStatesDir: string;
  private readonly runsDir: string;
  private readonly eventsDir: string;

  constructor(private readonly dataDir: string) {
    this.requestsDir = path.join(dataDir, "requests");
    this.requestStatesDir = path.join(dataDir, "request-states");
    this.runsDir = path.join(dataDir, "runs");
    this.eventsDir = path.join(dataDir, "events");
  }

  async initialize(): Promise<void> {
    const directories = [
      this.dataDir,
      this.requestsDir,
      this.requestStatesDir,
      this.runsDir,
      this.eventsDir,
    ];
    await Promise.all(
      directories.map(async (directory) => {
        await mkdir(directory, { recursive: true, mode: 0o700 });
        await chmod(directory, 0o700);
      }),
    );
  }

  private requestPath(requestId: string): string {
    return path.join(this.requestsDir, `${requestId}.json`);
  }

  private requestStatePath(requestId: string): string {
    return path.join(this.requestStatesDir, `${requestId}.json`);
  }

  private runPath(runId: string): string {
    return path.join(this.runsDir, `${runId}.json`);
  }

  private eventsPath(runId: string): string {
    return path.join(this.eventsDir, `${runId}.jsonl`);
  }

  private async writeExclusive(
    filePath: string,
    value: unknown,
  ): Promise<void> {
    let handle;
    try {
      handle = await open(
        filePath,
        constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
        0o600,
      );
      await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`, "utf8");
      await handle.sync();
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") {
        throw new RunnerError(
          409,
          "IMMUTABLE_RECORD_EXISTS",
          "The immutable execution request already exists",
        );
      }
      throw error;
    } finally {
      await handle?.close();
    }
    await this.syncDirectory(path.dirname(filePath));
  }

  private async writeAtomic(filePath: string, value: unknown): Promise<void> {
    const temporaryPath = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
    let handle;
    try {
      handle = await open(
        temporaryPath,
        constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
        0o600,
      );
      await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`, "utf8");
      await handle.sync();
      await handle.close();
      handle = undefined;
      await rename(temporaryPath, filePath);
      await this.syncDirectory(path.dirname(filePath));
    } finally {
      await handle?.close();
      await unlink(temporaryPath).catch(() => undefined);
    }
  }

  private async syncDirectory(directory: string): Promise<void> {
    const handle = await open(directory, constants.O_RDONLY);
    try {
      await handle.sync();
    } finally {
      await handle.close();
    }
  }

  async createRequest(
    request: ExecutionRequestRecord,
    state: ExecutionRequestState,
  ): Promise<void> {
    await this.writeExclusive(this.requestPath(request.requestId), request);
    try {
      await this.writeExclusive(
        this.requestStatePath(request.requestId),
        state,
      );
    } catch (error) {
      await unlink(this.requestPath(request.requestId)).catch(() => undefined);
      throw error;
    }
  }

  async getRequest(requestId: string): Promise<ExecutionRequestRecord> {
    try {
      return parseJson<ExecutionRequestRecord>(
        await readFile(this.requestPath(requestId), "utf8"),
        "execution request",
      );
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        throw new RunnerError(
          404,
          "REQUEST_NOT_FOUND",
          "Execution request not found",
        );
      }
      throw error;
    }
  }

  async getRequestState(requestId: string): Promise<ExecutionRequestState> {
    try {
      return parseJson<ExecutionRequestState>(
        await readFile(this.requestStatePath(requestId), "utf8"),
        "execution request state",
      );
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        throw new RunnerError(
          404,
          "REQUEST_NOT_FOUND",
          "Execution request not found",
        );
      }
      throw error;
    }
  }

  async updateRequestState(state: ExecutionRequestState): Promise<void> {
    await this.writeAtomic(this.requestStatePath(state.requestId), state);
  }

  async createRun(run: RunRecord): Promise<void> {
    await this.writeExclusive(this.runPath(run.runId), run);
    const handle = await open(
      this.eventsPath(run.runId),
      constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY,
      0o600,
    );
    await handle.sync();
    await handle.close();
    await this.syncDirectory(this.eventsDir);
  }

  async getRun(runId: string): Promise<RunRecord> {
    try {
      return parseJson<RunRecord>(
        await readFile(this.runPath(runId), "utf8"),
        "run",
      );
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        throw new RunnerError(404, "RUN_NOT_FOUND", "Run not found");
      }
      throw error;
    }
  }

  async updateRun(run: RunRecord): Promise<void> {
    await this.writeAtomic(this.runPath(run.runId), run);
  }

  async listRuns(): Promise<RunRecord[]> {
    const names = await readdir(this.runsDir);
    return Promise.all(
      names
        .filter((name) => name.endsWith(".json"))
        .map(async (name) =>
          parseJson<RunRecord>(
            await readFile(path.join(this.runsDir, name), "utf8"),
            "run",
          ),
        ),
    );
  }

  async appendEvent(event: RunEvent): Promise<void> {
    const handle = await open(
      this.eventsPath(event.runId),
      constants.O_APPEND | constants.O_WRONLY,
    );
    try {
      await handle.writeFile(`${JSON.stringify(event)}\n`, "utf8");
      await handle.sync();
    } finally {
      await handle.close();
    }
  }

  async readEvents(runId: string, afterId = 0): Promise<RunEvent[]> {
    await this.getRun(runId);
    const content = await readFile(this.eventsPath(runId), "utf8");
    if (content.length === 0) {
      return [];
    }
    return content
      .split("\n")
      .filter((line) => line.length > 0)
      .map((line) => parseJson<RunEvent>(line, "run event"))
      .filter((event) => event.id > afterId)
      .sort((left, right) => left.id - right.id);
  }
}
