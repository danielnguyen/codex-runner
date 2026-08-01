export const REQUEST_STATUSES = [
  "pending_approval",
  "queued",
  "running",
  "completed",
  "failed",
  "interrupted",
] as const;
export type RequestStatus = (typeof REQUEST_STATUSES)[number];

export const RUN_STATUSES = [
  "queued",
  "running",
  "completed",
  "failed",
  "interrupted",
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export interface ExecutionRequestRecord {
  requestId: string;
  repositoryId: string;
  prompt: string;
  promptSha256: string;
  createdAt: string;
}

export interface ExecutionRequestState {
  requestId: string;
  status: RequestStatus;
  updatedAt: string;
  runId?: string;
}

export interface TokenUsage {
  inputTokens: number;
  cachedInputTokens: number;
  cacheWriteInputTokens: number;
  outputTokens: number;
  reasoningOutputTokens: number;
}

export interface SafeRunError {
  code: string;
  message: string;
}

export interface RunRecord {
  runId: string;
  requestId: string;
  repositoryId: string;
  status: RunStatus;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
  codexThreadId?: string;
  finalResponse?: string;
  usage?: TokenUsage;
  error?: SafeRunError;
}

export interface RunEvent {
  id: number;
  runId: string;
  type: string;
  timestamp: string;
  data: Record<string, unknown>;
}

export interface ExecutionRequestView extends ExecutionRequestRecord {
  status: RequestStatus;
  runId?: string;
}
