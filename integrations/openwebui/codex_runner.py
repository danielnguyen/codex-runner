"""
title: Codex
author: codex-runner contributors
version: 0.1.2
required_open_webui_version: 0.11.0
description: Submit explicitly approved Codex executions through codex-runner.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field

MAX_PROMPT_BYTES = 65_536
TERMINAL_RUN_STATUSES = {"completed", "failed", "interrupted"}
TERMINAL_EVENT_TYPES = {"run.completed", "run.failed", "run.interrupted"}

EventEmitter = Callable[[dict[str, Any]], Awaitable[Any]]
EventCall = Callable[[dict[str, Any]], Awaitable[Any]]
ConfirmationOutcome = Literal["confirmed", "rejected", "unavailable"]

UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
HASH_PATTERN = r"[0-9a-f]{64}"
APPROVAL_COMMAND_PATTERN = (
    rf"(?:(?i:@codex)[ \t]+)?(?i:approve)[ \t]+"
    rf"(?P<request_id>{UUID_PATTERN})[ \t]+(?P<prompt_sha256>{HASH_PATTERN})"
)
APPROVAL_COMMAND_RE = re.compile(rf"^{APPROVAL_COMMAND_PATTERN}$")
APPROVAL_PREFIX_RE = re.compile(r"^(?:(?i:@codex)\s+)?(?i:approve)(?:\s|$)")
EMBEDDED_APPROVAL_COMMAND_RE = re.compile(
    rf"(?<![A-Za-z0-9_]){APPROVAL_COMMAND_PATTERN}"
)
CHANNEL_MESSAGE_PATH_RE = re.compile(
    r"^/api/v1/channels/(?P<channel_id>[^/]+)/messages/post/?$"
)
MODEL_MENTION_RE = re.compile(r"<@M:(?P<model_id>[^|>]+)(?:\|(?P<label>[^>]*))?>")
ROUTING_DISPLAY_NAME = "Codex"


class IntegrationError(Exception):
    """A failure that is safe to describe to an API consumer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RunnerClientError(IntegrationError):
    """A sanitized codex-runner API failure."""


@dataclass(frozen=True)
class ApprovalCommand:
    request_id: str
    prompt_sha256: str


def parse_approval_command(prompt: str) -> ApprovalCommand | None:
    match = APPROVAL_COMMAND_RE.fullmatch(prompt)
    if match is not None:
        return ApprovalCommand(
            request_id=match.group("request_id"),
            prompt_sha256=match.group("prompt_sha256"),
        )
    if APPROVAL_PREFIX_RE.search(prompt) or EMBEDDED_APPROVAL_COMMAND_RE.search(prompt):
        raise IntegrationError(
            "INVALID_APPROVAL_COMMAND",
            "Approval commands must exactly match "
            "'@Codex approve <request-id> <prompt-sha256>'.",
        )
    return None


def normalize_runner_url(value: str) -> str:
    if not value:
        raise IntegrationError("CONFIGURATION_ERROR", "Runner URL is required.")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise IntegrationError(
            "CONFIGURATION_ERROR", "Runner URL is not a valid absolute URL."
        ) from error

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise IntegrationError(
            "CONFIGURATION_ERROR",
            "Runner URL must be an absolute http or https URL.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise IntegrationError(
            "CONFIGURATION_ERROR", "Runner URL must not contain user information."
        )
    if parsed.query or parsed.fragment:
        raise IntegrationError(
            "CONFIGURATION_ERROR",
            "Runner URL must not contain a query string or fragment.",
        )
    if parsed.hostname is None or port is not None and not 1 <= port <= 65_535:
        raise IntegrationError("CONFIGURATION_ERROR", "Runner URL is invalid.")

    path_parts = [part for part in parsed.path.split("/") if part]
    if any(part in {".", ".."} for part in path_parts):
        raise IntegrationError(
            "CONFIGURATION_ERROR", "Runner URL must not contain dot segments."
        )
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def extract_exact_prompt(
    body: Mapping[str, Any], metadata: Mapping[str, Any] | None
) -> str:
    for container in (body, metadata or {}):
        for key in ("files", "attachments", "sources"):
            if container.get(key):
                raise IntegrationError(
                    "UNSUPPORTED_INPUT",
                    "Attachments, sources, and multimodal input are not supported.",
                )

    messages = body.get("messages")
    if isinstance(messages, list):
        latest_user = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, Mapping) and message.get("role") == "user"
            ),
            None,
        )
        if latest_user is not None and not isinstance(latest_user.get("content"), str):
            raise IntegrationError(
                "UNSUPPORTED_INPUT", "The current user message must be plain text."
            )

    prompt: Any = None
    if metadata is not None and "user_prompt" in metadata:
        prompt = metadata["user_prompt"]
    elif isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, Mapping) and message.get("role") == "user":
                prompt = message.get("content")
                break

    if not isinstance(prompt, str):
        raise IntegrationError(
            "INVALID_PROMPT", "The current user message must be plain text."
        )
    if not prompt.strip():
        raise IntegrationError("INVALID_PROMPT", "The execution prompt is empty.")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise IntegrationError(
            "PROMPT_TOO_LARGE",
            f"The execution prompt exceeds {MAX_PROMPT_BYTES:,} UTF-8 bytes.",
        )
    return prompt


def _channel_error(message: str) -> IntegrationError:
    return IntegrationError("CHANNEL_RAW_PROMPT_UNAVAILABLE", message)


def _channel_id_from_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    chat_id = metadata.get("chat_id")
    session_id = metadata.get("session_id")
    channel_ids: list[str] = []
    for value in (chat_id, session_id):
        if isinstance(value, str) and value.startswith("channel:"):
            channel_ids.append(value.removeprefix("channel:"))
    if not channel_ids:
        return None
    if len(channel_ids) != 2 or channel_ids[0] != channel_ids[1]:
        raise _channel_error("Open WebUI supplied inconsistent Channel metadata.")
    message_id = metadata.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise _channel_error(
            "Open WebUI did not supply valid Channel message metadata."
        )
    return channel_ids[0]


def _channel_id_from_request(request: Any) -> str | None:
    if request is None:
        return None
    method = getattr(request, "method", None)
    url = getattr(request, "url", None)
    path = getattr(url, "path", None)
    if method != "POST" or not isinstance(path, str):
        return None
    match = CHANNEL_MESSAGE_PATH_RE.fullmatch(path)
    return match.group("channel_id") if match is not None else None


def _extract_channel_prompt(raw_content: Any, model_id: Any) -> str:
    if not isinstance(raw_content, str):
        raise _channel_error("Open WebUI did not supply a plain-text Channel message.")
    if not isinstance(model_id, str) or not model_id:
        raise _channel_error("Open WebUI did not identify the invoked Channel model.")

    mentions = list(MODEL_MENTION_RE.finditer(raw_content))
    if not mentions:
        # Open WebUI also invokes a model when a user replies to that model's
        # Channel message. No routing token is present in that authored content.
        return raw_content

    routing = mentions[0]
    if (
        routing.start() != 0
        or routing.group("model_id") != model_id
        or routing.group("label") != ROUTING_DISPLAY_NAME
        or len(mentions) != 1
    ):
        raise _channel_error(
            "Open WebUI supplied an ambiguous Channel model-mention payload."
        )

    prompt = raw_content[routing.end() :]
    # TipTap serializes the selected mention separately from the following
    # separator. Remove one ordinary separator, preserving all authored prompt
    # content after it byte-for-byte as a Python string.
    return prompt[1:] if prompt.startswith(" ") else prompt


async def extract_invocation_prompt(
    body: Mapping[str, Any],
    metadata: Mapping[str, Any] | None,
    request: Any,
) -> str:
    metadata_channel_id = _channel_id_from_metadata(metadata)
    request_channel_id = _channel_id_from_request(request)

    if metadata_channel_id is None and request_channel_id is None:
        return extract_exact_prompt(body, metadata)
    if (
        metadata_channel_id is None
        or request_channel_id is None
        or metadata_channel_id != request_channel_id
    ):
        raise _channel_error(
            "Open WebUI supplied only a decorated Channel prompt; the exact "
            "authored message could not be recovered."
        )

    try:
        payload = await request.json()
    except Exception:
        raise _channel_error(
            "Open WebUI's raw Channel message could not be read safely."
        ) from None
    if not isinstance(payload, Mapping):
        raise _channel_error("Open WebUI supplied malformed Channel message data.")

    data = payload.get("data")
    if data is not None and not isinstance(data, Mapping):
        raise _channel_error("Open WebUI supplied malformed Channel message data.")
    if isinstance(data, Mapping) and data.get("files"):
        raise IntegrationError(
            "UNSUPPORTED_INPUT",
            "Attachments, sources, and multimodal input are not supported.",
        )

    prompt = _extract_channel_prompt(payload.get("content"), body.get("model"))
    # Reuse the common exact-text validation without allowing the decorated
    # model prompt or thread history to become a fallback source.
    return extract_exact_prompt(
        {"messages": [{"role": "user", "content": prompt}]}, None
    )


@dataclass(frozen=True)
class SSEMessage:
    event: str | None
    event_id: str | None
    data: str


class SSEParser:
    """Incrementally parse an SSE byte-decoded text stream."""

    def __init__(self) -> None:
        self._buffer = ""
        self._event: str | None = None
        self._event_id: str | None = None
        self._data: list[str] = []

    def feed(self, chunk: str) -> list[SSEMessage]:
        self._buffer += chunk
        messages: list[SSEMessage] = []
        while True:
            boundary = self._next_line_boundary()
            if boundary is None:
                break
            index, width = boundary
            line = self._buffer[:index]
            self._buffer = self._buffer[index + width :]
            message = self._process_line(line)
            if message is not None:
                messages.append(message)
        return messages

    def finish(self) -> list[SSEMessage]:
        # A final unterminated line is a field, but an event is dispatched only by
        # a blank line. This lets callers distinguish an incomplete stream.
        if self._buffer.endswith("\r"):
            message = self._process_line(self._buffer[:-1])
            self._buffer = ""
            return [message] if message is not None else []
        if self._buffer:
            self._process_line(self._buffer)
            self._buffer = ""
        return []

    def _next_line_boundary(self) -> tuple[int, int] | None:
        for index, character in enumerate(self._buffer):
            if character == "\n":
                return index, 1
            if character == "\r":
                if index + 1 == len(self._buffer):
                    return None
                return index, 2 if self._buffer[index + 1] == "\n" else 1
        return None

    def _process_line(self, line: str) -> SSEMessage | None:
        if line == "":
            if not self._data:
                self._event = None
                return None
            message = SSEMessage(
                event=self._event,
                event_id=self._event_id,
                data="\n".join(self._data),
            )
            self._event = None
            self._data = []
            return message
        if line.startswith(":"):
            return None

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "id" and "\x00" not in value:
            self._event_id = value
        elif field == "data":
            self._data.append(value)
        return None


async def parse_sse_chunks(chunks: AsyncIterator[str]) -> AsyncIterator[SSEMessage]:
    parser = SSEParser()
    async for chunk in chunks:
        for message in parser.feed(chunk):
            yield message
    for message in parser.finish():
        yield message


class RunnerClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        connect_timeout: float,
        api_timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._token = token
        timeout = httpx.Timeout(api_timeout, connect=connect_timeout)
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> RunnerClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()

    async def create_request(self, repository_id: str, prompt: str) -> dict[str, Any]:
        return await self._json_request(
            "POST",
            "/v1/execution-requests",
            json_body={"repositoryId": repository_id, "prompt": prompt},
        )

    async def get_request(self, request_id: str) -> dict[str, Any]:
        return await self._json_request("GET", f"/v1/execution-requests/{request_id}")

    async def approve_request(
        self, request_id: str, prompt_sha256: str
    ) -> dict[str, Any]:
        return await self._json_request(
            "POST",
            f"/v1/execution-requests/{request_id}/approve",
            json_body={"promptSha256": prompt_sha256},
        )

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._json_request("GET", f"/v1/runs/{run_id}")

    async def stream_events(
        self, run_id: str, *, last_event_id: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        try:
            async with self._client.stream(
                "GET", self._url(f"/v1/runs/{run_id}/events"), headers=headers
            ) as response:
                await self._validate_response(response)
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type.lower():
                    raise RunnerClientError(
                        "INVALID_RUNNER_RESPONSE",
                        "Runner returned an invalid event-stream response.",
                    )
                async for message in parse_sse_chunks(response.aiter_text()):
                    yield self._decode_runner_event(message, run_id)
        except RunnerClientError:
            raise
        except (httpx.HTTPError, UnicodeError):
            raise RunnerClientError(
                "RUNNER_UNAVAILABLE", "Runner event streaming was interrupted."
            ) from None

    async def _json_request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                self._url(path),
                json=json_body,
                headers={"Accept": "application/json"},
            )
            await self._validate_response(response)
            payload = response.json()
        except RunnerClientError:
            raise
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise RunnerClientError(
                "RUNNER_UNAVAILABLE", "Runner request failed safely."
            ) from None
        if not isinstance(payload, dict):
            raise RunnerClientError(
                "INVALID_RUNNER_RESPONSE", "Runner returned invalid JSON."
            )
        return payload

    async def _validate_response(self, response: httpx.Response) -> None:
        if 300 <= response.status_code < 400:
            raise RunnerClientError(
                "RUNNER_REDIRECT_REJECTED", "Runner redirects are not allowed."
            )
        if response.status_code < 400:
            return

        code = "RUNNER_REQUEST_FAILED"
        message = f"Runner rejected the request with HTTP {response.status_code}."
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                if isinstance(error.get("code"), str):
                    code = error["code"]
                if isinstance(error.get("message"), str):
                    message = error["message"]
        except (ValueError, UnicodeError):
            pass
        raise RunnerClientError(self._redact_token(code), self._redact_token(message))

    def _decode_runner_event(
        self, message: SSEMessage, expected_run_id: str
    ) -> dict[str, Any]:
        try:
            event = json.loads(message.data)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise RunnerClientError(
                "INVALID_RUNNER_EVENT", "Runner sent malformed event data."
            ) from error
        if not isinstance(event, dict):
            raise RunnerClientError(
                "INVALID_RUNNER_EVENT", "Runner sent malformed event data."
            )
        event_id = event.get("id")
        if (
            not isinstance(event_id, int)
            or isinstance(event_id, bool)
            or event.get("runId") != expected_run_id
            or not isinstance(event.get("type"), str)
            or not isinstance(event.get("data"), dict)
        ):
            raise RunnerClientError(
                "INVALID_RUNNER_EVENT", "Runner sent malformed event data."
            )
        if message.event_id is not None and message.event_id != str(event_id):
            raise RunnerClientError(
                "INVALID_RUNNER_EVENT", "Runner event identifiers did not match."
            )
        return event

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _redact_token(self, value: str) -> str:
        return value.replace(self._token, "[redacted]")


class Pipe:
    class Valves(BaseModel):
        RUNNER_URL: str = Field(
            default="", description="Absolute URL of the codex-runner service."
        )
        RUNNER_TOKEN: str = Field(
            default="",
            description="Bearer token for codex-runner.",
            json_schema_extra={"input": {"type": "password"}},
        )
        REPOSITORY_ID: str = Field(
            default="", description="Server-configured repository ID."
        )
        ADMIN_ONLY: bool = Field(
            default=True, description="Restrict this Pipe to Open WebUI admins."
        )
        CONNECT_TIMEOUT_SECONDS: float = Field(default=5.0, ge=0.1, le=60.0)
        API_TIMEOUT_SECONDS: float = Field(default=30.0, ge=1.0, le=300.0)
        RUN_TIMEOUT_SECONDS: float = Field(default=7200.0, ge=1.0, le=86_400.0)

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._transport: httpx.AsyncBaseTransport | None = None
        self._poll_interval_seconds = 1.0

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __metadata__: dict[str, Any] | None = None,
        __event_emitter__: EventEmitter | None = None,
        __event_call__: EventCall | None = None,
        __request__: Any = None,
    ) -> str:
        request_id: str | None = None
        run_id: str | None = None
        prompt_sha256: str | None = None
        repository_id = self.valves.REPOSITORY_ID

        try:
            config = self._validated_config()
            repository_id = config["repository_id"]
            self._authorize(__user__)
            prompt = await extract_invocation_prompt(body, __metadata__, __request__)
            if config["runner_token"] in prompt:
                raise IntegrationError(
                    "SECRET_IN_PROMPT",
                    "The prompt contains the configured runner token and was rejected.",
                )
            approval_command = parse_approval_command(prompt)

            async with RunnerClient(
                base_url=config["runner_url"],
                token=config["runner_token"],
                connect_timeout=config["connect_timeout"],
                api_timeout=config["api_timeout"],
                transport=self._transport,
            ) as client:
                if approval_command is not None:
                    await self._emit_status(
                        __event_emitter__, "Verifying pending execution request…"
                    )
                    request_id = approval_command.request_id
                    request = await client.get_request(request_id)
                    stored_prompt, prompt_sha256 = self._validate_fetched_request(
                        request,
                        approval_command,
                        repository_id,
                    )
                    if config["runner_token"] in stored_prompt:
                        raise IntegrationError(
                            "SECRET_IN_PROMPT",
                            "The stored prompt contains the configured runner token "
                            "and was rejected.",
                        )
                else:
                    await self._emit_status(
                        __event_emitter__, "Creating guarded execution request…"
                    )
                    request = await client.create_request(repository_id, prompt)
                    request_id, prompt_sha256 = self._validate_created_request(
                        request, repository_id
                    )

                    confirmation = await self._request_confirmation(
                        __event_call__,
                        repository_id=repository_id,
                        request_id=request_id,
                        prompt_sha256=prompt_sha256,
                        prompt=prompt,
                    )
                    if confirmation == "rejected":
                        await self._emit_status(
                            __event_emitter__,
                            "Execution was explicitly rejected.",
                            done=True,
                        )
                        return self._redact_token(
                            self._rejected_result(
                                repository_id, request_id, prompt_sha256
                            )
                        )
                    if confirmation == "unavailable":
                        await self._emit_status(
                            __event_emitter__,
                            "Interactive confirmation unavailable; "
                            "request pending approval.",
                            done=True,
                        )
                        return self._redact_token(
                            self._pending_approval_result(
                                repository_id,
                                request_id,
                                prompt_sha256,
                                prompt,
                            )
                        )

                await self._emit_status(
                    __event_emitter__,
                    "Explicit authorization received; starting guarded execution…",
                )
                approval = await client.approve_request(request_id, prompt_sha256)
                run_id = self._validate_approval(approval, request_id)

                run = await self._follow_run(
                    client,
                    run_id,
                    config["run_timeout"],
                    __event_emitter__,
                )
                await self._emit_status(
                    __event_emitter__,
                    "Run completed."
                    if run.get("status") == "completed"
                    else "Run finished without completion.",
                    done=True,
                )
                return self._redact_token(
                    self._durable_run_result(
                        repository_id,
                        request_id,
                        run_id,
                        prompt_sha256,
                        run,
                    )
                )
        except IntegrationError as error:
            await self._emit_status(
                __event_emitter__,
                "Stopped safely.",
                done=True,
            )
            return self._redact_token(
                self._failure_result(
                    repository_id=repository_id,
                    request_id=request_id,
                    run_id=run_id,
                    prompt_sha256=prompt_sha256,
                    code=error.code,
                    message=error.message,
                )
            )
        except Exception:
            await self._emit_status(
                __event_emitter__,
                "Stopped safely after an unexpected error.",
                done=True,
            )
            return self._redact_token(
                self._failure_result(
                    repository_id=repository_id,
                    request_id=request_id,
                    run_id=run_id,
                    prompt_sha256=prompt_sha256,
                    code="INTEGRATION_ERROR",
                    message="The integration stopped safely after an unexpected error.",
                )
            )

    def _validated_config(self) -> dict[str, Any]:
        runner_url = normalize_runner_url(self.valves.RUNNER_URL)
        if not self.valves.RUNNER_TOKEN:
            raise IntegrationError("CONFIGURATION_ERROR", "Runner token is required.")
        if not self.valves.REPOSITORY_ID:
            raise IntegrationError("CONFIGURATION_ERROR", "Repository ID is required.")
        if not self.valves.REPOSITORY_ID.strip():
            raise IntegrationError(
                "CONFIGURATION_ERROR", "Repository ID must not be blank."
            )
        return {
            "runner_url": runner_url,
            "runner_token": self.valves.RUNNER_TOKEN,
            "repository_id": self.valves.REPOSITORY_ID,
            "connect_timeout": self._bounded_number(
                self.valves.CONNECT_TIMEOUT_SECONDS, 0.1, 60.0, "connect timeout"
            ),
            "api_timeout": self._bounded_number(
                self.valves.API_TIMEOUT_SECONDS, 1.0, 300.0, "API timeout"
            ),
            "run_timeout": self._bounded_number(
                self.valves.RUN_TIMEOUT_SECONDS, 1.0, 86_400.0, "run timeout"
            ),
        }

    def _bounded_number(
        self, value: Any, minimum: float, maximum: float, label: str
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise IntegrationError(
                "CONFIGURATION_ERROR", f"Configured {label} is invalid."
            )
        number = float(value)
        if not minimum <= number <= maximum:
            raise IntegrationError(
                "CONFIGURATION_ERROR", f"Configured {label} is out of bounds."
            )
        return number

    def _authorize(self, user: Mapping[str, Any] | None) -> None:
        if not self.valves.ADMIN_ONLY:
            return
        if user is None or user.get("role") != "admin":
            raise IntegrationError(
                "FORBIDDEN", "This Codex Pipe is restricted to administrators."
            )

    async def _request_confirmation(
        self,
        event_call: EventCall | None,
        *,
        repository_id: str,
        request_id: str,
        prompt_sha256: str,
        prompt: str,
    ) -> ConfirmationOutcome:
        if not callable(event_call):
            return "unavailable"
        byte_count = len(prompt.encode("utf-8"))
        message = (
            "Confirmation will start Codex in the configured "
            "allowlisted repository.\n\n"
            f"Repository ID: `{repository_id}`\n\n"
            f"Request ID: `{request_id}`\n\n"
            f"Prompt SHA-256: `{prompt_sha256}`\n\n"
            f"Prompt size: {byte_count} UTF-8 bytes\n\n"
            "Exact prompt follows:\n\n"
            f"{prompt}"
        )
        try:
            result = await event_call(
                {
                    "type": "confirmation",
                    "data": {
                        "title": "Approve guarded Codex execution?",
                        "message": message,
                    },
                }
            )
        except Exception:
            return "unavailable"
        if result is True:
            return "confirmed"
        if result is False:
            return "rejected"
        return "unavailable"

    def _validate_created_request(
        self, request: Mapping[str, Any], repository_id: str
    ) -> tuple[str, str]:
        request_id = request.get("requestId")
        prompt_sha256 = request.get("promptSha256")
        if (
            not isinstance(request_id, str)
            or not request_id
            or request.get("repositoryId") != repository_id
            or not isinstance(prompt_sha256, str)
            or len(prompt_sha256) != 64
            or any(character not in "0123456789abcdef" for character in prompt_sha256)
            or request.get("status") != "pending_approval"
            or not isinstance(request.get("createdAt"), str)
        ):
            raise IntegrationError(
                "INVALID_RUNNER_RESPONSE",
                "Runner returned an invalid execution-request response.",
            )
        return request_id, prompt_sha256

    def _validate_fetched_request(
        self,
        request: Mapping[str, Any],
        command: ApprovalCommand,
        repository_id: str,
    ) -> tuple[str, str]:
        request_id = request.get("requestId")
        stored_repository_id = request.get("repositoryId")
        prompt_sha256 = request.get("promptSha256")
        prompt = request.get("prompt")
        status = request.get("status")
        run_id = request.get("runId")

        if request_id != command.request_id:
            raise IntegrationError(
                "APPROVAL_REQUEST_MISMATCH",
                "Runner returned a different execution request.",
            )
        if stored_repository_id != repository_id:
            raise IntegrationError(
                "APPROVAL_REPOSITORY_MISMATCH",
                "The execution request belongs to a different configured repository.",
            )
        if not isinstance(prompt_sha256, str) or prompt_sha256 != command.prompt_sha256:
            raise IntegrationError(
                "APPROVAL_HASH_MISMATCH",
                "The supplied prompt hash does not match the immutable request.",
            )
        if not isinstance(prompt, str):
            raise IntegrationError(
                "INVALID_RUNNER_RESPONSE",
                "Runner returned an execution request without a text prompt.",
            )
        if status == "pending_approval":
            if run_id is not None:
                raise IntegrationError(
                    "INVALID_RUNNER_RESPONSE",
                    "Pending execution request unexpectedly contains a run ID.",
                )
        elif status in {"queued", "running", "completed"}:
            if not isinstance(run_id, str) or not run_id:
                raise IntegrationError(
                    "INVALID_RUNNER_RESPONSE",
                    "Started execution request does not contain a run ID.",
                )
        else:
            raise IntegrationError(
                "REQUEST_NOT_APPROVABLE",
                "The execution request is not pending or in an "
                "idempotent started state.",
            )
        return prompt, prompt_sha256

    def _validate_approval(self, approval: Mapping[str, Any], request_id: str) -> str:
        run_id = approval.get("runId")
        if (
            approval.get("requestId") != request_id
            or not isinstance(run_id, str)
            or not run_id
            or approval.get("status") not in {"queued", "running", "completed"}
        ):
            raise IntegrationError(
                "INVALID_RUNNER_RESPONSE",
                "Runner returned an invalid approval response.",
            )
        return run_id

    async def _follow_run(
        self,
        client: RunnerClient,
        run_id: str,
        run_timeout: float,
        emitter: EventEmitter | None,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + run_timeout
        terminal_seen = False
        try:
            async with asyncio.timeout(self._remaining(deadline)):
                async for event in client.stream_events(run_id):
                    await self._emit_runner_event_status(emitter, event["type"])
                    if event["type"] in TERMINAL_EVENT_TYPES:
                        terminal_seen = True
                        break
            if not terminal_seen:
                raise RunnerClientError(
                    "EVENT_STREAM_INCOMPLETE",
                    "Runner event stream ended before a terminal event.",
                )
            run = await client.get_run(run_id)
            return self._validate_run(run, run_id)
        except (RunnerClientError, TimeoutError):
            await self._emit_status(
                emitter,
                "Live events were interrupted; polling the existing run…",
            )
            return await self._poll_run(client, run_id, deadline)

    async def _poll_run(
        self, client: RunnerClient, run_id: str, deadline: float
    ) -> dict[str, Any]:
        while True:
            if self._remaining(deadline) <= 0:
                raise IntegrationError(
                    "RUN_WAIT_TIMEOUT",
                    "The client wait limit expired; the run was not cancelled "
                    "and no automatic retry occurred.",
                )
            try:
                async with asyncio.timeout(self._remaining(deadline)):
                    run = self._validate_run(await client.get_run(run_id), run_id)
            except TimeoutError as error:
                raise IntegrationError(
                    "RUN_WAIT_TIMEOUT",
                    "The client wait limit expired; the run was not cancelled "
                    "and no automatic retry occurred.",
                ) from error
            if run["status"] in TERMINAL_RUN_STATUSES:
                return run
            await asyncio.sleep(
                min(self._poll_interval_seconds, self._remaining(deadline))
            )

    def _validate_run(
        self, run: Mapping[str, Any], expected_run_id: str
    ) -> dict[str, Any]:
        if (
            run.get("runId") != expected_run_id
            or not isinstance(run.get("requestId"), str)
            or not isinstance(run.get("repositoryId"), str)
            or run.get("status")
            not in {"queued", "running", "completed", "failed", "interrupted"}
        ):
            raise IntegrationError(
                "INVALID_RUNNER_RESPONSE", "Runner returned an invalid run record."
            )
        return dict(run)

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

    async def _emit_runner_event_status(
        self, emitter: EventEmitter | None, event_type: str
    ) -> None:
        descriptions = {
            "run.started": "Codex run started.",
            "codex.thread_started": "Codex thread identified.",
            "codex.agent_message_completed": "Codex message completed.",
            "codex.command_completed": "Command/tool item completed.",
            "codex.tool_completed": "Command/tool item completed.",
            "codex.file_change_completed": "File-change item completed.",
            "codex.token_usage": "Token usage updated.",
            "codex.final_response": "Final Codex response received.",
            "run.completed": "Codex run completed.",
            "run.failed": "Codex run failed.",
            "run.interrupted": "Codex run was interrupted.",
        }
        description = descriptions.get(event_type)
        if description is not None:
            await self._emit_status(emitter, description)

    async def _emit_status(
        self, emitter: EventEmitter | None, description: str, *, done: bool = False
    ) -> None:
        if not callable(emitter):
            return
        try:
            await emitter(
                {
                    "type": "status",
                    "data": {
                        "description": description,
                        "done": done,
                        "hidden": False,
                    },
                }
            )
        except Exception:
            # Status delivery is supplementary and must not duplicate execution.
            return

    def _rejected_result(
        self, repository_id: str, request_id: str, prompt_sha256: str
    ) -> str:
        return (
            "## Codex execution rejected\n\n"
            "The execution was explicitly rejected in the confirmation dialog. "
            "No run was started, and the immutable request remains pending.\n\n"
            "- Status: `explicitly rejected`\n"
            f"- Repository ID: `{repository_id}`\n"
            f"- Request ID: `{request_id}`\n"
            f"- Prompt SHA-256: `{prompt_sha256}`"
        )

    def _pending_approval_result(
        self,
        repository_id: str,
        request_id: str,
        prompt_sha256: str,
        prompt: str,
    ) -> str:
        byte_count = len(prompt.encode("utf-8"))
        approval_command = f"@Codex approve {request_id} {prompt_sha256}"
        return (
            "## Codex execution pending approval\n\n"
            "Interactive confirmation was unavailable. The execution request "
            "remains pending approval. No run has started.\n\n"
            "Sending the exact approval command below is an explicit authorization "
            "to start Codex in the configured allowlisted repository.\n\n"
            "- Status: `pending approval`\n"
            f"- Repository ID: `{repository_id}`\n"
            f"- Request ID: `{request_id}`\n"
            f"- Prompt SHA-256: `{prompt_sha256}`\n"
            f"- Prompt size: {byte_count} UTF-8 bytes\n\n"
            "### Exact prompt\n\n"
            f"{prompt}\n\n"
            "### Explicit approval command\n\n"
            f"```text\n{approval_command}\n```"
        )

    def _durable_run_result(
        self,
        repository_id: str,
        request_id: str,
        run_id: str,
        prompt_sha256: str,
        run: Mapping[str, Any],
    ) -> str:
        status = run["status"]
        if status == "completed":
            final_response = run.get("finalResponse")
            if not isinstance(final_response, str):
                final_response = "Runner completed without a final response."
            lines = [
                "## Codex execution completed",
                "",
                f"- Status: `{status}`",
                f"- Repository ID: `{repository_id}`",
                f"- Request ID: `{request_id}`",
                f"- Run ID: `{run_id}`",
                f"- Prompt SHA-256: `{prompt_sha256}`",
            ]
            usage = run.get("usage")
            if isinstance(usage, dict):
                lines.extend(
                    [
                        "",
                        "### Usage",
                        "",
                        f"```json\n{json.dumps(usage, sort_keys=True)}\n```",
                    ]
                )
            lines.extend(["", "### Final Codex response", "", final_response])
            return "\n".join(lines)

        error = run.get("error")
        code = "RUN_FAILED" if status == "failed" else "RUN_INTERRUPTED"
        message = "The runner did not complete the execution."
        if isinstance(error, dict):
            if isinstance(error.get("code"), str):
                code = error["code"]
            if isinstance(error.get("message"), str):
                message = error["message"]
        return self._failure_result(
            repository_id=repository_id,
            request_id=request_id,
            run_id=run_id,
            prompt_sha256=prompt_sha256,
            code=code,
            message=message,
            status=status,
        )

    def _failure_result(
        self,
        *,
        repository_id: str,
        request_id: str | None,
        run_id: str | None,
        prompt_sha256: str | None,
        code: str,
        message: str,
        status: str = "failed",
    ) -> str:
        lines = [
            "## Codex execution did not complete",
            "",
            f"- Status: `{status}`",
        ]
        if repository_id:
            lines.append(f"- Repository ID: `{repository_id}`")
        if request_id:
            lines.append(f"- Request ID: `{request_id}`")
        if run_id:
            lines.append(f"- Run ID: `{run_id}`")
        if prompt_sha256:
            lines.append(f"- Prompt SHA-256: `{prompt_sha256}`")
        lines.extend(
            [
                f"- Error code: `{code}`",
                f"- Message: {message}",
                "",
                "No automatic retry or cancellation occurred.",
            ]
        )
        return "\n".join(lines)

    def _redact_token(self, value: str) -> str:
        token = self.valves.RUNNER_TOKEN
        if token:
            return value.replace(token, "[redacted]")
        return value
