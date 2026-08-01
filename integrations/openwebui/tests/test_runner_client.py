import json

import httpx
import pytest

from codex_runner import (
    IntegrationError,
    RunnerClient,
    RunnerClientError,
    normalize_runner_url,
)

TEST_TOKEN = "REPLACE_WITH_RANDOM_TOKEN"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "runner.example.internal:8787",
        "ftp://runner.example.internal",
        "http://operator@localhost",
        "http://runner.example.internal?mode=test",
        "http://runner.example.internal#fragment",
        "http://runner.example.internal/base/../other",
        "http://runner.example.internal:99999",
    ],
)
def test_invalid_runner_urls_are_rejected(value: str) -> None:
    with pytest.raises(IntegrationError):
        normalize_runner_url(value)


def test_runner_url_trailing_slash_is_normalized() -> None:
    assert (
        normalize_runner_url("https://runner.example.internal/base/")
        == "https://runner.example.internal/base"
    )


@pytest.mark.asyncio
async def test_client_sends_expected_request_and_bearer_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "requestId": "request-example",
                "repositoryId": "example-repository",
                "promptSha256": "a" * 64,
                "status": "pending_approval",
                "createdAt": "2026-01-01T00:00:00.000Z",
            },
        )

    async with RunnerClient(
        base_url="http://runner.example.internal:8787",
        token=TEST_TOKEN,
        connect_timeout=5,
        api_timeout=30,
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.create_request("example-repository", " exact prompt ")

    assert requests[0].url.path == "/v1/execution-requests"
    assert requests[0].headers["authorization"] == f"Bearer {TEST_TOKEN}"
    assert json.loads(requests[0].content) == {
        "repositoryId": "example-repository",
        "prompt": " exact prompt ",
    }


@pytest.mark.asyncio
async def test_redirects_are_not_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "https://other.invalid/"})

    async with RunnerClient(
        base_url="http://runner.example.internal:8787",
        token=TEST_TOKEN,
        connect_timeout=5,
        api_timeout=30,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RunnerClientError) as raised:
            await client.get_run("run-example")

    assert raised.value.code == "RUNNER_REDIRECT_REJECTED"
    assert calls == 1


@pytest.mark.asyncio
async def test_structured_runner_error_is_preserved_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "EXECUTION_BUSY",
                    "message": "Another execution is active",
                }
            },
        )

    async with RunnerClient(
        base_url="http://runner.example.internal:8787",
        token=TEST_TOKEN,
        connect_timeout=5,
        api_timeout=30,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RunnerClientError) as raised:
            await client.get_run("run-example")

    assert raised.value.code == "EXECUTION_BUSY"
    assert raised.value.message == "Another execution is active"
    assert TEST_TOKEN not in str(raised.value)


@pytest.mark.asyncio
async def test_token_is_redacted_from_structured_runner_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"code": "FAILED", "message": TEST_TOKEN}},
        )

    async with RunnerClient(
        base_url="http://runner.example.internal:8787",
        token=TEST_TOKEN,
        connect_timeout=5,
        api_timeout=30,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RunnerClientError) as raised:
            await client.get_run("run-example")

    assert TEST_TOKEN not in raised.value.message
    assert "[redacted]" in raised.value.message


@pytest.mark.asyncio
async def test_sse_uses_last_event_id_and_decodes_persisted_events() -> None:
    observed_last_id = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_last_id
        observed_last_id = request.headers.get("last-event-id")
        payloads = []
        for event_id, event_type in [(4, "run.started"), (5, "run.completed")]:
            event = {
                "id": event_id,
                "runId": "run-example",
                "type": event_type,
                "timestamp": "2026-01-01T00:00:00.000Z",
                "data": {},
            }
            payloads.append(
                f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(event)}\n\n"
            )
        return httpx.Response(
            200,
            text=": keepalive\n\n" + "".join(payloads),
            headers={"content-type": "text/event-stream; charset=utf-8"},
        )

    async with RunnerClient(
        base_url="http://runner.example.internal:8787",
        token=TEST_TOKEN,
        connect_timeout=5,
        api_timeout=30,
        transport=httpx.MockTransport(handler),
    ) as client:
        events = [
            event
            async for event in client.stream_events("run-example", last_event_id=3)
        ]

    assert observed_last_id == "3"
    assert [event["id"] for event in events] == [4, 5]
