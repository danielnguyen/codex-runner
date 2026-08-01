import { mkdir, rm } from "node:fs/promises";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { RepositoryRegistry } from "../src/repositories/repository-registry.js";
import {
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

async function newRoot(): Promise<string> {
  const root = await makeTemporaryDirectory("codex-runner-registry-");
  cleanup.push(root);
  return root;
}

describe("repository registry", () => {
  it("rejects relative paths", async () => {
    const root = await newRoot();
    const file = await writeRepositoryConfig(root, [
      { id: "example", path: "./relative" },
    ]);
    await expect(RepositoryRegistry.load(file)).rejects.toMatchObject({
      code: "REPOSITORY_PATH_NOT_ABSOLUTE",
    });
  });

  it("rejects duplicate IDs", async () => {
    const root = await newRoot();
    const first = await makeGitRepository(root, "first");
    const second = await makeGitRepository(root, "second");
    const file = await writeRepositoryConfig(root, [
      { id: "example", path: first },
      { id: "example", path: second },
    ]);
    await expect(RepositoryRegistry.load(file)).rejects.toMatchObject({
      code: "DUPLICATE_REPOSITORY_ID",
    });
  });

  it("rejects duplicate canonical paths", async () => {
    const root = await newRoot();
    const repository = await makeGitRepository(root);
    const file = await writeRepositoryConfig(root, [
      { id: "first", path: repository },
      { id: "second", path: repository },
    ]);
    await expect(RepositoryRegistry.load(file)).rejects.toMatchObject({
      code: "DUPLICATE_REPOSITORY_PATH",
    });
  });

  it("rejects non-Git directories", async () => {
    const root = await newRoot();
    const directory = path.join(root, "not-git");
    await mkdir(directory);
    const file = await writeRepositoryConfig(root, [
      { id: "example", path: directory },
    ]);
    await expect(RepositoryRegistry.load(file)).rejects.toMatchObject({
      code: "INVALID_REPOSITORY",
    });
  });

  it("rejects a directory whose Git toplevel is a parent", async () => {
    const root = await newRoot();
    const repository = await makeGitRepository(root);
    const child = path.join(repository, "child");
    await mkdir(child);
    const file = await writeRepositoryConfig(root, [
      { id: "example", path: child },
    ]);
    await expect(RepositoryRegistry.load(file)).rejects.toMatchObject({
      code: "REPOSITORY_TOPLEVEL_MISMATCH",
    });
  });
});
