import type { TokenUsage } from "../executions/execution-types.js";

export interface CodexExecutionInput {
  prompt: string;
  workingDirectory: string;
  networkAccess: boolean;
}

export type CodexProgressEvent =
  | { type: "thread_started"; threadId: string }
  | { type: "agent_message_completed"; text: string }
  | {
      type: "command_completed";
      itemId: string;
      status: string;
      exitCode?: number;
    }
  | {
      type: "file_change_completed";
      itemId: string;
      status: string;
      changeCount: number;
      changeKinds: string[];
    }
  | {
      type: "tool_completed";
      itemId: string;
      toolType: "mcp" | "web_search";
      status: string;
    }
  | { type: "token_usage"; usage: TokenUsage };

export interface CodexAdapter {
  execute(input: CodexExecutionInput): AsyncIterable<CodexProgressEvent>;
}
