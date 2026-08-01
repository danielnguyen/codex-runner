import { createHash, timingSafeEqual } from "node:crypto";

import type { FastifyReply, FastifyRequest } from "fastify";

import { RunnerError } from "./errors.js";

function digest(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}

export function createBearerAuthenticator(expectedToken: string) {
  const expectedDigest = digest(expectedToken);

  return async function authenticate(
    request: FastifyRequest,
    _reply: FastifyReply,
  ): Promise<void> {
    const authorization = request.headers.authorization;
    if (
      typeof authorization !== "string" ||
      !authorization.startsWith("Bearer ")
    ) {
      throw new RunnerError(
        401,
        "UNAUTHORIZED",
        "A valid bearer token is required",
      );
    }

    const suppliedToken = authorization.slice("Bearer ".length);
    const suppliedDigest = digest(suppliedToken);
    if (!timingSafeEqual(expectedDigest, suppliedDigest)) {
      throw new RunnerError(
        401,
        "UNAUTHORIZED",
        "A valid bearer token is required",
      );
    }
  };
}
