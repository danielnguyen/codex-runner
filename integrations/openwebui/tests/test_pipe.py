import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from codex_runner import (
    MAX_PROMPT_BYTES,
    IntegrationError,
    Pipe,
    extract_exact_prompt,
    extract_invocation_prompt,
    parse_approval_command,
)

REQUEST_ID = "aaaaaaaa-1111-4111-8111-111111111111"
RUN_ID = "run-example"
PROMPT_HASH = "a" * 64
TOKEN = "REPLACE_WITH_RANDOM_TOKEN"
REPOSITORY_ID = "example-repository"
CHANNEL_ID = "example-channel"
MODEL_ID = "codex"


class FakeChannelRequest:
    method = "POST"

    def __init__(self, payload: Any, *, channel_id: str = CHANNEL_ID) -> None:
        self.url = httpx.URL(
            f"http://openwebui.example.invalid/api/v1/channels/{channel_id}/messages/post"
        )
        self._payload = payload

    async def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def configured_pipe(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Pipe:
    pipe = Pipe()
    pipe.valves = Pipe.Valves(
        RUNNER_URL="http://runner.example.internal:8787",
        RUNNER_TOKEN=TOKEN,
        REPOSITORY_ID=REPOSITORY_ID,
    )
    if handler is not None:
        pipe._transport = httpx.MockTransport(handler)
    pipe._poll_interval_seconds = 0
    return pipe


def created_response() -> dict[str, Any]:
    return {
        "requestId": REQUEST_ID,
        "repositoryId": REPOSITORY_ID,
        "promptSha256": PROMPT_HASH,
        "status": "pending_approval",
        "createdAt": "2026-01-01T00:00:00.000Z",
    }


def fetched_request_response(
    *,
    request_id: str = REQUEST_ID,
    repository_id: str = REPOSITORY_ID,
    prompt_sha256: str = PROMPT_HASH,
    prompt: Any = "Exact prompt",
    status: str = "pending_approval",
    run_id: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "requestId": request_id,
        "repositoryId": repository_id,
        "promptSha256": prompt_sha256,
        "prompt": prompt,
        "status": status,
        "createdAt": "2026-01-01T00:00:00.000Z",
    }
    if run_id is not None:
        response["runId"] = run_id
    return response


def approval_response() -> dict[str, Any]:
    return {"requestId": REQUEST_ID, "runId": RUN_ID, "status": "queued"}


def run_response(
    *, status: str = "completed", final_response: str = "Finished safely."
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "runId": RUN_ID,
        "requestId": REQUEST_ID,
        "repositoryId": REPOSITORY_ID,
        "status": status,
        "createdAt": "2026-01-01T00:00:00.000Z",
    }
    if status == "completed":
        response["finalResponse"] = final_response
        response["usage"] = {"inputTokens": 12, "outputTokens": 4}
    return response


def sse_event(event_id: int, event_type: str) -> str:
    event = {
        "id": event_id,
        "runId": RUN_ID,
        "type": event_type,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "data": {},
    }
    return f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(event)}\n\n"


def lifecycle_handler(
    requests: list[httpx.Request],
    *,
    sse_status: int = 200,
    run_status: str = "completed",
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/execution-requests":
            return httpx.Response(201, json=created_response())
        if request.url.path == f"/v1/execution-requests/{REQUEST_ID}":
            return httpx.Response(200, json=fetched_request_response())
        if request.url.path == f"/v1/execution-requests/{REQUEST_ID}/approve":
            return httpx.Response(202, json=approval_response())
        if request.url.path == f"/v1/runs/{RUN_ID}/events":
            if sse_status != 200:
                return httpx.Response(
                    sse_status,
                    json={
                        "error": {
                            "code": "STREAM_ERROR",
                            "message": "Stream unavailable",
                        }
                    },
                )
            events = sse_event(1, "run.started") + sse_event(
                2, "run.completed" if run_status == "completed" else "run.failed"
            )
            return httpx.Response(
                200,
                text=": keepalive\n\n" + events,
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path == f"/v1/runs/{RUN_ID}":
            response = run_response(status=run_status)
            if run_status == "failed":
                response["error"] = {
                    "code": "CODEX_EXECUTION_FAILED",
                    "message": "Codex execution failed",
                }
            return httpx.Response(200, json=response)
        raise AssertionError(f"unexpected path: {request.url.path}")

    return handler


def approval_command_handler(
    requests: list[httpx.Request],
    *,
    fetched: dict[str, Any] | None = None,
    approval: dict[str, Any] | None = None,
    sse_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"/v1/execution-requests/{REQUEST_ID}":
            return httpx.Response(
                200,
                json=fetched if fetched is not None else fetched_request_response(),
            )
        if request.url.path == f"/v1/execution-requests/{REQUEST_ID}/approve":
            return httpx.Response(
                202,
                json=approval if approval is not None else approval_response(),
            )
        if request.url.path == f"/v1/runs/{RUN_ID}/events":
            if sse_status != 200:
                return httpx.Response(
                    sse_status,
                    json={
                        "error": {
                            "code": "STREAM_ERROR",
                            "message": "Stream unavailable",
                        }
                    },
                )
            return httpx.Response(
                200,
                text=sse_event(1, "run.started") + sse_event(2, "run.completed"),
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path == f"/v1/runs/{RUN_ID}":
            return httpx.Response(200, json=run_response())
        raise AssertionError(f"unexpected path: {request.url.path}")

    return handler


async def confirm(event: dict[str, Any]) -> bool:
    return True


async def reject(event: dict[str, Any]) -> bool:
    return False


def body(prompt: Any = "Exact prompt") -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": "Do not include this."},
            {"role": "user", "content": "Prior user text."},
            {"role": "assistant", "content": "Prior assistant text."},
            {"role": "user", "content": prompt},
        ]
    }


def channel_body(decorated_prompt: str) -> dict[str, Any]:
    request_body = body(decorated_prompt)
    request_body["model"] = MODEL_ID
    return request_body


def channel_metadata(decorated_prompt: str) -> dict[str, Any]:
    return {
        "chat_id": f"channel:{CHANNEL_ID}",
        "session_id": f"channel:{CHANNEL_ID}",
        "message_id": "assistant-message-example",
        "user_prompt": decorated_prompt,
    }


def channel_request(prompt: str) -> FakeChannelRequest:
    return FakeChannelRequest(
        {"content": f"<@M:{MODEL_ID}|Codex> {prompt}", "data": {"files": []}}
    )


def approval_command(*, mention: bool = True) -> str:
    prefix = "@Codex " if mention else ""
    return f"{prefix}approve {REQUEST_ID} {PROMPT_HASH}"


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("RUNNER_URL", "Runner URL is required"),
        ("RUNNER_TOKEN", "Runner token is required"),
        ("REPOSITORY_ID", "Repository ID is required"),
    ],
)
@pytest.mark.asyncio
async def test_missing_configuration_fails_closed(field: str, message: str) -> None:
    pipe = configured_pipe()
    setattr(pipe.valves, field, "")

    result = await pipe.pipe(body(), {"role": "admin"}, {}, None, confirm)

    assert message in result
    assert "No automatic retry or cancellation occurred" in result


@pytest.mark.asyncio
async def test_out_of_bounds_timeout_fails_closed() -> None:
    pipe = configured_pipe()
    pipe.valves.RUN_TIMEOUT_SECONDS = 100_000

    result = await pipe.pipe(body(), {"role": "admin"}, {}, None, confirm)

    assert "CONFIGURATION_ERROR" in result
    assert "out of bounds" in result


@pytest.mark.asyncio
async def test_admin_only_rejects_non_admin_and_missing_role() -> None:
    pipe = configured_pipe()
    for user in ({"role": "user"}, {}, None):
        result = await pipe.pipe(body(), user, {}, None, confirm)
        assert "FORBIDDEN" in result


@pytest.mark.asyncio
async def test_admin_only_can_be_disabled() -> None:
    requests: list[httpx.Request] = []
    pipe = configured_pipe(lifecycle_handler(requests))
    pipe.valves.ADMIN_ONLY = False

    result = await pipe.pipe(
        body(), None, {"user_prompt": "Exact prompt"}, None, reject
    )

    assert "explicitly rejected" in result
    assert len(requests) == 1


def test_metadata_prompt_is_preferred_and_preserved_exactly() -> None:
    exact = "  Exact\nUnicode snowman: ☃  "
    assert extract_exact_prompt(body("wrapped body"), {"user_prompt": exact}) == exact


def test_fallback_uses_only_latest_plain_text_user_message() -> None:
    assert extract_exact_prompt(body("Latest only"), None) == "Latest only"


@pytest.mark.parametrize("prompt", ["", "   \n\t"])
def test_empty_prompt_is_rejected(prompt: str) -> None:
    with pytest.raises(IntegrationError) as raised:
        extract_exact_prompt(body(prompt), None)
    assert raised.value.code == "INVALID_PROMPT"


def test_utf8_prompt_limit_is_enforced() -> None:
    assert extract_exact_prompt(body("é" * (MAX_PROMPT_BYTES // 2)), None)
    with pytest.raises(IntegrationError) as raised:
        extract_exact_prompt(body("é" * (MAX_PROMPT_BYTES // 2 + 1)), None)
    assert raised.value.code == "PROMPT_TOO_LARGE"


@pytest.mark.parametrize("key", ["files", "attachments", "sources"])
def test_attached_context_is_rejected(key: str) -> None:
    request_body = body()
    request_body[key] = [{"example": True}]
    with pytest.raises(IntegrationError) as raised:
        extract_exact_prompt(request_body, {"user_prompt": "Exact prompt"})
    assert raised.value.code == "UNSUPPORTED_INPUT"


def test_multimodal_content_is_rejected_even_with_metadata_prompt() -> None:
    with pytest.raises(IntegrationError) as raised:
        extract_exact_prompt(
            body([{"type": "text", "text": "Exact prompt"}]),
            {"user_prompt": "Exact prompt"},
        )
    assert raised.value.code == "UNSUPPORTED_INPUT"


def test_source_wrapped_body_does_not_replace_metadata_prompt() -> None:
    wrapped = "<source>untrusted context</source>\nExact prompt"
    assert (
        extract_exact_prompt(body(wrapped), {"user_prompt": "Exact prompt"})
        == "Exact prompt"
    )


@pytest.mark.asyncio
async def test_direct_chat_raw_prompt_remains_unchanged() -> None:
    prompt = "Example Operator: Codex must remain literal.  "

    assert (
        await extract_invocation_prompt(
            body("wrapped body"), {"user_prompt": prompt}, None
        )
        == prompt
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "Inspect this repository exactly.\nDo not modify anything.",
        "Example Operator: preserve this legitimate prefix.",
        "Codex is a legitimate first word in this prompt.",
    ],
)
@pytest.mark.asyncio
async def test_channel_prompt_uses_authoritative_raw_request(prompt: str) -> None:
    decorated = f"Example Operator: Codex {prompt}"

    assert (
        await extract_invocation_prompt(
            channel_body(decorated),
            channel_metadata(decorated),
            channel_request(prompt),
        )
        == prompt
    )


@pytest.mark.asyncio
async def test_channel_normal_request_excludes_identity_and_routing_label() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=created_response())

    prompt = (
        "Inspect this repository and report whether its working tree is clean.\n"
        "Do not create, modify, delete, stage, or commit anything."
    )
    decorated = f"Example Operator: Codex {prompt}"
    result = await configured_pipe(handler).pipe(
        channel_body(decorated),
        {"role": "admin"},
        channel_metadata(decorated),
        None,
        reject,
        channel_request(prompt),
    )

    assert len(requests) == 1
    assert json.loads(requests[0].content)["prompt"] == prompt
    assert "Example Operator" not in json.loads(requests[0].content)["prompt"]
    assert result.startswith("## Codex execution rejected")


@pytest.mark.asyncio
async def test_channel_pending_response_explains_structured_mention_workflow() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=created_response())

    prompt = "Inspect this repository without changing it."
    decorated = f"Example Operator: Codex {prompt}"
    result = await configured_pipe(handler).pipe(
        channel_body(decorated),
        {"role": "admin"},
        channel_metadata(decorated),
        None,
        None,
        channel_request(prompt),
    )

    command = f"approve {REQUEST_ID} {PROMPT_HASH}"
    assert [request.url.path for request in requests] == ["/v1/execution-requests"]
    assert "Type `@`, select **Codex** from the mention list, then paste:" in result
    assert f"```text\n{command}\n```" in result
    assert f"@Codex {command}" not in result
    assert "No run has started" in result


@pytest.mark.asyncio
async def test_channel_approval_uses_raw_command_without_creating_request() -> None:
    requests: list[httpx.Request] = []
    command = approval_command(mention=False)
    decorated = f"Example Operator: Codex {command}"

    result = await configured_pipe(approval_command_handler(requests)).pipe(
        channel_body(decorated),
        {"role": "admin"},
        channel_metadata(decorated),
        None,
        None,
        channel_request(command),
    )

    paths = [request.url.path for request in requests]
    assert paths[0] == f"/v1/execution-requests/{REQUEST_ID}"
    assert "/v1/execution-requests" not in paths
    assert paths.count(f"/v1/execution-requests/{REQUEST_ID}/approve") == 1
    assert "Finished safely." in result


@pytest.mark.asyncio
async def test_decorated_only_channel_input_fails_closed_before_runner_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    decorated = "Example Operator: Codex inspect this repository."
    result = await configured_pipe(handler).pipe(
        channel_body(decorated),
        {"role": "admin"},
        channel_metadata(decorated),
    )

    assert calls == 0
    assert "CHANNEL_RAW_PROMPT_UNAVAILABLE" in result
    assert "decorated Channel prompt" in result


@pytest.mark.parametrize(
    "raw_content",
    [
        f"Before <@M:{MODEL_ID}|Codex> ambiguous",
        "<@M:another-model|Codex> wrong model",
        f"<@M:{MODEL_ID}|Different Label> wrong label",
        f"<@M:{MODEL_ID}|Codex> prompt <@M:another-model|Other> extra mention",
    ],
)
@pytest.mark.asyncio
async def test_ambiguous_channel_mentions_fail_closed(raw_content: str) -> None:
    decorated = "Example Operator: decorated model prompt"

    with pytest.raises(IntegrationError) as raised:
        await extract_invocation_prompt(
            channel_body(decorated),
            channel_metadata(decorated),
            FakeChannelRequest({"content": raw_content, "data": {"files": []}}),
        )

    assert raised.value.code == "CHANNEL_RAW_PROMPT_UNAVAILABLE"


@pytest.mark.parametrize(
    ("metadata", "channel_request_obj"),
    [
        ({"chat_id": f"channel:{CHANNEL_ID}"}, None),
        (
            {
                "chat_id": f"channel:{CHANNEL_ID}",
                "session_id": "channel:different-channel",
                "message_id": "assistant-message-example",
            },
            channel_request("Exact prompt"),
        ),
        (
            {
                "chat_id": f"channel:{CHANNEL_ID}",
                "session_id": f"channel:{CHANNEL_ID}",
            },
            channel_request("Exact prompt"),
        ),
        (
            channel_metadata("decorated"),
            FakeChannelRequest({"data": {"files": []}}),
        ),
        (
            channel_metadata("decorated"),
            FakeChannelRequest(ValueError("body unavailable")),
        ),
    ],
)
@pytest.mark.asyncio
async def test_missing_or_malformed_channel_source_fails_closed(
    metadata: dict[str, Any], channel_request_obj: FakeChannelRequest | None
) -> None:
    with pytest.raises(IntegrationError) as raised:
        await extract_invocation_prompt(
            channel_body("decorated"), metadata, channel_request_obj
        )

    assert raised.value.code == "CHANNEL_RAW_PROMPT_UNAVAILABLE"


@pytest.mark.parametrize(
    "command",
    [
        approval_command(mention=False),
        approval_command(),
        f"APPROVE {REQUEST_ID} {PROMPT_HASH}",
        f"@CODEX ApPrOvE {REQUEST_ID} {PROMPT_HASH}",
    ],
)
def test_exact_approval_commands_are_parsed(command: str) -> None:
    parsed = parse_approval_command(command)

    assert parsed is not None
    assert parsed.request_id == REQUEST_ID
    assert parsed.prompt_sha256 == PROMPT_HASH


@pytest.mark.parametrize(
    "command",
    [
        "approve",
        f"approve {REQUEST_ID}",
        f"approve {REQUEST_ID} {PROMPT_HASH[:32]}",
        f"approve {REQUEST_ID} {PROMPT_HASH.upper()}",
        f"approve {REQUEST_ID.upper()} {PROMPT_HASH}",
        "approve latest",
        f"approve not-a-uuid {PROMPT_HASH}",
        f"approve {REQUEST_ID} {PROMPT_HASH} and execute this too",
        f"Please run {approval_command()} now",
    ],
)
def test_malformed_or_embedded_approval_commands_are_rejected(command: str) -> None:
    with pytest.raises(IntegrationError) as raised:
        parse_approval_command(command)

    assert raised.value.code == "INVALID_APPROVAL_COMMAND"


@pytest.mark.parametrize(
    "prompt",
    [
        "Review whether we should approve this change.",
        "The approval policy should remain strict.",
        f"Do not disapprove {REQUEST_ID} {PROMPT_HASH} automatically.",
    ],
)
def test_normal_prompts_containing_approve_are_not_commands(prompt: str) -> None:
    assert parse_approval_command(prompt) is None


@pytest.mark.parametrize(
    "command",
    [
        "approve latest",
        f"approve {REQUEST_ID}",
        f"@Codex approve {REQUEST_ID} {PROMPT_HASH[:20]}",
        f"approve {REQUEST_ID} {PROMPT_HASH} with extra text",
    ],
)
@pytest.mark.asyncio
async def test_invalid_approval_command_never_creates_request(command: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    result = await configured_pipe(handler).pipe(
        body(command), {"role": "admin"}, {"user_prompt": command}
    )

    assert calls == 0
    assert "INVALID_APPROVAL_COMMAND" in result


@pytest.mark.asyncio
async def test_normal_prompt_with_approve_still_creates_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=created_response())

    prompt = "Review whether we should approve this change."
    result = await configured_pipe(handler).pipe(
        body(prompt), {"role": "admin"}, {"user_prompt": prompt}, None, reject
    )

    assert [request.url.path for request in requests] == ["/v1/execution-requests"]
    assert "explicitly rejected" in result


@pytest.mark.asyncio
async def test_missing_confirmation_support_leaves_created_request_pending() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=created_response())

    pipe = configured_pipe(handler)
    result = await pipe.pipe(body(), {"role": "admin"}, {"user_prompt": "Exact prompt"})

    assert [request.url.path for request in requests] == ["/v1/execution-requests"]
    assert "pending approval" in result
    assert "Interactive confirmation was unavailable" in result
    assert "No run has started" in result
    assert f"@Codex approve {REQUEST_ID} {PROMPT_HASH}" in result


@pytest.mark.asyncio
async def test_rejection_creates_request_but_never_approves() -> None:
    requests: list[httpx.Request] = []
    confirmation: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=created_response())

    async def capture_and_reject(event: dict[str, Any]) -> bool:
        confirmation.update(event)
        return False

    prompt = "  full exact prompt\nsecond line  "
    pipe = configured_pipe(handler)
    result = await pipe.pipe(
        body(prompt),
        {"role": "admin"},
        {"user_prompt": prompt},
        None,
        capture_and_reject,
    )

    assert [request.url.path for request in requests] == ["/v1/execution-requests"]
    assert json.loads(requests[0].content) == {
        "repositoryId": REPOSITORY_ID,
        "prompt": prompt,
    }
    message = confirmation["data"]["message"]
    assert confirmation["type"] == "confirmation"
    assert REPOSITORY_ID in message
    assert REQUEST_ID in message
    assert PROMPT_HASH in message
    assert f"{len(prompt.encode('utf-8'))} UTF-8 bytes" in message
    assert message.endswith(prompt)
    assert "Codex execution rejected" in result
    assert "explicitly rejected" in result
    assert "confirmation was unavailable" not in result.lower()
    assert REQUEST_ID in result and PROMPT_HASH in result
    assert "remains pending" in result


@pytest.mark.parametrize("result_shape", [None, {}, "yes", 1])
@pytest.mark.asyncio
async def test_unsupported_confirmation_result_leaves_request_pending(
    result_shape: Any,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=created_response())

    async def unsupported(event: dict[str, Any]) -> Any:
        return result_shape

    result = await configured_pipe(handler).pipe(
        body(), {"role": "admin"}, {"user_prompt": "Exact prompt"}, None, unsupported
    )

    assert len(requests) == 1
    assert "pending approval" in result
    assert "rejected" not in result.lower()
    assert "cancelled" not in result.lower()
    assert "not approved by the user" not in result.lower()


@pytest.mark.parametrize("raised_error", [RuntimeError("disconnected"), TimeoutError()])
@pytest.mark.asyncio
async def test_confirmation_failure_leaves_request_pending_without_approval(
    raised_error: Exception,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=created_response())

    async def unavailable(event: dict[str, Any]) -> bool:
        raise raised_error

    prompt = "  full exact prompt\nsecond line  "
    result = await configured_pipe(handler).pipe(
        body(prompt),
        {"role": "admin"},
        {"user_prompt": prompt},
        None,
        unavailable,
    )

    assert [request.url.path for request in requests] == ["/v1/execution-requests"]
    assert "Interactive confirmation was unavailable" in result
    assert "pending approval" in result
    assert "rejected" not in result.lower()
    assert prompt in result
    assert f"{len(prompt.encode('utf-8'))} UTF-8 bytes" in result
    assert REPOSITORY_ID in result and REQUEST_ID in result and PROMPT_HASH in result
    assert f"@Codex approve {REQUEST_ID} {PROMPT_HASH}" in result


@pytest.mark.asyncio
async def test_approval_command_fetches_verifies_and_approves_without_creation() -> (
    None
):
    requests: list[httpx.Request] = []
    statuses: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        statuses.append(event)

    command = approval_command()
    result = await configured_pipe(approval_command_handler(requests)).pipe(
        body(command),
        {"role": "admin"},
        {"user_prompt": command},
        emit,
        None,
    )

    paths = [request.url.path for request in requests]
    assert paths == [
        f"/v1/execution-requests/{REQUEST_ID}",
        f"/v1/execution-requests/{REQUEST_ID}/approve",
        f"/v1/runs/{RUN_ID}/events",
        f"/v1/runs/{RUN_ID}",
    ]
    assert "/v1/execution-requests" not in paths
    approval_request = requests[1]
    assert json.loads(approval_request.content) == {"promptSha256": PROMPT_HASH}
    assert "Codex execution completed" in result
    assert "Finished safely." in result
    assert TOKEN not in result
    assert TOKEN not in json.dumps(statuses)


@pytest.mark.parametrize(
    ("fetched", "expected_code"),
    [
        (
            fetched_request_response(request_id="22222222-2222-4222-8222-222222222222"),
            "APPROVAL_REQUEST_MISMATCH",
        ),
        (
            fetched_request_response(repository_id="another-repository"),
            "APPROVAL_REPOSITORY_MISMATCH",
        ),
        (
            fetched_request_response(prompt_sha256="b" * 64),
            "APPROVAL_HASH_MISMATCH",
        ),
        (
            fetched_request_response(status="failed"),
            "REQUEST_NOT_APPROVABLE",
        ),
        (
            fetched_request_response(prompt=None),
            "INVALID_RUNNER_RESPONSE",
        ),
    ],
)
@pytest.mark.asyncio
async def test_fetched_request_mismatch_never_calls_approval(
    fetched: dict[str, Any], expected_code: str
) -> None:
    requests: list[httpx.Request] = []
    command = approval_command()
    result = await configured_pipe(
        approval_command_handler(requests, fetched=fetched)
    ).pipe(body(command), {"role": "admin"}, {"user_prompt": command})

    assert [request.url.path for request in requests] == [
        f"/v1/execution-requests/{REQUEST_ID}"
    ]
    assert expected_code in result


@pytest.mark.asyncio
async def test_wrong_command_hash_fetches_but_never_approves() -> None:
    requests: list[httpx.Request] = []
    wrong_hash = "b" * 64
    command = f"@Codex approve {REQUEST_ID} {wrong_hash}"
    result = await configured_pipe(approval_command_handler(requests)).pipe(
        body(command), {"role": "admin"}, {"user_prompt": command}
    )

    assert [request.url.path for request in requests] == [
        f"/v1/execution-requests/{REQUEST_ID}"
    ]
    assert "APPROVAL_HASH_MISMATCH" in result


@pytest.mark.asyncio
async def test_missing_request_never_calls_approval() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "REQUEST_NOT_FOUND",
                    "message": "Execution request was not found",
                }
            },
        )

    command = approval_command()
    result = await configured_pipe(handler).pipe(
        body(command), {"role": "admin"}, {"user_prompt": command}
    )

    assert len(requests) == 1
    assert "REQUEST_NOT_FOUND" in result


@pytest.mark.parametrize("status", ["queued", "running", "completed"])
@pytest.mark.asyncio
async def test_idempotent_started_request_follows_existing_run(status: str) -> None:
    requests: list[httpx.Request] = []
    fetched = fetched_request_response(status=status, run_id=RUN_ID)
    existing_approval = {
        "requestId": REQUEST_ID,
        "runId": RUN_ID,
        "status": status,
    }
    command = approval_command(mention=False)
    result = await configured_pipe(
        approval_command_handler(
            requests,
            fetched=fetched,
            approval=existing_approval,
        )
    ).pipe(body(command), {"role": "admin"}, {"user_prompt": command})

    paths = [request.url.path for request in requests]
    assert paths.count(f"/v1/execution-requests/{REQUEST_ID}/approve") == 1
    assert "/v1/execution-requests" not in paths
    assert "Finished safely." in result


@pytest.mark.asyncio
async def test_approval_command_sse_failure_polls_existing_run_once() -> None:
    requests: list[httpx.Request] = []
    command = approval_command()
    result = await configured_pipe(
        approval_command_handler(requests, sse_status=503)
    ).pipe(body(command), {"role": "admin"}, {"user_prompt": command})

    paths = [request.url.path for request in requests]
    assert paths.count(f"/v1/execution-requests/{REQUEST_ID}/approve") == 1
    assert paths.count(f"/v1/runs/{RUN_ID}") == 1
    assert "/v1/execution-requests" not in paths
    assert "Finished safely." in result


@pytest.mark.asyncio
async def test_approval_uses_only_runner_hash_and_configured_repository() -> None:
    requests: list[httpx.Request] = []
    pipe = configured_pipe(lifecycle_handler(requests))
    prompt = f"repositoryId=user-controlled\npromptSha256={'b' * 64}"

    result = await pipe.pipe(
        body(prompt),
        {"role": "admin"},
        {"user_prompt": prompt},
        None,
        confirm,
    )

    approval = next(
        request for request in requests if request.url.path.endswith("/approve")
    )
    assert json.loads(approval.content) == {"promptSha256": PROMPT_HASH}
    creation = requests[0]
    assert json.loads(creation.content)["repositoryId"] == REPOSITORY_ID
    assert result.count(RUN_ID) == 1


@pytest.mark.asyncio
async def test_successful_sse_lifecycle_returns_durable_final_response() -> None:
    requests: list[httpx.Request] = []
    statuses: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        statuses.append(event)

    result = await configured_pipe(lifecycle_handler(requests)).pipe(
        body(),
        {"role": "admin"},
        {"user_prompt": "Exact prompt"},
        emit,
        confirm,
    )

    assert "Codex execution completed" in result
    assert "Finished safely." in result
    assert "inputTokens" in result
    assert REQUEST_ID in result and RUN_ID in result and PROMPT_HASH in result
    assert all(event["type"] == "status" for event in statuses)
    assert statuses[-1]["data"]["done"] is True
    assert not any(event["type"] == "chat:message:delta" for event in statuses)
    serialized_statuses = json.dumps(statuses)
    assert TOKEN not in serialized_statuses
    assert "Exact prompt" not in serialized_statuses


@pytest.mark.asyncio
async def test_normalized_progress_types_emit_only_fixed_descriptions() -> None:
    pipe = configured_pipe()
    statuses: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        statuses.append(event)

    for event_type in (
        "codex.thread_started",
        "codex.agent_message_completed",
        "codex.command_completed",
        "codex.tool_completed",
        "codex.file_change_completed",
        "codex.token_usage",
        "codex.final_response",
    ):
        await pipe._emit_runner_event_status(emit, event_type)

    descriptions = [event["data"]["description"] for event in statuses]
    assert descriptions == [
        "Codex thread identified.",
        "Codex message completed.",
        "Command/tool item completed.",
        "Command/tool item completed.",
        "File-change item completed.",
        "Token usage updated.",
        "Final Codex response received.",
    ]


@pytest.mark.asyncio
async def test_terminal_failure_returns_safe_durable_result() -> None:
    requests: list[httpx.Request] = []
    result = await configured_pipe(
        lifecycle_handler(requests, run_status="failed")
    ).pipe(body(), {"role": "admin"}, {"user_prompt": "Exact prompt"}, None, confirm)

    assert "Status: `failed`" in result
    assert "CODEX_EXECUTION_FAILED" in result
    assert "Codex execution failed" in result
    assert "No automatic retry or cancellation occurred" in result


@pytest.mark.asyncio
async def test_sse_failure_polls_same_run_without_reapproval() -> None:
    requests: list[httpx.Request] = []
    statuses: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        statuses.append(event)

    result = await configured_pipe(lifecycle_handler(requests, sse_status=503)).pipe(
        body(),
        {"role": "admin"},
        {"user_prompt": "Exact prompt"},
        emit,
        confirm,
    )

    paths = [request.url.path for request in requests]
    assert paths.count("/v1/execution-requests") == 1
    assert paths.count(f"/v1/execution-requests/{REQUEST_ID}/approve") == 1
    assert paths.count(f"/v1/runs/{RUN_ID}") == 1
    assert "Finished safely." in result
    assert any(
        "polling the existing run" in event["data"]["description"] for event in statuses
    )


@pytest.mark.asyncio
async def test_end_of_stream_without_terminal_event_falls_back_to_polling() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/execution-requests":
            return httpx.Response(201, json=created_response())
        if request.url.path.endswith("/approve"):
            return httpx.Response(202, json=approval_response())
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                text=sse_event(1, "run.started"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=run_response())

    result = await configured_pipe(handler).pipe(
        body(), {"role": "admin"}, {"user_prompt": "Exact prompt"}, None, confirm
    )

    assert "Finished safely." in result
    assert [request.url.path for request in requests].count(f"/v1/runs/{RUN_ID}") == 1


@pytest.mark.asyncio
async def test_polling_timeout_does_not_claim_cancellation() -> None:
    requests: list[httpx.Request] = []
    pipe = configured_pipe(
        lifecycle_handler(requests, sse_status=503, run_status="running")
    )
    pipe.valves.RUN_TIMEOUT_SECONDS = 1
    pipe._poll_interval_seconds = 0.01

    result = await pipe.pipe(
        body(), {"role": "admin"}, {"user_prompt": "Exact prompt"}, None, confirm
    )

    assert "RUN_WAIT_TIMEOUT" in result
    assert "was not cancelled" in result
    assert [request.url.path for request in requests].count(
        f"/v1/execution-requests/{REQUEST_ID}/approve"
    ) == 1


@pytest.mark.asyncio
async def test_malformed_sse_falls_back_safely_to_existing_run() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/execution-requests":
            return httpx.Response(201, json=created_response())
        if request.url.path.endswith("/approve"):
            return httpx.Response(202, json=approval_response())
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                text="id: 1\ndata: not-json\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=run_response())

    result = await configured_pipe(handler).pipe(
        body(), {"role": "admin"}, {"user_prompt": "Exact prompt"}, None, confirm
    )

    assert "Finished safely." in result
    assert [request.url.path for request in requests].count(
        f"/v1/execution-requests/{REQUEST_ID}/approve"
    ) == 1


@pytest.mark.asyncio
async def test_token_never_appears_in_results_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    result = await configured_pipe(handler).pipe(
        body(), {"role": "admin"}, {"user_prompt": "Exact prompt"}, None, confirm
    )

    assert TOKEN not in result
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_configured_token_in_prompt_is_rejected_before_creation() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    pipe = configured_pipe(handler)
    result = await pipe.pipe(
        body(TOKEN), {"role": "admin"}, {"user_prompt": TOKEN}, None, confirm
    )

    assert calls == 0
    assert TOKEN not in result
    assert "SECRET_IN_PROMPT" in result


@pytest.mark.asyncio
async def test_token_is_redacted_from_runner_final_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/execution-requests":
            return httpx.Response(201, json=created_response())
        if request.url.path.endswith("/approve"):
            return httpx.Response(202, json=approval_response())
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                text=sse_event(1, "run.completed"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=run_response(final_response=TOKEN))

    result = await configured_pipe(handler).pipe(
        body(), {"role": "admin"}, {"user_prompt": "Exact prompt"}, None, confirm
    )

    assert TOKEN not in result
    assert "[redacted]" in result


def test_valve_schema_marks_token_as_password() -> None:
    schema = Pipe.Valves.model_json_schema()
    assert schema["properties"]["RUNNER_TOKEN"]["input"]["type"] == "password"


def test_pipe_display_title_is_codex() -> None:
    import codex_runner

    assert "title: Codex\n" in (codex_runner.__doc__ or "")
    assert "title: Codex Runner" not in (codex_runner.__doc__ or "")


@pytest.mark.asyncio
async def test_every_configuration_failure_returns_a_string() -> None:
    result = await Pipe().pipe(body(), {"role": "admin"}, {}, None, confirm)
    assert isinstance(result, str)
