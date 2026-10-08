import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, cast
from uuid import uuid4

from geo_api.api.errors import error_body
from geo_api.config import Settings

ASGIApp = Callable[
    [dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]
]


class BodyLimitExceeded(Exception):
    pass


class UploadReceiveTimeout(Exception):
    pass


class RequestBoundaryMiddleware:
    """Assign request IDs and bound upload admission before multipart parsing."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.upload_lock = asyncio.Lock()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        response_started = False

        async def identified_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = list(message.get("headers", []))
                path = scope.get("path", "")
                if path in {"/docs", "/redoc"}:
                    content_policy = (
                        "default-src 'self' https://cdn.jsdelivr.net; "
                        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                        "base-uri 'none'"
                    )
                else:
                    content_policy = (
                        "default-src 'self'; object-src 'none'; frame-ancestors 'none'; "
                        "base-uri 'none'"
                    )
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode("ascii")),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"cache-control", b"no-store"),
                        (b"content-security-policy", content_policy.encode("ascii")),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        is_upload = scope.get("method") == "POST" and scope.get("path") in {
            "/api/files",
            "/api/files/",
        }
        if not is_upload:
            await self.app(scope, receive, identified_send)
            return

        if self.upload_lock.locked():
            await self._error(
                identified_send,
                request_id,
                503,
                "UPLOAD_BUSY",
                "Another upload is being processed. Retry shortly.",
                headers=[(b"retry-after", b"5")],
            )
            return

        await self.upload_lock.acquire()
        application_task: asyncio.Task[None] | None = None
        disconnect_task: asyncio.Task[bool] | None = None
        try:
            request_started = time.monotonic()
            body_bytes = 0
            body_complete = asyncio.Event()

            async def bounded_receive() -> dict[str, Any]:
                nonlocal body_bytes
                remaining = self.settings.upload_timeout_seconds - (
                    time.monotonic() - request_started
                )
                if remaining <= 0:
                    raise UploadReceiveTimeout
                try:
                    message = await asyncio.wait_for(receive(), timeout=remaining)
                except TimeoutError as exc:
                    raise UploadReceiveTimeout from exc
                if message["type"] == "http.request":
                    body_bytes += len(message.get("body", b""))
                    if body_bytes > self.settings.body_limit_bytes:
                        raise BodyLimitExceeded
                    if not message.get("more_body", False):
                        body_complete.set()
                return cast(dict[str, Any], message)

            async def watch_disconnect() -> bool:
                await body_complete.wait()
                while True:
                    try:
                        message = await receive()
                    except (ConnectionError, OSError):
                        return True
                    if message["type"] == "http.disconnect":
                        return True

            async def run_application() -> None:
                await self.app(scope, bounded_receive, identified_send)

            application_task = asyncio.create_task(run_application())
            disconnect_task = asyncio.create_task(watch_disconnect())
            try:
                done, _ = await asyncio.wait(
                    {application_task, disconnect_task},
                    timeout=self.settings.request_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if application_task in done:
                    await application_task
                elif disconnect_task in done and disconnect_task.result():
                    if scope["state"].get("publication_started"):
                        await asyncio.wait_for(
                            application_task,
                            timeout=min(
                                self.settings.commit_reconciliation_seconds,
                                max(
                                    0,
                                    self.settings.request_timeout_seconds
                                    - (time.monotonic() - request_started),
                                ),
                            ),
                        )
                    else:
                        await self._cancel(application_task)
                else:
                    await self._cancel(application_task)
                    if not response_started:
                        await self._error(
                            identified_send,
                            request_id,
                            408,
                            "UPLOAD_TIMEOUT",
                            "The upload request exceeded its time limit.",
                        )
            except BodyLimitExceeded:
                if not response_started:
                    await self._error(
                        identified_send,
                        request_id,
                        413,
                        "REQUEST_BODY_TOO_LARGE",
                        "The complete multipart request exceeds the configured body limit.",
                    )
            except UploadReceiveTimeout:
                if not response_started:
                    await self._error(
                        identified_send,
                        request_id,
                        408,
                        "UPLOAD_TIMEOUT",
                        "The upload request exceeded its time limit.",
                    )
            except TimeoutError:
                if not response_started:
                    await self._error(
                        identified_send,
                        request_id,
                        408,
                        "UPLOAD_TIMEOUT",
                        "The upload request exceeded its time limit.",
                    )
        finally:
            if application_task is not None:
                await self._cancel(application_task)
            if disconnect_task is not None:
                await self._cancel(disconnect_task)
            self.upload_lock.release()

    @staticmethod
    async def _cancel(task: asyncio.Task[Any]) -> None:
        if task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _error(
        self,
        send: Callable[..., Awaitable[Any]],
        request_id: str,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(error_body(request_id, code, message), separators=(",", ":")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [(b"content-type", b"application/json"), *(headers or [])],
            }
        )
        await send({"type": "http.response.body", "body": body})
