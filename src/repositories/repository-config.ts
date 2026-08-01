import { z } from "zod";

export const repositoryEntrySchema = z.object({
  id: z
    .string()
    .min(1)
    .max(64)
    .regex(/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/),
  displayName: z.string().min(1).max(200),
  path: z.string().min(1),
  requireCleanWorktree: z.boolean(),
  networkAccess: z.boolean(),
});

export const repositoriesFileSchema = z.object({
  repositories: z.array(repositoryEntrySchema).min(1),
});

export type RepositoryConfigEntry = z.infer<typeof repositoryEntrySchema>;
