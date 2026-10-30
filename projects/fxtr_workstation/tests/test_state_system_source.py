"""Unit tests for ``SystemSource`` (stage 7a).

Stage 7a widens ``Registration.source`` from ``RPCConnectionRecord``
to ``RPCConnectionRecord | SystemSource`` so workstation-internal
service mounts (stage-7 persistence caps, future built-ins) can ride
the existing tracked-service registration machinery. These tests
exercise the new union directly on a live ``WorkstationServer``:

- ``release_registration`` cleanly drops a ``SystemSource``
  registration without touching any RPC connection.
- The services snapshot builder partitions registrations by source —
  RPC connection IDs land in ``registering_connection_ids``, system
  kinds land in ``system_source_kinds``.
- ``describe_registration_source`` produces a uniform error-message
  fragment for both source kinds.

End-to-end persistence-cap behaviour lands in stage 7b.
"""

from __future__ import annotations

import anyio
from fxtr_workstation import WorkstationServer, expose_service
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import (
    Registration,
    SystemSource,
    _build_service_record,
    describe_registration_source,
    release_registration,
)

from ._fixtures import EchoImpl
from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 10.0


def test_system_source_construct() -> None:
    """``SystemSource`` is a frozen dataclass; construction with
    just ``kind`` works, ``metadata`` defaults to an empty dict, and
    direct attribute mutation raises (frozen contract)."""
    src = SystemSource(kind="persistence")
    assert src.kind == "persistence"
    assert src.metadata == {}

    src2 = SystemSource(kind="persistence", metadata={"external_name": "p:default"})
    assert src2.metadata == {"external_name": "p:default"}

    # Frozen contract: assignment raises FrozenInstanceError.
    import dataclasses

    try:
        src.kind = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("expected FrozenInstanceError")


def test_describe_registration_source_formats() -> None:
    """RPC sources render with their connection UUID; system sources
    render with their kind. Both used in external-name conflict
    messages."""
    sys_src = SystemSource(kind="persistence")
    assert describe_registration_source(sys_src) == "system source 'persistence'"


async def test_release_registration_for_system_source() -> None:
    """A ``Registration`` with a ``SystemSource`` source can be
    released cleanly. ``release_registration`` skips the
    per-connection list maintenance (there is no connection) but
    still:

    - removes the registration from ``tracked.registrations``;
    - clears the ``service_external_names`` index entry;
    - releases the underlying record (no other holders) and triggers
      its ``shutdown_event``.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session:
                ws_ref = session.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await session.scope.mount(EchoImpl())

                # ``expose_service`` keeps the RPC-sourced registration
                # alive for the duration of the inner block (and
                # releases it cleanly on exit, sidestepping the
                # ``register_service`` cycle).
                async with expose_service(
                    ws_ref, "system-source-test", echo_ref, session.scope
                ) as short_id:
                    # Build a parallel SystemSource registration that
                    # points at the same ``TrackedServiceRecord``
                    # (different external name — exclusivity is
                    # per-name).
                    tracked = next(
                        t for t in ws.state.service_records.values() if t.short_id == short_id
                    )
                    sys_reg = Registration(
                        service_key=tracked.service_key,
                        source=SystemSource(kind="persistence", metadata={"namespace": "test"}),
                        external_name="persistence:test",
                    )
                    tracked.registrations.append(sys_reg)
                    ws.state.service_external_names["persistence:test"] = sys_reg

                    # Snapshot reports both sources.
                    record = _build_service_record(ws.state, tracked)
                    assert record["external_names"] == ["system-source-test", "persistence:test"]
                    assert len(record["registering_connection_ids"]) == 1
                    assert record["system_source_kinds"] == ["persistence"]

                    # Release the system-sourced registration. Should
                    # drop its external-name index entry and the entry
                    # in ``tracked.registrations``, but leave the RPC
                    # reg alone (so the record stays alive).
                    release_registration(ws.state, sys_reg)
                    assert sys_reg.released
                    assert "persistence:test" not in ws.state.service_external_names
                    assert sys_reg not in tracked.registrations
                    # RPC registration is still there → record alive.
                    assert ws.state.service_records.get(tracked.service_key) is tracked
                    # And the system_source_kinds field is now empty.
                    record_after = _build_service_record(ws.state, tracked)
                    assert record_after["system_source_kinds"] == []
                    assert len(record_after["registering_connection_ids"]) == 1


async def test_release_registration_for_solo_system_source_drops_record() -> None:
    """If a ``TrackedServiceRecord``'s only holder is a
    ``SystemSource`` registration (no RPC peers, no session pins),
    releasing that registration drops the record and fires its
    ``shutdown_event`` so the underlying scope drains."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session:
                ws_ref = session.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await session.scope.mount(EchoImpl())

                # Mint the record via an RPC ``expose_service`` block,
                # then inject a SystemSource registration alongside,
                # then exit the block (RPC reg released → record
                # held only by sys_reg). Finally, release sys_reg
                # and assert the record drops.
                async with expose_service(
                    ws_ref, "soon-to-be-system-only", echo_ref, session.scope
                ) as short_id:
                    tracked = next(
                        t for t in ws.state.service_records.values() if t.short_id == short_id
                    )
                    sys_reg = Registration(
                        service_key=tracked.service_key,
                        source=SystemSource(kind="persistence"),
                        external_name="persistence:solo",
                    )
                    tracked.registrations.append(sys_reg)
                    ws.state.service_external_names["persistence:solo"] = sys_reg

                # ``expose_service`` released the RPC reg on exit;
                # record is still alive (held by sys_reg).
                assert ws.state.service_records.get(tracked.service_key) is tracked
                assert not tracked.shutdown_event.is_set()
                assert len(tracked.registrations) == 1
                assert tracked.registrations[0] is sys_reg

                # Drop the system source registration. Record drops
                # and shutdown_event fires.
                release_registration(ws.state, sys_reg)
                assert tracked.shutdown_event.is_set()
                assert ws.state.service_records.get(tracked.service_key) is None
                assert "persistence:solo" not in ws.state.service_external_names
