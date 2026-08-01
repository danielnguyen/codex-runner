import path from "node:path";

import { describe, expect, it } from "vitest";

import { loadConfig } from "../src/config.js";

describe("runner configuration", () => {
  it("rejects a missing bearer token", () => {
    expect(() =>
      loadConfig({
        CODEX_RUNNER_REPOSITORIES_FILE: path.resolve("repositories.json"),
      }),
    ).toThrow(/configuration is missing or invalid/i);
  });

  it("rejects a weak bearer token", () => {
    expect(() =>
      loadConfig({
        CODEX_RUNNER_TOKEN: "too-short",
        CODEX_RUNNER_REPOSITORIES_FILE: path.resolve("repositories.json"),
      }),
    ).toThrow(/at least 32 bytes/i);
  });

  it("rejects the documented placeholder token", () => {
    expect(() =>
      loadConfig({
        CODEX_RUNNER_TOKEN:
          "REPLACE_WITH_RANDOM_TOKEN_OF_AT_LEAST_32_RANDOM_BYTES",
        CODEX_RUNNER_REPOSITORIES_FILE: path.resolve("repositories.json"),
      }),
    ).toThrow(/non-placeholder/i);
  });

  it("rejects a repeated-character token", () => {
    expect(() =>
      loadConfig({
        CODEX_RUNNER_TOKEN: "x".repeat(64),
        CODEX_RUNNER_REPOSITORIES_FILE: path.resolve("repositories.json"),
      }),
    ).toThrow(/non-placeholder/i);
  });
});
