export class RunnerError extends Error {
  readonly statusCode: number;
  readonly code: string;
  readonly details?: Record<string, unknown>;

  constructor(
    statusCode: number,
    code: string,
    message: string,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "RunnerError";
    this.statusCode = statusCode;
    this.code = code;
    if (details !== undefined) {
      this.details = details;
    }
  }
}

export function safeExecutionError(): {
  code: string;
  message: string;
} {
  return {
    code: "CODEX_EXECUTION_FAILED",
    message: "Codex execution failed",
  };
}
