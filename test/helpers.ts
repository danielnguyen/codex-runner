import { execFile as execFileCallback } from "node:child_process";
import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import type {
  CodexAdapter,
  CodexExecutionInput,
  CodexProgressEvent,
} from "../src/codex/codex-adapter.js";
import type { RunnerConfig } from "../src/config.js";
import type { RunRecord } from "../src/executions/execution-types.js";

const execFile = promisify(execFileCallback);

export const TEST_TOKEN = "test-token-".padEnd(48, "x");

export async function makeTemporaryDirectory(prefix: string): Promise<string> {
  return mkdtemp(path.join(os.tmpdir(), prefix));
}

export async function makeGitRepository(
  root: string,
  name = "repository",
): Promise<string> {
  const repositoryPath = path.join(root, name);
  await execFile("git", ["init", "-q", repositoryPath]);
  await execFile("git", [
    "-C",
    repositoryPath,
    "config",
    "user.name",
    "operator",
  ]);
  await execFile("git", [
    "-C",
    repositoryPath,
    "config",
    "user.email",
    "user@example.invalid",
  ]);
  return repositoryPath;
}

export async function writeRepositoryConfig(
  root: string,
  repositories: Array<{
    id: string;
    displayName?: string;
    path: string;
    requireCleanWorktree?: boolean;
    networkAccess?: boolean;
  }>,
): Promise<string> {
  const configurationPath = path.join(root, "repositories.json");
  await writeFile(
    configurationPath,
    `${JSON.stringify(
      {
        repositories: repositories.map((repository) => ({
          displayName: "Example Repository",
          requireCleanWorktree: true,
          networkAccess: false,
          ...repository,
        })),
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
  return configurationPath;
}

export function makeConfig(
  root: string,
  repositoriesFile: string,
): RunnerConfig {
  return {
    host: "127.0.0.1",
    port: 8787,
    token: TEST_TOKEN,
    repositoriesFile,
    dataDir: path.join(root, "data"),
    logLevel: "silent",
  };
}

export class SuccessfulFakeCodexAdapter implements CodexAdapter {
  readonly calls: CodexExecutionInput[] = [];

  async *execute(
    input: CodexExecutionInput,
  ): AsyncIterable<CodexProgressEvent> {
    this.calls.push(input);
    yield { type: "thread_started", threadId: "thread-example" };
    yield {
      type: "command_completed",
      itemId: "command-1",
      status: "completed",
      exitCode: 0,
    };
    yield { type: "agent_message_completed", text: "Finished safely." };
    yield {
      type: "token_usage",
      usage: {
        inputTokens: 10,
        cachedInputTokens: 2,
        cacheWriteInputTokens: 0,
        outputTokens: 5,
        reasoningOutputTokens: 1,
      },
    };
  }
}

export class FailingFakeCodexAdapter implements CodexAdapter {
  async *execute(
    _input: CodexExecutionInput,
  ): AsyncIterable<CodexProgressEvent> {
    yield { type: "thread_started", threadId: "thread-failure" };
    throw new Error("private internal failure details");
  }
}

export class BlockingFakeCodexAdapter implements CodexAdapter {
  readonly calls: CodexExecutionInput[] = [];
  private releaseExecution: (() => void) | undefined;
  private released = false;

  async *execute(
    input: CodexExecutionInput,
  ): AsyncIterable<CodexProgressEvent> {
    this.calls.push(input);
    yield { type: "thread_started", threadId: "thread-blocked" };
    await new Promise<void>((resolve) => {
      if (this.released) {
        resolve();
      } else {
        this.releaseExecution = resolve;
      }
    });
    yield { type: "agent_message_completed", text: "Released." };
  }

  release(): void {
    this.released = true;
    this.releaseExecution?.();
  }
}

export async function waitForTerminal(
  getRun: () => Promise<RunRecord>,
  timeoutMilliseconds = 5_000,
): Promise<RunRecord> {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    const run = await getRun();
    if (["completed", "failed", "interrupted"].includes(run.status)) {
      return run;
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("Run did not reach a terminal state before the test timeout");
}
