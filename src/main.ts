import { buildApp } from "./app.js";
import { OpenAICodexAdapter } from "./codex/openai-codex-adapter.js";
import { loadConfig } from "./config.js";
import { ExecutionStore } from "./executions/execution-store.js";
import { RepositoryRegistry } from "./repositories/repository-registry.js";

async function main(): Promise<void> {
  const config = loadConfig(process.env);
  const repositories = await RepositoryRegistry.load(config.repositoriesFile);
  const store = new ExecutionStore(config.dataDir);
  const codex = new OpenAICodexAdapter(config.model);
  const app = await buildApp({ config, repositories, store, codex });

  const close = async (signal: string): Promise<void> => {
    app.log.info({ signal }, "Shutting down");
    await app.close();
  };
  process.once("SIGINT", () => void close("SIGINT"));
  process.once("SIGTERM", () => void close("SIGTERM"));

  await app.listen({ host: config.host, port: config.port });
}

main().catch((error: unknown) => {
  process.stderr.write(`codex-runner failed to start: ${String(error)}\n`);
  process.exitCode = 1;
});
