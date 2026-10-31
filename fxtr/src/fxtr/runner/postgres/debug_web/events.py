"""The viewer's events: what ``/api/events`` sends every open page of the viewer, over a WebSocket.

Each event is one JSON text message with a ``type``:

- ``{"type": "views", "version", "state", "error"}``: the project's view bundle as ``fxtr view``
  rebuilds it (a :class:`~.views_watch.ViewsState`). Every socket gets the current state first, so
  a page that reconnects after a restart catches up. A project with no view bundle sends none.
- ``{"type": "job", "jobId", "workflowName", "resumed"}``: a job was just launched, or resumed when
  it has more than one job attempt. Its launcher announces it with ``POST /api/launches``
  (:func:`launch_request` reads the body; ``fxtr.project.viewer.announce_job`` sends it), and each
  page decides whether to switch to it (``frontend/src/App.tsx``).

:class:`EventHub` fans events out to the open sockets and counts them, which is how a launch learns
whether any page heard it. A socket only sends: it ends when its page goes away, when it falls so
far behind that the hub drops it (its page reconnects and catches up), or when the server shuts
down, which closes every open socket. A WebSocket is not one of the six connections a browser
opens to one origin over HTTP/1.1, so any number of open pages leave those to the pages' requests.
Browsers let any page open a WebSocket to any address, so the socket and the launch route refuse a
page of another origin (:func:`same_origin`).
"""

from __future__ import annotations

import contextlib
import json
import math
import uuid
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from sqlalchemy import func, select
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from fxtr.project.client import JobID
from fxtr.runner.postgres import ledger
from fxtr.runner.postgres.client import LocalProjectClient
from fxtr.runner.postgres.debug_web.views_watch import ViewsState, ViewsWatcher
from fxtr.runner.postgres.schema import job_attempts

QUEUE_SIZE = 64
"""How many events a socket may have waiting to be sent before the hub drops it."""


class EventHub:
    """The open sockets' queues of events to send, and the latest views event, which each socket
    gets first."""

    def __init__(self) -> None:
        self._queues: set[MemoryObjectSendStream[str]] = set()
        self._views: str | None = None

    @property
    def listeners(self) -> int:
        """How many sockets are open."""
        return len(self._queues)

    def publish_views(self, state: ViewsState) -> None:
        """Send the view bundle's state to every socket, and to each later one first."""
        self._views = json.dumps({"type": "views", **state.to_json()})
        self._publish(self._views)

    def publish(self, event: Mapping[str, Any]) -> int:
        """Send ``event`` to every open socket; how many it went to."""
        self._publish(json.dumps(event))
        return self.listeners

    def _publish(self, message: str) -> None:
        for queue in list(self._queues):
            try:
                queue.send_nowait(message)
            except (anyio.WouldBlock, anyio.BrokenResourceError):
                # The page is this far behind (or gone): its socket closes, and a page reconnects
                # to catch up.
                self._queues.discard(queue)
                queue.close()

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[MemoryObjectReceiveStream[str]]:
        """A socket's events while it is open: the current views event, then what is published."""
        send, receive = anyio.create_memory_object_stream[str](max_buffer_size=QUEUE_SIZE)
        if self._views is not None:
            send.send_nowait(self._views)
        self._queues.add(send)
        try:
            with receive:
                yield receive
        finally:
            self._queues.discard(send)
            send.close()


async def follow_views(views: ViewsWatcher, hub: EventHub) -> None:
    """Publish each state of the project's view bundle, the current one first, until cancelled."""
    async for state in views.updates(timeout=math.inf):
        if state is not None:
            hub.publish_views(state)


def same_origin(headers: Mapping[str, str]) -> bool:
    """Whether a request comes from no page (``fxtr``'s launcher sends no ``Origin``) or from a page
    of the host the request was sent to, the viewer's own (through its dev server's proxy too,
    which keeps the ``Host`` the page used)."""
    origin = headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc.lower() == headers.get("host", "").lower()


async def serve_events(websocket: WebSocket, hub: EventHub) -> None:
    """One page's ``/api/events``: every event the hub publishes, until the page goes away. A page
    of another origin is refused before the socket opens."""
    if not same_origin(websocket.headers):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        with hub.subscribe() as events:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_cancel_when_gone, websocket, tg.cancel_scope)
                async for message in events:
                    await websocket.send_text(message)
                tg.cancel_scope.cancel()
        if websocket.client_state == WebSocketState.CONNECTED:
            # The hub dropped this socket for falling behind: its page reconnects.
            await websocket.close(code=1013)
    except* WebSocketDisconnect:
        pass


async def _cancel_when_gone(websocket: WebSocket, scope: anyio.CancelScope) -> None:
    """Read (and ignore) what the page sends, and cancel ``scope`` once it has gone away."""
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass
    scope.cancel()


def launch_request(body: bytes) -> JobID:
    """The job a ``POST /api/launches`` body announces: JSON ``{"jobId": "<job id>"}``.

    Raises:
        ValueError: If the body is not that shape or the id is not a job id.
    """
    try:
        request = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the body is not JSON: {exc}") from exc
    job_id = request.get("jobId") if isinstance(request, dict) else None
    if not isinstance(job_id, str):
        raise ValueError('the body must be {"jobId": "<job id>"}')
    try:
        return JobID(uuid.UUID(job_id))
    except ValueError:
        raise ValueError(f"malformed job id {job_id!r}") from None


async def launch_event(client: LocalProjectClient, job_id: JobID) -> dict[str, Any] | None:
    """The ``job`` event announcing job ``job_id``, ``None`` when the project has no such job."""
    async with client.database.transaction(snapshot=True) as tx:
        try:
            job = await ledger.get_job(tx, job_id)
        except ledger.NotFoundError:
            return None
        count = select(func.count()).select_from(job_attempts)
        attempts = (await tx.execute(count.where(job_attempts.c.job_id == job_id))).scalar_one()
    return {
        "type": "job",
        "jobId": str(job_id),
        "workflowName": job.workflow_name,
        "resumed": attempts > 1,
    }
