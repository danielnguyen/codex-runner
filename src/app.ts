import { z } from "zod";
import Fastify, {
  type FastifyInstance,
  type FastifyServerOptions,
} from "fastify";

import { createBearerAuthenticator } from "./auth.js";
import type { CodexAdapter } from "./codex/codex-adapter.js";
import type { RunnerConfig } from "./config.js";
import { RunnerError } from "./errors.js";
import {
  ExecutionService,
  MAX_PROMPT_BYTES,
} from "./executions/execution-service.js";
import type { ExecutionStore } from "./executions/execution-store.js";
import { streamRunEvents } from "./executions/event-stream.js";
import type { RepositoryRegistry } from "./repositories/repository-registry.js";

const idParamsSchema = z.object({
  requestId: z.string().uuid().optional(),
  runId: z.string().uuid().optional(),
});
const createRequestSchema = z
  .object({
    repositoryId: z.string().min(1).max(64),
    prompt: z.string().min(1),
  })
  .strict();
const approveSchema = z
  .object({
    promptSha256: z.string().regex(/^[a-fA-F0-9]{64}$/),
  })
  .strict();

export interface BuildAppOptions {
  config: RunnerConfig;
  repositories: RepositoryRegistry;
  store: ExecutionStore;
  codex: CodexAdapter;
  logger?: FastifyServerOptions["logger"];
}

function parseBody<T>(schema: z.ZodType<T>, body: unknown): T {
  const result = schema.safeParse(body);
  if (!result.success) {
    throw new RunnerError(
      400,
      "INVALID_REQUEST_BODY",
      "Request body is invalid",
    );
  }
  return result.data;
}

function parseId(value: unknown, kind: "requestId" | "runId"): string {
  const result = idParamsSchema.safeParse({ [kind]: value });
  const id = result.success ? result.data[kind] : undefined;
  if (id === undefined) {
    throw new RunnerError(
      400,
      "INVALID_IDENTIFIER",
      "Resource identifier is invalid",
    );
  }
  return id;
}

export async function buildApp(
  options: BuildAppOptions,
): Promise<FastifyInstance> {
  const serverOptions: FastifyServerOptions = {
    bodyLimit: MAX_PROMPT_BYTES + 16 * 1024,
    logger: options.logger ?? {
      level: options.config.logLevel,
      redact: {
        paths: ["req.headers.authorization", "request.headers.authorization"],
        censor: "[REDACTED]",
      },
    },
    requestIdHeader: "x-request-id",
  };
  const app = Fastify(serverOptions);
  const service = new ExecutionService(
    options.store,
    options.repositories,
    options.codex,
    app.log,
  );
  await service.initialize();

  app.get("/health", async () => ({ status: "ok", service: "codex-runner" }));

  app.setNotFoundHandler(async (_request, reply) => {
    return reply.code(404).send({
      error: { code: "ROUTE_NOT_FOUND", message: "Route not found" },
    });
  });

  app.setErrorHandler(async (error, request, reply) => {
    if (error instanceof RunnerError) {
      request.log.warn(
        { code: error.code, statusCode: error.statusCode },
        "Request rejected",
      );
      const response: {
        error: { code: string; message: string; details?: unknown };
      } = {
        error: { code: error.code, message: error.message },
      };
      if (error.details !== undefined) {
        response.error.details = error.details;
      }
      return reply.code(error.statusCode).send(response);
    }
    const fastifyError = error as { code?: string; statusCode?: number };
    if (fastifyError.code === "FST_ERR_CTP_BODY_TOO_LARGE") {
      return reply.code(413).send({
        error: { code: "BODY_TOO_LARGE", message: "Request body is too large" },
      });
    }
    if (fastifyError.statusCode === 400) {
      return reply.code(400).send({
        error: {
          code: "MALFORMED_REQUEST",
          message: "Request could not be parsed",
        },
      });
    }
    request.log.error({ err: error }, "Unhandled request error");
    return reply.code(500).send({
      error: { code: "INTERNAL_ERROR", message: "Internal server error" },
    });
  });

  await app.register(
    async (api) => {
      api.addHook("onRequest", createBearerAuthenticator(options.config.token));

      api.get("/repositories", async () => ({
        repositories: await options.repositories.listSafe(),
      }));

      api.post("/execution-requests", async (request, reply) => {
        const body = parseBody(createRequestSchema, request.body);
        const created = await service.createRequest(
          body.repositoryId,
          body.prompt,
        );
        return reply.code(201).send({
          requestId: created.requestId,
          repositoryId: created.repositoryId,
          promptSha256: created.promptSha256,
          status: created.status,
          createdAt: created.createdAt,
        });
      });

      api.get("/execution-requests/:requestId", async (request) => {
        const requestId = parseId(
          (request.params as Record<string, unknown>)["requestId"],
          "requestId",
        );
        return service.getRequest(requestId);
      });

      api.post(
        "/execution-requests/:requestId/approve",
        async (request, reply) => {
          const requestId = parseId(
            (request.params as Record<string, unknown>)["requestId"],
            "requestId",
          );
          const body = parseBody(approveSchema, request.body);
          const result = await service.approve(requestId, body.promptSha256);
          return reply.code(202).send(result);
        },
      );

      api.get("/runs/:runId", async (request) => {
        const runId = parseId(
          (request.params as Record<string, unknown>)["runId"],
          "runId",
        );
        return service.getRun(runId);
      });

      api.get("/runs/:runId/events", async (request, reply) => {
        const runId = parseId(
          (request.params as Record<string, unknown>)["runId"],
          "runId",
        );
        const rawLastEventId = request.headers["last-event-id"];
        let lastEventId = 0;
        if (typeof rawLastEventId === "string" && rawLastEventId.length > 0) {
          if (!/^\d+$/.test(rawLastEventId)) {
            throw new RunnerError(
              400,
              "INVALID_LAST_EVENT_ID",
              "Last-Event-ID must be a non-negative integer",
            );
          }
          lastEventId = Number(rawLastEventId);
          if (!Number.isSafeInteger(lastEventId)) {
            throw new RunnerError(
              400,
              "INVALID_LAST_EVENT_ID",
              "Last-Event-ID must be a safe non-negative integer",
            );
          }
        }
        await streamRunEvents(service, runId, lastEventId, request, reply);
      });
    },
    { prefix: "/v1" },
  );

  return app;
}
