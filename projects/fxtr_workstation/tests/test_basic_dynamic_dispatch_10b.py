"""Tests for :mod:`fxtr_workstation.dynamic_dispatch.basic_dynamic_dispatch`.

Three layers:

1. :func:`compile_renderer` round-trip — TSX in, ESM bytes out, the
   external imports survive as bare specifiers.
2. :meth:`BasicDynamicDispatchImpl.compile_and_register` walks
   renderer directories, stores renderer entities in the supplied
   scope, populates the lookup table.
3. End-to-end ``resolve()`` call across the wire from an RPC peer
   into a workstation hosting the impl as an external service.

All tests requiring esbuild are gated on :func:`npx_available`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import anyio
import pytest
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, EntityRef, FxtrScope
from fxtr2.leases.leased_cap_ir import IRException
from fxtr_workstation import ExternalServiceConfig, WorkstationServer
from fxtr_workstation.dynamic_dispatch import (
    BasicDynamicDispatchImpl,
    RendererEntity,
    RendererResolution,
    WorkstationDispatcherProto,
    mount_basic_dynamic_dispatch,
)
from fxtr_workstation.dynamic_dispatch.basic_dynamic_dispatch import compile_renderer, npx_available
from fxtr_workstation.protocols import WorkstationServiceProto

from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 60.0
FIXTURES_DIR = Path(__file__).parent / "_fixtures_basic_dynamic_dispatch"

#: Skip module marker for environments without ``npx`` on PATH.
needs_npx = pytest.mark.skipif(
    not npx_available(),
    reason="npx not on PATH — install Node.js to exercise esbuild compile path.",
)


def _build_renderer_dir_for_type(
    tmp_path: Path,
    type_ref: EntityRef,
    fixture_subdir_name: str,
) -> Path:
    """Materialise a renderer dir whose single subdirectory is named
    after ``type_ref``'s id_string, containing a copy of the fixture
    TSX. Returns the renderer dir (suitable for ``renderer_dirs=[...]``).
    """
    renderer_dir = tmp_path / "renderers"
    type_subdir = renderer_dir / type_ref.entity_id.id_string()
    type_subdir.mkdir(parents=True)
    shutil.copyfile(
        FIXTURES_DIR / "renderers_a" / fixture_subdir_name / "index.tsx",
        type_subdir / "index.tsx",
    )
    return renderer_dir


@needs_npx
async def test_compile_renderer_round_trip() -> None:
    """``compile_renderer`` produces ESM bytes whose `import` of an
    externalized module survives as a bare specifier (not bundled in).
    """
    bytes_ = await compile_renderer(FIXTURES_DIR / "renderers_a" / "type_id_aaa")
    text = bytes_.decode()
    # The compiled module must keep the external as a bare specifier
    # so the frontend's import-map shim can resolve it. If esbuild
    # bundled it, the import would be replaced with the bundled module
    # body (no `from "react/jsx-runtime"` string would survive).
    assert 'from "react/jsx-runtime"' in text, (
        f"react/jsx-runtime import did not survive as bare specifier; "
        f"compiled output:\n{text[:500]}"
    )
    # Smoke-check that the default export shape is preserved.
    assert "export" in text


async def test_compile_renderer_missing_entry(tmp_path: Path) -> None:
    """Missing ``index.tsx`` raises a clear error before invoking the
    compiler — no point round-tripping esbuild for that case."""
    from fxtr_workstation.dynamic_dispatch.basic_dynamic_dispatch import RendererCompileError

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RendererCompileError, match="no index.tsx"):
        await compile_renderer(empty)


@needs_npx
async def test_compile_and_register_populates_table() -> None:
    """``compile_and_register`` walks every renderer dir, compiles
    each subdirectory's ``index.tsx``, stores the resulting
    ``RendererEntity`` in the supplied scope, and populates the
    in-impl table keyed by subdirectory name."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        # include=[GLOBAL_SCOPE] so the RendererEntity type
        # descriptor is reachable for the store walks.
        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="basic_dispatch_test",
        ) as scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=scope,
                    renderer_dirs=[FIXTURES_DIR / "renderers_a"],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                # Direct internal-state check: the table holds both type
                # buckets, each carrying a `RendererResolution` with the
                # default subtype key and a renderer ref loadable from
                # `scope` (because we just stored it there).
                assert set(impl._table.keys()) == {"type_id_aaa", "type_id_bbb"}
                for key, resolution in impl._table.items():
                    assert isinstance(resolution, RendererResolution)
                    assert resolution.subtype_key == "default"
                    loaded = await resolution.renderer.load(scope)
                    assert isinstance(loaded, RendererEntity)
                    assert (
                        b"react/jsx-runtime" in loaded.module
                    ), f"Compiled module for {key} lost its external import"
                impl_tg.cancel_scope.cancel()


async def _get_dispatcher(session, external_name: str):  # type: ignore[no-untyped-def]
    """Resolve the workstation main service, get the dispatcher ref by
    external name, and resolve a typed proxy in ``session.scope``."""
    workstation_proxy = await session.remote_service.replace_service_type(
        WorkstationServiceProto
    ).resolve(session.scope)
    dispatcher_service_ref = await workstation_proxy.get_service_by_external_name(external_name)
    return await dispatcher_service_ref.replace_service_type(WorkstationDispatcherProto).resolve(
        session.scope
    )


@needs_npx
async def test_end_to_end_resolve_via_workstation(tmp_path: Path) -> None:
    """End-to-end: mount the impl as an external service via
    :func:`mount_basic_dynamic_dispatch`, connect an RPC peer, call
    ``resolve(type_ref)``, verify the returned ``RendererResolution``
    carries a loadable renderer entity whose bytes are the compiled
    ESM module from the fixture.

    The renderer table is keyed by id_string of the type entity. We
    materialise a fresh renderer dir at runtime — store an arbitrary
    entity in a scratch scope to read its id_string, then create
    ``<tmp>/renderers/<id_string>/index.tsx`` by copying the fixture
    TSX. The impl walks that dir, compiles the renderer, and
    indexes it under the real id_string — so ``resolve(type_ref)``
    with the same content is a real lookup, not a spliced-in one.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        # Scratch scope just to read the type entity's id_string —
        # used to name the renderer dir. The impl's
        # `mount_basic_dynamic_dispatch` opens its own scope.
        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="type_id_reader",
        ) as scratch_scope:
            type_entity = RendererEntity(module=b"type-stand-in").store(scratch_scope)
            renderer_dir = _build_renderer_dir_for_type(
                tmp_path, type_entity, fixture_subdir_name="type_id_aaa"
            )
            expected_id_string = type_entity.entity_id.id_string()

        async with mount_basic_dynamic_dispatch(
            renderer_dirs=[renderer_dir],
        ) as mounted:
            assert expected_id_string in mounted.impl._table
            cfg = ExternalServiceConfig(
                external_name="renderer-dispatcher",
                service=mounted.service_ref,
                scope=mounted.scope,
            )
            async with WorkstationServer(external_services=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    # We need the type ref in session.scope to ship it
                    # across the wire. Mint a fresh ref pointing at
                    # the same data id; the workstation-side bridge
                    # will recognise the id when the impl looks up
                    # its id_string.
                    wire_type_ref = RendererEntity(module=b"type-stand-in").store(session.scope)
                    assert (
                        wire_type_ref.entity_id.id_string() == expected_id_string
                    ), "Content-addressed ids should match for same content"

                    dispatcher = await _get_dispatcher(session, "renderer-dispatcher")
                    # `BasicDynamicDispatchImpl` ignores `dispatch_key_uuid`
                    # so any string works; pass the empty string to make
                    # that explicit.
                    resolution = await dispatcher.resolve(wire_type_ref, "")
                    assert isinstance(resolution, RendererResolution)
                    assert resolution.subtype_key == "default"
                    loaded = await resolution.renderer.load(session.scope)
                    assert isinstance(loaded, RendererEntity)
                    assert b"react/jsx-runtime" in loaded.module


@needs_npx
async def test_resolve_unknown_type_returns_none() -> None:
    """A ``resolve()`` call for an unknown type-id-string returns
    ``None`` — the frontend surfaces this as the dispatch key's
    ``NoMatchFallback`` rather than as an error."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with mount_basic_dynamic_dispatch(
            renderer_dirs=[FIXTURES_DIR / "renderers_a"],
        ) as mounted:
            cfg = ExternalServiceConfig(
                external_name="renderer-dispatcher",
                service=mounted.service_ref,
                scope=mounted.scope,
            )
            async with WorkstationServer(external_services=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    arbitrary_ref = RendererEntity(module=b"unknown").store(session.scope)
                    dispatcher = await _get_dispatcher(session, "renderer-dispatcher")
                    result = await dispatcher.resolve(arbitrary_ref, "")
                    assert result is None


async def test_resolve_before_register_raises_irexception() -> None:
    """Calling :meth:`BasicDynamicDispatchImpl.resolve` before
    :meth:`compile_and_register` completes is a programming error and
    surfaces as an :class:`IRException` (category
    ``DispatcherNotRegistered``) rather than as a ``None`` no-match
    result."""
    async with FxtrScope(
        isolated=True,
        include=[GLOBAL_SCOPE],
        name="dispatcher_owner",
    ) as user_scope:
        async with anyio.create_task_group() as impl_tg:
            impl = BasicDynamicDispatchImpl(scope=user_scope, renderer_dirs=[], impl_tg=impl_tg)
            dummy_ref = RendererEntity(module=b"unused").store(user_scope)
            with pytest.raises(IRException) as exc_info:
                await impl.resolve(dummy_ref.replace_target_type(UnknownEntity), "")
            assert exc_info.value.category == "DispatcherNotRegistered"
            impl_tg.cancel_scope.cancel()
