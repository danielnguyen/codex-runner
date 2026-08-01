import json

import pytest

from codex_runner import RunnerClient, RunnerClientError, SSEParser, parse_sse_chunks


def event_data(event_id: int = 1, event_type: str = "run.started") -> str:
    return json.dumps(
        {
            "id": event_id,
            "runId": "run-example",
            "type": event_type,
            "timestamp": "2026-01-01T00:00:00.000Z",
            "data": {},
        }
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_parser_supports_line_endings_and_comments(newline: str) -> None:
    parser = SSEParser()
    stream = (
        newline.join(
            [": keepalive", "id: 1", "event: run.started", f"data: {event_data()}", ""]
        )
        + newline
    )

    messages = parser.feed(stream) + parser.finish()

    assert len(messages) == 1
    assert messages[0].event_id == "1"
    assert messages[0].event == "run.started"
    assert json.loads(messages[0].data)["type"] == "run.started"


def test_parser_handles_chunked_crlf_and_multiple_data_lines() -> None:
    parser = SSEParser()

    assert parser.feed("id: 4\r") == []
    assert parser.feed("\nevent: sample\r\ndata: {\r\ndata: }\r\n\r") == []
    messages = parser.feed("\n")

    assert messages[0].event_id == "4"
    assert messages[0].data == "{\n}"


def test_parser_does_not_dispatch_incomplete_event_at_eof() -> None:
    parser = SSEParser()
    parser.feed("id: 1\ndata: partial")

    assert parser.finish() == []


@pytest.mark.asyncio
async def test_async_parser_preserves_event_order() -> None:
    async def chunks():
        yield f"id: 1\ndata: {event_data(1)}\n\n"
        yield f"id: 2\ndata: {event_data(2, 'run.completed')}\n\n"

    messages = [message async for message in parse_sse_chunks(chunks())]

    assert [message.event_id for message in messages] == ["1", "2"]


def test_runner_event_rejects_invalid_json() -> None:
    client = RunnerClient(
        base_url="http://runner.example.internal:8787",
        token="REPLACE_WITH_RANDOM_TOKEN",
        connect_timeout=5,
        api_timeout=30,
    )
    message = SSEParser().feed("id: 1\ndata: not-json\n\n")[0]

    with pytest.raises(RunnerClientError, match="malformed event"):
        client._decode_runner_event(message, "run-example")


def test_runner_event_rejects_mismatched_id() -> None:
    client = RunnerClient(
        base_url="http://runner.example.internal:8787",
        token="REPLACE_WITH_RANDOM_TOKEN",
        connect_timeout=5,
        api_timeout=30,
    )
    message = SSEParser().feed(f"id: 9\ndata: {event_data(1)}\n\n")[0]

    with pytest.raises(RunnerClientError, match="identifiers"):
        client._decode_runner_event(message, "run-example")
