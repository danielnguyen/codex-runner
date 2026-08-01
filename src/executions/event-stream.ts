import type { FastifyReply, FastifyRequest } from "fastify";

import type { ExecutionService } from "./execution-service.js";

const TERMINAL_EVENTS = new Set([
  "run.completed",
  "run.failed",
  "run.interrupted",
]);
const POLL_INTERVAL_MS = 250;
const KEEPALIVE_INTERVAL_MS = 15_000;

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export async function streamRunEvents(
  service: ExecutionService,
  runId: string,
  lastEventId: number,
  request: FastifyRequest,
  reply: FastifyReply,
): Promise<void> {
  await service.getRun(runId);

  reply.hijack();
  reply.raw.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });
  reply.raw.flushHeaders();

  let closed = false;
  let cursor = lastEventId;
  let lastWrite = Date.now();
  request.raw.on("close", () => {
    closed = true;
  });

  while (!closed) {
    const events = await service.getEvents(runId, cursor);
    let terminal = false;
    for (const event of events) {
      if (closed) {
        break;
      }
      reply.raw.write(`id: ${event.id}\n`);
      reply.raw.write(`event: ${event.type}\n`);
      reply.raw.write(`data: ${JSON.stringify(event)}\n\n`);
      cursor = event.id;
      lastWrite = Date.now();
      if (TERMINAL_EVENTS.has(event.type)) {
        terminal = true;
      }
    }
    if (terminal) {
      reply.raw.end();
      return;
    }
    if (events.length === 0) {
      const run = await service.getRun(runId);
      if (["completed", "failed", "interrupted"].includes(run.status)) {
        const persisted = await service.getEvents(runId, 0);
        const terminalEvent = persisted.find((event) =>
          TERMINAL_EVENTS.has(event.type),
        );
        if (terminalEvent !== undefined && terminalEvent.id <= cursor) {
          reply.raw.end();
          return;
        }
      }
    }
    if (Date.now() - lastWrite >= KEEPALIVE_INTERVAL_MS) {
      reply.raw.write(": keepalive\n\n");
      lastWrite = Date.now();
    }
    await wait(POLL_INTERVAL_MS);
  }
}
