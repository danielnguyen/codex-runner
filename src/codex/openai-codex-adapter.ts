import { Codex, type ThreadEvent, type Usage } from "@openai/codex-sdk";

import type { TokenUsage } from "../executions/execution-types.js";
import type {
  CodexAdapter,
  CodexExecutionInput,
  CodexProgressEvent,
} from "./codex-adapter.js";

function normalizeUsage(usage: Usage): TokenUsage {
  return {
    inputTokens: usage.input_tokens,
    cachedInputTokens: usage.cached_input_tokens,
    cacheWriteInputTokens: usage.cache_write_input_tokens,
    outputTokens: usage.output_tokens,
    reasoningOutputTokens: usage.reasoning_output_tokens,
  };
}

function normalizeEvent(event: ThreadEvent): CodexProgressEvent | undefined {
  if (event.type === "thread.started") {
    return { type: "thread_started", threadId: event.thread_id };
  }
  if (event.type === "turn.completed") {
    return { type: "token_usage", usage: normalizeUsage(event.usage) };
  }
  if (event.type === "turn.failed") {
    throw new Error(event.error.message);
  }
  if (event.type === "error") {
    throw new Error(event.message);
  }
  if (
    event.type === "turn.started" ||
    event.type === "item.started" ||
    event.type === "item.updated"
  ) {
    return undefined;
  }
  if (event.type !== "item.completed") {
    throw new Error("Codex emitted an unsupported event type");
  }

  const item = event.item;
  switch (item.type) {
    case "agent_message":
      return { type: "agent_message_completed", text: item.text };
    case "command_execution": {
      const result: CodexProgressEvent = {
        type: "command_completed",
        itemId: item.id,
        status: item.status,
      };
      if (item.exit_code !== undefined) {
        result.exitCode = item.exit_code;
      }
      return result;
    }
    case "file_change":
      return {
        type: "file_change_completed",
        itemId: item.id,
        status: item.status,
        changeCount: item.changes.length,
        changeKinds: [...new Set(item.changes.map((change) => change.kind))],
      };
    case "mcp_tool_call":
      return {
        type: "tool_completed",
        itemId: item.id,
        toolType: "mcp",
        status: item.status,
      };
    case "web_search":
      return {
        type: "tool_completed",
        itemId: item.id,
        toolType: "web_search",
        status: "completed",
      };
    case "reasoning":
    case "todo_list":
    case "error":
      return undefined;
    default:
      throw new Error("Codex emitted an unsupported item type");
  }
}

export class OpenAICodexAdapter implements CodexAdapter {
  private readonly codex: Codex;

  constructor(private readonly model?: string) {
    // Omitting `env` intentionally inherits the runner process environment,
    // including the operator's existing local Codex authentication.
    this.codex = new Codex();
  }

  async *execute(
    input: CodexExecutionInput,
  ): AsyncIterable<CodexProgressEvent> {
    const threadOptions = {
      workingDirectory: input.workingDirectory,
      sandboxMode: "workspace-write" as const,
      approvalPolicy: "never" as const,
      networkAccessEnabled: input.networkAccess,
      webSearchMode: input.networkAccess
        ? ("cached" as const)
        : ("disabled" as const),
      ...(this.model === undefined ? {} : { model: this.model }),
    };
    const thread = this.codex.startThread(threadOptions);
    const { events } = await thread.runStreamed(input.prompt);
    for await (const event of events) {
      const normalized = normalizeEvent(event);
      if (normalized !== undefined) {
        yield normalized;
      }
    }
  }
}
