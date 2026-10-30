"""Tests for NetworkTransportHandle type and its integration with the workflow system."""

from pathlib import Path
from typing import Any

import anyio.abc
import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.network_transport import ExternalNetworkTransportHandle, NetworkTransportHandle
from fxtr_workflow.resolve import (
    collect_cache_slot_args,
    collect_cache_slots,
    compute_cache_key,
    resolve_args,
)
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity, make_dummy_slot

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyConnector:
    """A connector that records connect() calls.

    Returns one end of a socketpair; holds the other end open so that
    SocketStream.from_socket's getpeername() check passes. Call close()
    to clean up peer sockets.
    """

    def __init__(self):
        self.connect_count = 0
        self._peer_sockets: list[Any] = []  # prevent GC closing the other end

    async def connect(self) -> anyio.abc.ByteStream:
        self.connect_count += 1
        import socket

        a, b = socket.socketpair()
        self._peer_sockets.append(b)
        return await anyio.abc.SocketStream.from_socket(a)

    def close(self) -> None:
        for s in self._peer_sockets:
            s.close()
        self._peer_sockets.clear()


def _make_handle(label: str = "test_transport") -> ExternalNetworkTransportHandle:
    key_id = StepKeyEntity(label=label).store().entity_id
    return ExternalNetworkTransportHandle(DummyConnector(), key_id)


# ---------------------------------------------------------------------------
# Basic handle tests
# ---------------------------------------------------------------------------


async def test_handle_identity():
    """Handle identity comes from the key_id."""
    async with FxtrScope():
        key_id = StepKeyEntity(label="my_transport").store().entity_id
        handle = ExternalNetworkTransportHandle(DummyConnector(), key_id)
        assert handle.identity == key_id


async def test_handle_connect():
    """connect() delegates to the underlying connector."""
    async with FxtrScope():
        connector = DummyConnector()
        key_id = StepKeyEntity(label="conn_test").store().entity_id
        handle = ExternalNetworkTransportHandle(connector, key_id)

        stream = await handle.connect()
        assert connector.connect_count == 1
        await stream.aclose()
        connector.close()


# ---------------------------------------------------------------------------
# Cache key integration
# ---------------------------------------------------------------------------


async def test_cache_key_includes_transport_identity():
    """Different transport handles produce different cache keys."""
    async with FxtrScope() as scope:
        step_key = StepKeyEntity(label="caller").store().entity_id
        handle_a = _make_handle("transport_a")
        handle_b = _make_handle("transport_b")

        k1 = compute_cache_key(step_key, (handle_a,), {})
        k2 = compute_cache_key(step_key, (handle_b,), {})
        assert k1.entity_id != k2.entity_id


async def test_cache_key_same_transport_same_key():
    """Same transport handle produces the same cache key."""
    async with FxtrScope() as scope:
        step_key = StepKeyEntity(label="caller").store().entity_id
        handle = _make_handle("transport_x")

        k1 = compute_cache_key(step_key, (handle,), {})
        k2 = compute_cache_key(step_key, (handle,), {})
        assert k1.entity_id == k2.entity_id


async def test_cache_key_transport_mixed_with_other_args():
    """Transport handles mix correctly with other arg types in cache keys."""
    async with FxtrScope() as scope:
        step_key = StepKeyEntity(label="caller").store().entity_id
        handle = _make_handle("api")

        k1 = compute_cache_key(step_key, (handle, "model_a"), {})
        k2 = compute_cache_key(step_key, (handle, "model_b"), {})
        assert k1.entity_id != k2.entity_id


# ---------------------------------------------------------------------------
# Arg resolution
# ---------------------------------------------------------------------------


async def test_resolve_args_passes_through_transport():
    """Transport handles pass through arg resolution unchanged."""
    async with FxtrScope():
        handle = _make_handle("passthrough")
        args, kwargs = await resolve_args((handle, 42), {"t": handle})
        assert args[0] is handle
        assert args[1] == 42
        assert kwargs["t"] is handle


# ---------------------------------------------------------------------------
# Cache slot collection (transport handles should be excluded)
# ---------------------------------------------------------------------------


async def test_collect_cache_slots_skips_transport():
    """Transport handles are not collected as cache slot args."""
    async with FxtrScope():
        handle = _make_handle("skip_test")
        slot = make_dummy_slot("s1")

        slots = collect_cache_slots((handle, slot, "other"), {})
        assert len(slots) == 1
        assert slots[0] is slot


async def test_collect_cache_slot_args_skips_transport():
    """Transport handles don't appear in cache_slot_args paths."""
    async with FxtrScope():
        handle = _make_handle("skip_test")
        slot = make_dummy_slot("s1")

        entries = collect_cache_slot_args((handle, slot), {})
        assert len(entries) == 1
        assert entries[0][0] == "args[1]"


# ---------------------------------------------------------------------------
# End-to-end workflow integration
# ---------------------------------------------------------------------------


async def test_step_receives_transport_handle(tmp_path: Path):
    """A step can receive a transport handle as an arg and use it."""
    received_handle = None

    async def use_transport(ctx: StepContext, transport: NetworkTransportHandle) -> str:
        nonlocal received_handle
        received_handle = transport
        return "done"

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            step_key = StepKeyEntity(label="use_transport").store().entity_id
            step = ep.bind_external_step(use_transport, step_key)

            connector = DummyConnector()
            transport_key = StepKeyEntity(label="api_service").store().entity_id
            handle = ep.bind_network_transport(connector, transport_key)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, handle)
            assert result == "done"
            assert received_handle is handle


async def test_transport_handle_affects_cache_key(tmp_path: Path):
    """Changing the transport handle invalidates the cache."""
    call_count = 0

    async def counting_step(ctx: StepContext, transport: NetworkTransportHandle) -> str:
        nonlocal call_count
        call_count += 1
        return "result"

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First run with transport A
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            step_key = StepKeyEntity(label="counting").store().entity_id
            step = ep.bind_external_step(counting_step, step_key)

            key_a = StepKeyEntity(label="transport_a").store().entity_id
            handle_a = ep.bind_network_transport(DummyConnector(), key_a)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, handle_a)
            assert result == "result"
            assert call_count == 1

    # Second run with same transport — should hit cache
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            step_key = StepKeyEntity(label="counting").store().entity_id
            step = ep.bind_external_step(counting_step, step_key)

            key_a = StepKeyEntity(label="transport_a").store().entity_id
            handle_a = ep.bind_network_transport(DummyConnector(), key_a)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, handle_a)
            assert result == "result"
            assert call_count == 1  # not called again

    # Third run with different transport — cache miss (write-once violation)
    from fxtr_workflow.errors import CacheSlotError

    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            step_key = StepKeyEntity(label="counting").store().entity_id
            step = ep.bind_external_step(counting_step, step_key)

            key_b = StepKeyEntity(label="transport_b").store().entity_id
            handle_b = ep.bind_network_transport(DummyConnector(), key_b)

            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(CacheSlotError):
                await slot.run(step, handle_b)


async def test_validate_result_rejects_transport_handle(tmp_path: Path):
    """Returning a transport handle from a step is a StepAbortError."""
    from fxtr_workflow.errors import StepAbortError

    async def bad_step(ctx: StepContext, transport: NetworkTransportHandle):
        return transport  # not allowed

    db = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    async with SQLiteFxtrNamespace(db, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            step_key = StepKeyEntity(label="bad").store().entity_id
            step = ep.bind_external_step(bad_step, step_key)

            transport_key = StepKeyEntity(label="api").store().entity_id
            handle = ep.bind_network_transport(DummyConnector(), transport_key)

            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(step, handle)
            with pytest.raises(StepAbortError):
                await pending
