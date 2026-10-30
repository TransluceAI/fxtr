"""Crash scenarios: a connection that registered a service or entity
raises an inner error mid-flight. Verifies:

- The original exception propagates (it's not swallowed by cleanup).
- The crasher's connection unwinds cleanly (no deadlocks).
- Names registered by the crasher go away on the workstation side.
- Entities already retrieved by other peers stay loadable (the RPC
  layer bridged the entity's lifetime token at call return time).
- Services already retrieved by other peers start raising on call —
  the underlying handle was on the dead connection.
"""

from __future__ import annotations

import anyio
import pytest
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import EchoImpl, EchoProto, Note
from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def _resolve_workstation(session, scope):  # type: ignore[no-untyped-def]
    return await session.remote_service.replace_service_type(WorkstationServiceProto).resolve(scope)


async def test_service_register_then_crash_cleans_up_and_propagates() -> None:
    """A registers a service via ``expose_service``, B retrieves it,
    A raises a RuntimeError inside the ``with`` block. The error
    propagates; A's connection unwinds; the workstation's tracking
    is cleaned up; B's already-resolved ref now fails (the underlying
    handle was on A's now-dead connection)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_b:
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                b_echo: EchoProto | None = None

                with pytest.raises(BaseException) as exc_info:
                    async with connect_rpc(ws.rpc_url) as session_a:
                        ws_a_ref = session_a.remote_service.replace_service_type(
                            WorkstationServiceProto
                        )
                        echo_ref = await session_a.scope.mount(EchoImpl())
                        async with expose_service(
                            ws_a_ref, "the-echo", echo_ref, session_a.scope
                        ) as short_id:
                            retrieved = await ws_b.get_service(short_id)
                            b_echo = await retrieved.replace_service_type(EchoProto).resolve(
                                session_b.scope
                            )
                            assert b_echo is not None
                            assert await b_echo.echo("before") == "echo:before"
                            raise RuntimeError("a crashes")

                # The original RuntimeError should be reachable —
                # possibly wrapped in an ExceptionGroup from a task
                # group somewhere in the CM chain.
                _assert_contains_runtime_error(exc_info.value, "a crashes")

                # Workstation-side cleanup: the registration's name is
                # gone. (The connection-record cleanup happens too;
                # we poll for it because the rpc_endpoint task may
                # still be unwinding.)
                with pytest.raises(Exception):
                    await ws_b.get_service_by_external_name("the-echo")
                with anyio.fail_after(2.0):
                    while len(ws.state.rpc_connections) != 1:
                        await anyio.sleep(0.01)

                # B's already-resolved ref breaks: A's mount is gone
                # so the handle is invalid.
                assert b_echo is not None
                with pytest.raises(Exception):
                    await b_echo.echo("after")


async def test_entity_copy_then_crash_cleans_up_and_consumer_can_still_load() -> None:
    """A copies an entity via ``expose_entity``, B retrieves the
    FxtrID, A raises. The error propagates; A's connection unwinds;
    the workstation forgets the entity; but B can still ``load()``
    because the RPC layer bridged the entity's lifetime token."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_b:
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                b_retrieved_id = None

                with pytest.raises(BaseException) as exc_info:
                    async with connect_rpc(ws.rpc_url) as session_a:
                        ws_a_ref = session_a.remote_service.replace_service_type(
                            WorkstationServiceProto
                        )
                        note = Note(title="hi", body="there").store(session_a.scope)
                        async with expose_entity(
                            ws_a_ref, "the-note", note.entity_id, session_a.scope
                        ) as short_id:
                            b_retrieved_id = await ws_b.get_entity(short_id)
                            loaded = await EntityRef(b_retrieved_id, Note).load(session_b.scope)
                            assert loaded == Note(title="hi", body="there")
                            raise RuntimeError("a crashes")

                _assert_contains_runtime_error(exc_info.value, "a crashes")

                # Workstation-side: name lookup gone, connection
                # record removed.
                with pytest.raises(Exception):
                    await ws_b.get_entity_by_external_name("the-note")
                with anyio.fail_after(2.0):
                    while len(ws.state.rpc_connections) != 1:
                        await anyio.sleep(0.01)

                # But B can still load: the token bridge keeps the
                # entity alive on B's side.
                assert b_retrieved_id is not None
                loaded = await EntityRef(b_retrieved_id, Note).load(session_b.scope)
                assert loaded == Note(title="hi", body="there")


def _assert_contains_runtime_error(exc: BaseException, match: str) -> None:
    """Walk a possibly-grouped exception chain looking for a
    ``RuntimeError`` whose message contains ``match``. Raises an
    ``AssertionError`` with the full structure if not found."""
    seen: list[BaseException] = []

    def _walk(e: BaseException) -> bool:
        seen.append(e)
        if isinstance(e, RuntimeError) and match in str(e):
            return True
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                if _walk(sub):
                    return True
        return False

    if not _walk(exc):
        raise AssertionError(
            f"expected a RuntimeError containing {match!r} in the chain; "
            f"saw: {[(type(e).__name__, str(e)) for e in seen]}"
        )
