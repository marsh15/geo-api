from __future__ import annotations

import asyncio

import pytest

from geo_api.api.routes import _await_publication


@pytest.mark.asyncio
async def test_cancelled_request_waits_for_bounded_publication() -> None:
    started = asyncio.Event()
    release_commit = asyncio.Event()
    committed = asyncio.Event()

    async def publish() -> None:
        started.set()
        await release_commit.wait()
        committed.set()

    request = asyncio.create_task(_await_publication(publish(), reconciliation_seconds=1))
    await started.wait()
    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()

    release_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert committed.is_set()


@pytest.mark.asyncio
async def test_cancelled_request_cancels_publication_after_reconciliation_deadline() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def publish() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    request = asyncio.create_task(_await_publication(publish(), reconciliation_seconds=0))
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert cancelled.is_set()
