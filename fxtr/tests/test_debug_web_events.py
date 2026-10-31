"""The viewer's events (``debug_web/events.py``): the hub that fans them out to the open pages, the
``/api/events`` WebSocket, and ``POST /api/launches``, through which a launch tells the open pages
about its job (a resumed one as resumed)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import anyio
import pytest

pytest.importorskip("starlette")

from asgi_testing import PAGE_ORIGIN, a_page_socket, a_request  # noqa: E402
from postgres_testing import a_client  # noqa: E402
from starlette.types import Message  # noqa: E402
from starlette.websockets import WebSocket  # noqa: E402

from fxtr.entity_defns.array import Array  # noqa: E402
from fxtr.experiment.handles import ArrayHandle  # noqa: E402
from fxtr.experiment.workflows import WorkflowContext, workflow  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.debug_web.app import create_app  # noqa: E402
from fxtr.runner.postgres.debug_web.events import (  # noqa: E402
    QUEUE_SIZE,
    EventHub,
    launch_request,
    same_origin,
    serve_events,
)
from fxtr.runner.postgres.debug_web.views_watch import ViewsState  # noqa: E402

pytestmark = pytest.mark.anyio

FAIL = [False]


@pytest.fixture(autouse=True)
def reset_failure() -> None:
    FAIL[0] = False


@workflow(inputs={"x": int}, output=int)
async def echo(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return x


@workflow(inputs={"x": int}, output=int)
async def flaky(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    if FAIL[0]:
        raise RuntimeError("failing until told not to")
    return x


def one() -> dict[str, Array[Any]]:
    return {"x": Array.scalar(1, int)}


# ---- the hub ----


async def test_a_socket_gets_the_current_views_state_then_what_is_published() -> None:
    hub = EventHub()
    hub.publish_views(ViewsState(version="v1", state="ready"))
    with hub.subscribe() as first:
        assert hub.listeners == 1
        assert hub.publish({"type": "job", "jobId": "j"}) == 1
        hub.publish_views(ViewsState(version="v2", state="building"))
        assert [json.loads(first.receive_nowait()) for _ in range(3)] == [
            {"type": "views", "version": "v1", "state": "ready", "error": None},
            {"type": "job", "jobId": "j"},
            {"type": "views", "version": "v2", "state": "building", "error": None},
        ]
        with hub.subscribe() as second:  # a later socket starts at the latest state
            assert json.loads(second.receive_nowait())["version"] == "v2"
            assert hub.listeners == 2
    assert hub.listeners == 0
    assert hub.publish({"type": "job", "jobId": "j"}) == 0


async def test_a_socket_that_falls_behind_is_dropped() -> None:
    hub = EventHub()
    with hub.subscribe() as behind:
        for n in range(QUEUE_SIZE):
            hub.publish({"n": n})
        assert hub.listeners == 1
        assert hub.publish({"n": QUEUE_SIZE}) == 0  # one too many: dropped
        # What was queued still arrives, then the events end and its socket closes.
        assert [json.loads(message)["n"] async for message in behind] == list(range(QUEUE_SIZE))


async def test_a_disconnect_during_a_send_is_handled_and_unsubscribed() -> None:
    connected = False

    async def receive() -> Message:
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await anyio.sleep_forever()
        raise AssertionError("the socket reader should have been cancelled")

    async def send(message: Message) -> None:
        if message["type"] == "websocket.send":
            raise OSError("the page closed during the send")

    socket = WebSocket({"type": "websocket", "headers": []}, receive, send)
    hub = EventHub()
    hub.publish_views(ViewsState(version="v1", state="ready"))
    with anyio.fail_after(1):
        await serve_events(socket, hub)
    assert hub.listeners == 0


def test_only_the_viewers_own_pages_or_no_page_are_heard() -> None:
    assert same_origin({"host": "127.0.0.1:8000"})  # a launcher sends no Origin
    assert same_origin({"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000"})
    assert same_origin({"host": "Localhost:18000", "origin": "http://localhost:18000"})
    assert not same_origin({"host": "127.0.0.1:8000", "origin": "https://example.com"})
    assert not same_origin({"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8001"})


def test_a_launch_names_its_job() -> None:
    job = uuid.uuid4()
    assert launch_request(json.dumps({"jobId": str(job)}).encode()) == job
    for bad in [b"not json", b"[]", b'{"job": "x"}', b'{"jobId": 1}', b'{"jobId": "not-a-uuid"}']:
        with pytest.raises(ValueError):
            launch_request(bad)


# ---- through the app ----


def unused_database() -> Database:
    return Database("postgresql+asyncpg://unused", pg_schema="unused")


async def announce(app: Any, job_id: object, **headers: str) -> tuple[int, bytes]:
    """``POST /api/launches`` as a launcher sends it, with ``headers`` added or replaced."""
    status, _, body = await a_request(
        app,
        "/api/launches",
        method="POST",
        body=json.dumps({"jobId": str(job_id)}).encode(),
        headers={"content-type": "application/json", **headers},
    )
    return status, body


async def test_the_socket_opens_for_any_project_but_not_for_another_sites_page() -> None:
    async with a_client(unused_database()) as client:
        app = create_app(client)
        assert client.project.views is None
        async with a_page_socket(app) as page:
            assert page.accepted
            assert await page.quiet_for(0.2)  # no view bundle, so no views event
        async with a_page_socket(app, origin=None) as page:
            assert page.accepted
        async with a_page_socket(app, origin="https://example.com") as page:
            assert not page.accepted


async def test_a_launch_is_json_from_no_page_or_the_viewers_own() -> None:
    async with a_client(unused_database()) as client:
        app = create_app(client)
        job = uuid.uuid4()
        assert (await announce(app, job, **{"content-type": "text/plain"}))[0] == 415
        assert (await announce(app, job, origin="https://example.com"))[0] == 403
        status, body = await announce(app, "not a job id")
        assert status == 400 and b"malformed job id" in body
        status, _, _ = await a_request(app, "/api/launches")
        assert status == 405


async def test_a_launch_reaches_every_open_page(database: Database) -> None:
    async with a_client(database, echo) as client:
        job = (await client.run(echo, one())).job_id
        app = create_app(client)
        assert await announce(app, job) == (200, b'{"listeners":0}')
        async with a_page_socket(app) as first, a_page_socket(app) as second:
            assert await announce(app, job, origin=PAGE_ORIGIN) == (200, b'{"listeners":2}')
            for page in (first, second):
                assert await page.next_event() == {
                    "type": "job",
                    "jobId": str(job),
                    "workflowName": echo.name,
                    "resumed": False,
                }
            status, body = await announce(app, uuid.uuid4())
            assert status == 404 and b"no job" in body
            assert await first.quiet_for(0.1)


async def test_a_resumed_job_is_announced_as_resumed(database: Database) -> None:
    FAIL[0] = True
    async with a_client(database, flaky) as client:
        failed = await client.run(flaky, one())
        assert failed.failed
        app = create_app(client)
        async with a_page_socket(app) as page:
            await announce(app, failed.job_id)
            assert (await page.next_event())["resumed"] is False
            FAIL[0] = False
            handle = await client.resume(failed.job_id)
            with anyio.fail_after(15):
                assert (await handle.wait()).state == "succeeded"
            await announce(app, failed.job_id)
            assert (await page.next_event())["resumed"] is True
