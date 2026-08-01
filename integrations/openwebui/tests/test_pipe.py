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
)

REQUEST_ID = "request-example"
RUN_ID = "run-example"
PROMPT_HASH = "a" * 64
TOKEN = "REPLACE_WITH_RANDOM_TOKEN"
REPOSITORY_ID = "example-repository"


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

    assert "not approved" in result
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
async def test_confirmation_unavailable_prevents_request_creation() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    pipe = configured_pipe(handler)
    result = await pipe.pipe(body(), {"role": "admin"}, {"user_prompt": "Exact prompt"})

    assert "CONFIRMATION_UNAVAILABLE" in result
    assert calls == 0


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
    assert "not approved" in result
    assert REQUEST_ID in result and PROMPT_HASH in result
    assert "not deleted" in result


@pytest.mark.asyncio
async def test_confirmation_error_never_calls_approval() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=created_response())

    async def timeout(event: dict[str, Any]) -> bool:
        raise TimeoutError

    result = await configured_pipe(handler).pipe(
        body(), {"role": "admin"}, {"user_prompt": "Exact prompt"}, None, timeout
    )

    assert len(requests) == 1
    assert "not approved" in result


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


@pytest.mark.asyncio
async def test_every_configuration_failure_returns_a_string() -> None:
    result = await Pipe().pipe(body(), {"role": "admin"}, {}, None, confirm)
    assert isinstance(result, str)
