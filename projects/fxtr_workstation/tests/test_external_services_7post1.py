"""End-to-end tests for stage 7 post-1 user-supplied external services.

Each test opens a user-owned :class:`FxtrScope` (the scope the
workstation will bridge from), mounts a service impl on it, and
hands a :class:`ExternalServiceConfig` to
:class:`WorkstationServer`. RPC peers reach the service through the
standard ``get_service_by_external_name`` path.
"""

from __future__ import annotations

import pathlib

import anyio
import pytest
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workstation import ExternalServiceConfig, PersistenceNamespaceConfig, WorkstationServer
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import SystemSource

from ._fixtures import EchoImpl, EchoProto
from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 30.0


async def _resolve_external_echo(session, scope, external_name: str):  # type: ignore[no-untyped-def]
    """Resolve the workstation main service, then look up the
    external EchoProto cap by external name."""
    ws_ref = session.remote_service.replace_service_type(WorkstationServiceProto)
    workstation = await ws_ref.resolve(scope)
    ref = await workstation.get_service_by_external_name(external_name)
    return await ref.replace_service_type(EchoProto).resolve(scope)


async def test_external_service_round_trip() -> None:
    """One external service: an RPC peer resolves it via
    ``get_service_by_external_name`` and invokes ``echo``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(isolated=True, name="external_owner") as user_scope:
            ref = (await user_scope.mount(EchoImpl())).replace_service_type(EchoProto)
            cfg = ExternalServiceConfig(external_name="echo:test", service=ref, scope=user_scope)
            async with WorkstationServer(external_services=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    echo = await _resolve_external_echo(session, session.scope, "echo:test")
                    assert await echo.echo("hello") == "echo:hello"


async def test_two_external_services_coexist() -> None:
    """Two external services with distinct names. Both are
    independently reachable and invocable."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(isolated=True, name="external_owner_a") as scope_a:
            async with FxtrScope(isolated=True, name="external_owner_b") as scope_b:
                ref_a = (await scope_a.mount(EchoImpl())).replace_service_type(EchoProto)
                ref_b = (await scope_b.mount(EchoImpl())).replace_service_type(EchoProto)
                cfgs = [
                    ExternalServiceConfig(external_name="echo:a", service=ref_a, scope=scope_a),
                    ExternalServiceConfig(external_name="echo:b", service=ref_b, scope=scope_b),
                ]
                async with WorkstationServer(external_services=cfgs) as ws:
                    async with connect_rpc(ws.rpc_url) as session:
                        echo_a = await _resolve_external_echo(session, session.scope, "echo:a")
                        echo_b = await _resolve_external_echo(session, session.scope, "echo:b")
                        assert await echo_a.echo("from-a") == "echo:from-a"
                        assert await echo_b.echo("from-b") == "echo:from-b"


async def test_mixed_external_and_persistence(tmp_path: pathlib.Path) -> None:
    """One persistence cap + one external service co-exist on the
    same workstation. Both reachable; the ``Registration.source``
    on each record reports the right ``SystemSource.kind``
    (``persistence`` vs ``external``)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with SQLiteFxtrNamespace(str(tmp_path / "store.sqlite"), "ns") as ns:
            async with FxtrScope(isolated=True, name="external_owner") as user_scope:
                ref = (await user_scope.mount(EchoImpl())).replace_service_type(EchoProto)
                ext_cfg = ExternalServiceConfig(
                    external_name="echo:ext", service=ref, scope=user_scope
                )
                pers_cfg = PersistenceNamespaceConfig(
                    external_name="persistence:store", namespace=ns
                )
                async with WorkstationServer(
                    persistence_namespaces=[pers_cfg],
                    external_services=[ext_cfg],
                ) as ws:
                    # Both reachable via the standard RPC path.
                    async with connect_rpc(ws.rpc_url) as session:
                        echo = await _resolve_external_echo(session, session.scope, "echo:ext")
                        assert await echo.echo("ping") == "echo:ping"
                        # Persistence is exposed too — just verify the
                        # ref resolves; full pin/retrieve coverage
                        # lives in test_persistence_7.
                        ws_ref = session.remote_service.replace_service_type(
                            WorkstationServiceProto
                        )
                        workstation = await ws_ref.resolve(session.scope)
                        pers_ref = await workstation.get_service_by_external_name(
                            "persistence:store"
                        )
                        assert pers_ref is not None

                    # Each system-sourced registration reports the
                    # right kind on the underlying SystemSource.
                    ext_to_kind: dict[str, str] = {}
                    for tracked in ws.state.service_records.values():
                        for reg in tracked.registrations:
                            if reg.external_name in (
                                "echo:ext",
                                "persistence:store",
                            ):
                                assert isinstance(reg.source, SystemSource)
                                ext_to_kind[reg.external_name] = reg.source.kind
                    assert ext_to_kind == {
                        "echo:ext": "external",
                        "persistence:store": "persistence",
                    }


async def test_duplicate_name_across_external_and_persistence(
    tmp_path: pathlib.Path,
) -> None:
    """An external_services config and a persistence_namespaces
    config sharing an ``external_name`` is rejected at construction
    time by the unified validator."""
    async with SQLiteFxtrNamespace(str(tmp_path / "store.sqlite"), "ns") as ns:
        async with FxtrScope(isolated=True, name="external_owner") as user_scope:
            ref = (await user_scope.mount(EchoImpl())).replace_service_type(EchoProto)
            ext_cfg = ExternalServiceConfig(external_name="dup", service=ref, scope=user_scope)
            pers_cfg = PersistenceNamespaceConfig(external_name="dup", namespace=ns)
            with pytest.raises(ValueError, match="duplicate external name"):
                WorkstationServer(
                    persistence_namespaces=[pers_cfg],
                    external_services=[ext_cfg],
                )


async def test_duplicate_name_within_external_services() -> None:
    """Two external_services configs sharing an ``external_name``
    are rejected at construction time."""
    async with FxtrScope(isolated=True, name="external_owner") as user_scope:
        ref_a = (await user_scope.mount(EchoImpl())).replace_service_type(EchoProto)
        ref_b = (await user_scope.mount(EchoImpl())).replace_service_type(EchoProto)
        cfg_a = ExternalServiceConfig(external_name="dup", service=ref_a, scope=user_scope)
        cfg_b = ExternalServiceConfig(external_name="dup", service=ref_b, scope=user_scope)
        with pytest.raises(ValueError, match="duplicate external name"):
            WorkstationServer(external_services=[cfg_a, cfg_b])
