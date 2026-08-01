import { execFile as execFileCallback } from "node:child_process";
import { readFile, realpath } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

import { RunnerError } from "../errors.js";
import {
  repositoriesFileSchema,
  type RepositoryConfigEntry,
} from "./repository-config.js";

const execFile = promisify(execFileCallback);

export interface RegisteredRepository {
  id: string;
  displayName: string;
  canonicalPath: string;
  requireCleanWorktree: boolean;
  networkAccess: boolean;
}

export interface SafeRepositoryMetadata {
  id: string;
  displayName: string;
  available: boolean;
  requireCleanWorktree: boolean;
  networkAccess: boolean;
}

async function runGit(repositoryPath: string, args: string[]): Promise<string> {
  try {
    const { stdout } = await execFile("git", ["-C", repositoryPath, ...args], {
      encoding: "utf8",
      timeout: 10_000,
      maxBuffer: 1024 * 1024,
    });
    return stdout;
  } catch {
    throw new RunnerError(
      500,
      "INVALID_REPOSITORY",
      "A configured repository could not be validated",
    );
  }
}

async function canonicalize(
  entry: RepositoryConfigEntry,
): Promise<RegisteredRepository> {
  if (!path.isAbsolute(entry.path)) {
    throw new RunnerError(
      500,
      "REPOSITORY_PATH_NOT_ABSOLUTE",
      `Repository '${entry.id}' must use an absolute path`,
    );
  }

  let canonicalPath: string;
  try {
    canonicalPath = await realpath(entry.path);
  } catch {
    throw new RunnerError(
      500,
      "INVALID_REPOSITORY",
      `Repository '${entry.id}' could not be resolved`,
    );
  }

  const topLevelOutput = await runGit(canonicalPath, [
    "rev-parse",
    "--show-toplevel",
  ]);
  let topLevel: string;
  try {
    topLevel = await realpath(topLevelOutput.trim());
  } catch {
    throw new RunnerError(
      500,
      "INVALID_REPOSITORY",
      `Repository '${entry.id}' has an invalid Git toplevel`,
    );
  }
  if (topLevel !== canonicalPath) {
    throw new RunnerError(
      500,
      "REPOSITORY_TOPLEVEL_MISMATCH",
      `Repository '${entry.id}' must identify the exact Git toplevel`,
    );
  }

  return {
    id: entry.id,
    displayName: entry.displayName,
    canonicalPath,
    requireCleanWorktree: entry.requireCleanWorktree,
    networkAccess: entry.networkAccess,
  };
}

export class RepositoryRegistry {
  private readonly repositories: Map<string, RegisteredRepository>;

  private constructor(repositories: Map<string, RegisteredRepository>) {
    this.repositories = repositories;
  }

  static async load(configurationPath: string): Promise<RepositoryRegistry> {
    let parsed: unknown;
    try {
      parsed = JSON.parse(await readFile(configurationPath, "utf8")) as unknown;
    } catch {
      throw new RunnerError(
        500,
        "INVALID_REPOSITORY_CONFIGURATION",
        "Repository configuration could not be read as JSON",
      );
    }
    const result = repositoriesFileSchema.safeParse(parsed);
    if (!result.success) {
      throw new RunnerError(
        500,
        "INVALID_REPOSITORY_CONFIGURATION",
        "Repository configuration does not match the required schema",
      );
    }

    const repositories = new Map<string, RegisteredRepository>();
    const paths = new Set<string>();
    for (const entry of result.data.repositories) {
      if (repositories.has(entry.id)) {
        throw new RunnerError(
          500,
          "DUPLICATE_REPOSITORY_ID",
          `Duplicate repository ID '${entry.id}'`,
        );
      }
      const repository = await canonicalize(entry);
      if (paths.has(repository.canonicalPath)) {
        throw new RunnerError(
          500,
          "DUPLICATE_REPOSITORY_PATH",
          "Repository paths must be unique after canonicalization",
        );
      }
      repositories.set(repository.id, repository);
      paths.add(repository.canonicalPath);
    }
    return new RepositoryRegistry(repositories);
  }

  get(repositoryId: string): RegisteredRepository {
    const repository = this.repositories.get(repositoryId);
    if (repository === undefined) {
      throw new RunnerError(
        404,
        "REPOSITORY_NOT_FOUND",
        "Repository not found",
      );
    }
    return repository;
  }

  async revalidate(repositoryId: string): Promise<RegisteredRepository> {
    const configured = this.get(repositoryId);
    return canonicalize({
      id: configured.id,
      displayName: configured.displayName,
      path: configured.canonicalPath,
      requireCleanWorktree: configured.requireCleanWorktree,
      networkAccess: configured.networkAccess,
    });
  }

  async requireClean(repository: RegisteredRepository): Promise<void> {
    if (!repository.requireCleanWorktree) {
      return;
    }
    const status = await runGit(repository.canonicalPath, [
      "status",
      "--porcelain",
      "--untracked-files=normal",
    ]);
    if (status.length > 0) {
      throw new RunnerError(
        409,
        "REPOSITORY_DIRTY",
        "The repository worktree must be clean before execution",
      );
    }
  }

  async listSafe(): Promise<SafeRepositoryMetadata[]> {
    return Promise.all(
      [...this.repositories.values()].map(async (repository) => {
        let available = true;
        try {
          await this.revalidate(repository.id);
        } catch {
          available = false;
        }
        return {
          id: repository.id,
          displayName: repository.displayName,
          available,
          requireCleanWorktree: repository.requireCleanWorktree,
          networkAccess: repository.networkAccess,
        };
      }),
    );
  }
}
