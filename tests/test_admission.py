from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from geo_api.config import Settings
from geo_api.middleware.admission import RequestBoundaryMiddleware


def _scope() -> dict[str, Any]:
    return {"type": "http", "method": "POST", "path": "/api/files/", "headers": []}


async def _read_once_app(scope: Any, receive: Any, send: Any) -> None:
    while True:
        message = await receive()
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 201, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _run(
    middleware: RequestBoundaryMiddleware,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(_scope(), receive, send)
    return sent


@pytest.mark.asyncio
async def test_streaming_body_limit_applies_without_content_length() -> None:
    settings = Settings(_env_file=None, body_limit_bytes=4)
    middleware = RequestBoundaryMiddleware(_read_once_app, settings)
    sent = await _run(
        middleware,
        [
            {"type": "http.request", "body": b"abcd", "more_body": True},
            {"type": "http.request", "body": b"ef", "more_body": False},
        ],
    )

    assert sent[0]["status"] == 413
    headers = dict(sent[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-request-id"]
    assert json.loads(sent[1]["body"])["error"]["code"] == "REQUEST_BODY_TOO_LARGE"


@pytest.mark.asyncio
async def test_upload_slot_rejects_second_request_and_releases_after_first() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    body_delivered = False

    async def delayed_receive() -> dict[str, Any]:
        nonlocal body_delivered
        if body_delivered:
            return {"type": "http.disconnect"}
        started.set()
        await release.wait()
        body_delivered = True
        return {"type": "http.request", "body": b"x", "more_body": False}

    first_sent: list[dict[str, Any]] = []
    second_sent: list[dict[str, Any]] = []

    async def first_send(message: dict[str, Any]) -> None:
        first_sent.append(message)

    async def second_send(message: dict[str, Any]) -> None:
        second_sent.append(message)

    middleware = RequestBoundaryMiddleware(_read_once_app, Settings(_env_file=None))
    first = asyncio.create_task(middleware(_scope(), delayed_receive, first_send))
    await started.wait()
    await middleware(
        _scope(),
        lambda: asyncio.sleep(0, result={"type": "http.request", "body": b"", "more_body": False}),
        second_send,
    )

    assert second_sent[0]["status"] == 503
    assert (b"retry-after", b"5") in second_sent[0]["headers"]
    release.set()
    await first
    assert first_sent[0]["status"] == 201


@pytest.mark.asyncio
async def test_upload_receive_deadline_is_structured() -> None:
    middleware = RequestBoundaryMiddleware(
        _read_once_app,
        Settings(_env_file=None, upload_timeout_seconds=0),
    )
    sent = await _run(
        middleware,
        [{"type": "http.request", "body": b"", "more_body": False}],
    )

    assert sent[0]["status"] == 408
    assert json.loads(sent[1]["body"])["error"]["code"] == "UPLOAD_TIMEOUT"


@pytest.mark.asyncio
async def test_disconnect_before_publication_cancels_the_processing_task() -> None:
    processing = asyncio.Event()
    cancelled = asyncio.Event()
    messages = [
        {"type": "http.request", "body": b"x", "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        processing.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = RequestBoundaryMiddleware(app, Settings(_env_file=None))
    await middleware(_scope(), receive, send)

    assert processing.is_set()
    assert cancelled.is_set()
    assert sent == []


@pytest.mark.asyncio
async def test_disconnect_during_publication_waits_for_bounded_commit() -> None:
    publication_started = asyncio.Event()
    release_commit = asyncio.Event()
    messages = [
        {"type": "http.request", "body": b"x", "more_body": False},
        {"type": "http.disconnect"},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        scope["state"]["publication_started"] = True
        publication_started.set()
        await release_commit.wait()
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"committed"})

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = RequestBoundaryMiddleware(
        app,
        Settings(_env_file=None, request_timeout_seconds=2, commit_reconciliation_seconds=1),
    )
    request = asyncio.create_task(middleware(_scope(), receive, send))
    await publication_started.wait()
    await asyncio.sleep(0.01)
    assert not request.done()

    release_commit.set()
    await request

    assert sent[0]["status"] == 201
    assert sent[1]["body"] == b"committed"


@pytest.mark.asyncio
async def test_external_cancellation_reaps_the_upload_task_and_releases_the_slot() -> None:
    processing = asyncio.Event()
    cancelled = asyncio.Event()
    receive_count = 0
    app_calls = 0
    middleware: RequestBoundaryMiddleware

    async def receive() -> dict[str, Any]:
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            return {"type": "http.request", "body": b"x", "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal app_calls
        app_calls += 1
        await receive()
        if app_calls == 1:
            processing.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestBoundaryMiddleware(app, Settings(_env_file=None))
    first_sent: list[dict[str, Any]] = []

    async def first_send(message: dict[str, Any]) -> None:
        first_sent.append(message)

    first = asyncio.create_task(middleware(_scope(), receive, first_send))
    await processing.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert cancelled.is_set()

    second_messages = [
        {"type": "http.request", "body": b"x", "more_body": False},
        {"type": "http.disconnect"},
    ]
    second_sent: list[dict[str, Any]] = []

    async def second_receive() -> dict[str, Any]:
        return second_messages.pop(0)

    async def second_send(message: dict[str, Any]) -> None:
        second_sent.append(message)

    await middleware(_scope(), second_receive, second_send)
    assert second_sent[0]["status"] == 201
