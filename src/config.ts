import path from "node:path";

import { z } from "zod";

import { RunnerError } from "./errors.js";

const weakTokens = new Set([
  "replace_with_random_token",
  "replace_with_random_token_of_at_least_32_random_bytes",
  "changeme",
  "password",
]);

const logLevels = [
  "fatal",
  "error",
  "warn",
  "info",
  "debug",
  "trace",
  "silent",
] as const;

const environmentSchema = z.object({
  HOST: z.string().min(1).default("127.0.0.1"),
  PORT: z.coerce.number().int().min(1).max(65_535).default(8787),
  CODEX_RUNNER_TOKEN: z.string().min(1),
  CODEX_RUNNER_REPOSITORIES_FILE: z.string().min(1),
  CODEX_RUNNER_DATA_DIR: z.string().min(1).optional(),
  CODEX_MODEL: z.string().min(1).optional(),
  LOG_LEVEL: z.enum(logLevels).default("info"),
});

export interface RunnerConfig {
  host: string;
  port: number;
  token: string;
  repositoriesFile: string;
  dataDir: string;
  model?: string;
  logLevel: (typeof logLevels)[number];
}

export function loadConfig(
  environment: NodeJS.ProcessEnv,
  workingDirectory = process.cwd(),
): RunnerConfig {
  const result = environmentSchema.safeParse(environment);
  if (!result.success) {
    throw new RunnerError(
      500,
      "INVALID_CONFIGURATION",
      "Required runner configuration is missing or invalid",
    );
  }

  const values = result.data;
  const tokenBytes = Buffer.byteLength(values.CODEX_RUNNER_TOKEN, "utf8");
  const normalizedToken = values.CODEX_RUNNER_TOKEN.toLowerCase();
  const repeatedCharacter = /^(.)\1+$/u.test(values.CODEX_RUNNER_TOKEN);
  if (
    tokenBytes < 32 ||
    values.CODEX_RUNNER_TOKEN.trim().length < 16 ||
    weakTokens.has(normalizedToken) ||
    repeatedCharacter
  ) {
    throw new RunnerError(
      500,
      "WEAK_RUNNER_TOKEN",
      "CODEX_RUNNER_TOKEN must contain at least 32 bytes of non-placeholder text",
    );
  }

  if (!path.isAbsolute(values.CODEX_RUNNER_REPOSITORIES_FILE)) {
    throw new RunnerError(
      500,
      "INVALID_REPOSITORIES_FILE",
      "CODEX_RUNNER_REPOSITORIES_FILE must be an absolute path",
    );
  }

  const config: RunnerConfig = {
    host: values.HOST,
    port: values.PORT,
    token: values.CODEX_RUNNER_TOKEN,
    repositoriesFile: values.CODEX_RUNNER_REPOSITORIES_FILE,
    dataDir:
      values.CODEX_RUNNER_DATA_DIR ?? path.resolve(workingDirectory, "data"),
    logLevel: values.LOG_LEVEL,
  };
  if (values.CODEX_MODEL !== undefined) {
    config.model = values.CODEX_MODEL;
  }
  return config;
}
