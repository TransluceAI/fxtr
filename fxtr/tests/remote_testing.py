"""Helpers for testing the remote worker session in process: a connected pair of bytes-message
channels over anyio memory streams, and a runner that serves a session over one end to a worker
on the other."""

from __future__ import annotations

import math
from typing import Any

import anyio
from anyio.abc import ObjectStream
from anyio.streams.stapled import StapledObjectStream

from fxtr.experiment.registry import FunctionRegistry
from fxtr.runner.experimental.multiplexed import WorkerSession
from fxtr.runner.experimental.remote.proxy import RemoteWorkerSession
from fxtr.runner.experimental.remote.serve import serve_session
from fxtr.runner.experimental.worker import Worker


def memory_channel_pair() -> tuple[ObjectStream[bytes], ObjectStream[bytes]]:
    """Two connected channel ends: what one sends, the other receives. Unbounded."""
    a_send, a_receive = anyio.create_memory_object_stream[bytes](math.inf)
    b_send, b_receive = anyio.create_memory_object_stream[bytes](math.inf)
    return StapledObjectStream(a_send, b_receive), StapledObjectStream(b_send, a_receive)


async def run_worker_over_wire(
    session: WorkerSession,
    registry: FunctionRegistry,
    *,
    max_concurrency: int = 8,
    pipeline_stores: bool = True,
    worker_kwargs: dict[str, Any] | None = None,
) -> None:
    """Serve ``session`` over a memory channel and run a :class:`Worker` against a
    :class:`RemoteWorkerSession` on the other end, until the worker returns."""
    host_end, worker_end = memory_channel_pair()
    async with anyio.create_task_group() as tg:
        tg.start_soon(serve_session, host_end, session)
        async with RemoteWorkerSession(worker_end, pipeline_stores=pipeline_stores) as remote:
            await Worker(
                remote, registry, max_concurrency=max_concurrency, **(worker_kwargs or {})
            ).run()
