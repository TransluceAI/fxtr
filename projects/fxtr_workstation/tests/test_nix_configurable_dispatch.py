"""Builder-free unit tests for :class:`NixConfigurableDispatchImpl`.

The nix layer (``eval_preset_table`` / ``build_drv_output``) is monkeypatched
so these run without a nix builder, but everything else is **real**: the impl
is mounted into a scope, a :class:`NixDispatcherProto` proxy is resolved, and
``set_preset`` / ``resolve`` are driven *through the proxy* (the same wire /
typed-service dispatch path a real agent + frontend exercise) rather than by
poking the impl object. Validation reads a real tiny layered store written
with :class:`LayeredFileStoreIsolatedWriter`; the renderer "build" returns a
temp ``.mjs``.

The full nix-backed flow (real ``esbuildRenderer`` + ``mkEntityFromPython``
preset built through ``prepare_workdir_repo``) is the chunk-4 integration
test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import fxtr_workstation.dynamic_dispatch.nix_configurable_dispatch.service_impl as svc_impl
import pytest
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, get_active_scope
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.services.service_dialect import FxtrServiceData
from fxtr2.services.subscription_types import Subscriber
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_nix_runner import Materialized
from fxtr_workstation.dynamic_dispatch import ReloadEverythingNotification, RendererEntity
from fxtr_workstation.dynamic_dispatch.dynamic_dispatch_types import NixDispatcherProto
from fxtr_workstation.dynamic_dispatch.nix_configurable_dispatch import NixConfigurableDispatchImpl


@pytest.fixture(autouse=True)
def _hermetic_nix_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests hermetic: the nix layer (``eval_preset_table`` /
    ``build_drv_outputs``) is monkeypatched per test, but ``set_preset`` still
    enters the dispatcher's builder context. Pin that to a no-op
    :class:`HostBackend` so the tests neither require a configured backend nor
    (under a podman-configured env) start a real builder container."""
    from fxtr_nix_runner import HostBackend

    monkeypatch.setattr(svc_impl, "resolve_backend", lambda *a, **k: HostBackend())


DEADLOCK_TIMEOUT = 30.0
_DISPATCH_UUID = "21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234"  # arbitrary (FullPanel's)
_LAYER_KEY = "t"
_TYPE_DRV = "/drv/typeentity"
_RENDERER_DRV = "/drv/renderer"
_MODULE_BYTES = b"export default { demo: {} };\n"


def _mat(path: Path) -> Materialized:
    """Wrap a host path as a `build_drv_outputs` result (host backend: no
    parent-path remap)."""
    return Materialized(host_path=path, remap=None)


# ---------- Recording subscriber (copied from test_basic_dynamic_dispatch_10c) -


class _RecordingSubscriberImpl(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Records every ``on_event`` + signals a fresh event per call so
    tests can ``await`` arrival without polling."""

    def __init__(self) -> None:
        super().__init__()
        self.notifications: list[Any] = []
        self._signal = anyio.Event()

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("test-recording-subscriber"))

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        self.notifications.append(payload)
        signal = self._signal
        self._signal = anyio.Event()
        signal.set()

    async def wait_for_count(self, target: int) -> None:
        while len(self.notifications) < target:
            event = self._signal
            await event.wait()


# ---------- helpers ---------------------------------------------------------


async def _write_type_layer(out_dir: Path, entity_id: FxtrID, scope: FxtrScope) -> None:
    """Write a single-layer store pinning ``entity_id`` under ``_LAYER_KEY``."""
    async with LayeredFileStoreIsolatedWriter(str(out_dir), parents=[]) as writer:
        await writer.pin_to_this_layer(_LAYER_KEY, entity_id, scope)


def _table_for(type_id_str: str) -> dict[str, Any]:
    """The canned ``eval_preset_table`` result: one type, one dispatch key."""
    return {
        type_id_str: {
            "typeEntityLayerDrv": _TYPE_DRV,
            "typeEntityLayerKey": _LAYER_KEY,
            "byDispatchKey": {
                _DISPATCH_UUID: {"subtypeKey": "default", "rendererDrv": _RENDERER_DRV},
            },
        }
    }


def _builtin_table_for(type_id_str: str, *, builtin_id: str | None = None) -> dict[str, Any]:
    """Canned table for a ``builtin``-typeEntity entry (a primitive FxtrID like
    ``!basic_type``): carries ``idString`` instead of a layer drv. ``builtin_id``
    defaults to ``type_id_str`` (the matching, valid case)."""
    return {
        type_id_str: {
            "builtin": True,
            "idString": builtin_id if builtin_id is not None else type_id_str,
            "byDispatchKey": {
                _DISPATCH_UUID: {"subtypeKey": "default", "rendererDrv": _RENDERER_DRV},
            },
        }
    }


# ---------- tests -----------------------------------------------------------


async def test_set_preset_resolves_mints_and_reloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the resolved proxy: ``set_preset`` builds the table, mints a
    ``RendererEntity`` (loadable bytes), validates the typeEntity, and fires
    the reload; ``resolve`` returns the resolution on a hit and ``None`` on a
    miss."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="test") as scope:
            # The stand-in "type entity": a stored entity whose id_string is
            # both the table key and what the typeEntity layer pins.
            type_ref = ReloadEverythingNotification().store(scope)
            type_id_str = type_ref.entity_id.id_string()
            layer_dir = tmp_path / "type_layer"
            await _write_type_layer(layer_dir, type_ref.entity_id, scope)
            mjs = tmp_path / "renderer.mjs"
            mjs.write_bytes(_MODULE_BYTES)

            async def fake_eval(repo, commit, attr, flake_path=None):  # noqa: ANN001
                return _table_for(type_id_str)

            async def fake_build(h, drvs: list[str]) -> dict[str, Materialized]:  # noqa: ANN001
                return {d: _mat(layer_dir if d == _TYPE_DRV else mjs) for d in drvs}

            monkeypatch.setattr(svc_impl, "eval_preset_table", fake_eval)
            monkeypatch.setattr(svc_impl, "build_drv_outputs", fake_build)

            async with anyio.create_task_group() as impl_tg:
                impl = NixConfigurableDispatchImpl(scope=scope, repo=tmp_path, impl_tg=impl_tg)
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(NixDispatcherProto)
                listener = _RecordingSubscriberImpl()
                listener_ref = await scope.mount(listener)

                dispatcher = await dispatcher_ref.resolve(scope)
                await dispatcher.subscribe(listener_ref)

                await dispatcher.set_preset("commit1", "fxtr.uiPresets.p")

                # Reload broadcast reached the subscribed listener.
                await listener.wait_for_count(1)
                assert impl.current_ref == ("commit1", "fxtr.uiPresets.p", None)

                # Hit: right type + right dispatch key.
                hit = await dispatcher.resolve(type_ref, _DISPATCH_UUID)
                assert hit is not None
                assert hit.subtype_key == "default"
                loaded = await hit.renderer.load(scope)
                assert loaded.module == _MODULE_BYTES

                # Miss on the dispatch key (known type, unknown key).
                assert await dispatcher.resolve(type_ref, "no-such-uuid") is None
                # Miss on the type (unknown id_string).
                other = RendererEntity(module=b"x").store(scope)
                assert await dispatcher.resolve(other, _DISPATCH_UUID) is None

                impl_tg.cancel_scope.cancel()


async def test_set_preset_reuses_renderer_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ``set_preset`` calls of the same commit build each renderer drv
    only once (cache by ``(commit, rendererDrv)``); the typeEntity is rebuilt
    each time (validation is eager, uncached)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="test") as scope:
            type_ref = ReloadEverythingNotification().store(scope)
            type_id_str = type_ref.entity_id.id_string()
            layer_dir = tmp_path / "type_layer"
            await _write_type_layer(layer_dir, type_ref.entity_id, scope)
            mjs = tmp_path / "renderer.mjs"
            mjs.write_bytes(_MODULE_BYTES)

            renderer_builds = 0

            async def fake_eval(repo, commit, attr, flake_path=None):  # noqa: ANN001
                return _table_for(type_id_str)

            async def fake_build(h, drvs: list[str]) -> dict[str, Materialized]:  # noqa: ANN001
                nonlocal renderer_builds
                if _RENDERER_DRV in drvs:
                    renderer_builds += 1
                return {d: _mat(layer_dir if d == _TYPE_DRV else mjs) for d in drvs}

            monkeypatch.setattr(svc_impl, "eval_preset_table", fake_eval)
            monkeypatch.setattr(svc_impl, "build_drv_outputs", fake_build)

            async with anyio.create_task_group() as impl_tg:
                impl = NixConfigurableDispatchImpl(scope=scope, repo=tmp_path, impl_tg=impl_tg)
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(NixDispatcherProto)
                dispatcher = await dispatcher_ref.resolve(scope)

                await dispatcher.set_preset("commit1", "fxtr.uiPresets.p")
                await dispatcher.set_preset("commit1", "fxtr.uiPresets.p")
                assert renderer_builds == 1  # second call reused the cache

                impl_tg.cancel_scope.cancel()


async def test_set_preset_validation_mismatch_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preset key that doesn't match its built ``typeEntity`` id makes
    ``set_preset`` raise ``RendererMisconfigured`` and leaves the table
    unswapped (whole-preset reject)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="test") as scope:
            # Pin the *wrong* entity in the type layer; key on a different id.
            pinned = ReloadEverythingNotification().store(scope)
            wrong_key = RendererEntity(module=b"unrelated").store(scope).entity_id.id_string()
            layer_dir = tmp_path / "type_layer"
            await _write_type_layer(layer_dir, pinned.entity_id, scope)
            mjs = tmp_path / "renderer.mjs"
            mjs.write_bytes(_MODULE_BYTES)

            async def fake_eval(repo, commit, attr, flake_path=None):  # noqa: ANN001
                return _table_for(wrong_key)

            async def fake_build(h, drvs: list[str]) -> dict[str, Materialized]:  # noqa: ANN001
                return {d: _mat(layer_dir if d == _TYPE_DRV else mjs) for d in drvs}

            monkeypatch.setattr(svc_impl, "eval_preset_table", fake_eval)
            monkeypatch.setattr(svc_impl, "build_drv_outputs", fake_build)

            async with anyio.create_task_group() as impl_tg:
                impl = NixConfigurableDispatchImpl(scope=scope, repo=tmp_path, impl_tg=impl_tg)
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(NixDispatcherProto)
                dispatcher = await dispatcher_ref.resolve(scope)

                with pytest.raises(IRException) as exc_info:
                    await dispatcher.set_preset("commit1", "fxtr.uiPresets.p")
                assert exc_info.value.category == "RendererMisconfigured"
                # Table left untouched — failure rejected the whole preset.
                assert impl._table == {}

                impl_tg.cancel_scope.cancel()


async def test_initialize_autoloads_initial_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``initialize`` applies the configured initial ``(commit, attr)`` so a
    restart comes up with a populated table (the startup auto-load path the
    mount helper drives)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="test") as scope:
            type_ref = ReloadEverythingNotification().store(scope)
            type_id_str = type_ref.entity_id.id_string()
            layer_dir = tmp_path / "type_layer"
            await _write_type_layer(layer_dir, type_ref.entity_id, scope)
            mjs = tmp_path / "renderer.mjs"
            mjs.write_bytes(_MODULE_BYTES)

            async def fake_eval(repo, commit, attr, flake_path=None):  # noqa: ANN001
                return _table_for(type_id_str)

            async def fake_build(h, drvs: list[str]) -> dict[str, Materialized]:  # noqa: ANN001
                return {d: _mat(layer_dir if d == _TYPE_DRV else mjs) for d in drvs}

            monkeypatch.setattr(svc_impl, "eval_preset_table", fake_eval)
            monkeypatch.setattr(svc_impl, "build_drv_outputs", fake_build)

            async with anyio.create_task_group() as impl_tg:
                impl = NixConfigurableDispatchImpl(
                    scope=scope,
                    repo=tmp_path,
                    impl_tg=impl_tg,
                    initial_commit="commit1",
                    initial_attr="fxtr.uiPresets.p",
                )
                await impl.initialize()
                assert impl.current_ref == ("commit1", "fxtr.uiPresets.p", None)
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(NixDispatcherProto)
                dispatcher = await dispatcher_ref.resolve(scope)
                hit = await dispatcher.resolve(type_ref, _DISPATCH_UUID)
                assert hit is not None and hit.subtype_key == "default"

                impl_tg.cancel_scope.cancel()


async def test_initialize_failed_autoload_warns_and_comes_up_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing initial auto-load (here: eval raises) does not crash —
    ``initialize`` swallows it, the table is empty, and ``current_ref``
    records the attempted ref for the debug surface."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="test") as scope:

            async def boom_eval(repo, commit, attr, flake_path=None):  # noqa: ANN001
                raise RuntimeError("boom")

            monkeypatch.setattr(svc_impl, "eval_preset_table", boom_eval)

            async with anyio.create_task_group() as impl_tg:
                impl = NixConfigurableDispatchImpl(
                    scope=scope,
                    repo=tmp_path,
                    impl_tg=impl_tg,
                    initial_commit="commit1",
                    initial_attr="fxtr.uiPresets.p",
                )
                await impl.initialize()  # must not raise
                assert impl._table == {}
                assert impl.current_ref == ("commit1", "fxtr.uiPresets.p", None)

                impl_tg.cancel_scope.cancel()


async def test_set_preset_builtin_entity_skips_layer_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``builtin``-typeEntity entry (primitive FxtrID, e.g. ``!basic_type``)
    resolves through ``set_preset`` with **no** typeEntity layer build/read —
    only the renderer is built — and its key validates against the carried
    idString."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="test") as scope:
            # Stand-in "primitive" type: a stored entity's id_string is both the
            # table key and the builtin idString. (A real primitive like
            # `!basic_type` resolves the same way — resolve only compares ids.)
            type_ref = ReloadEverythingNotification().store(scope)
            type_id_str = type_ref.entity_id.id_string()
            mjs = tmp_path / "renderer.mjs"
            mjs.write_bytes(_MODULE_BYTES)

            async def fake_eval(repo, commit, attr, flake_path=None):  # noqa: ANN001
                return _builtin_table_for(type_id_str)

            async def fake_build(h, drvs: list[str]) -> dict[str, Materialized]:  # noqa: ANN001
                # No typeEntity layer should ever be requested for a builtin.
                assert _TYPE_DRV not in drvs, drvs
                return {d: _mat(mjs) for d in drvs}

            monkeypatch.setattr(svc_impl, "eval_preset_table", fake_eval)
            monkeypatch.setattr(svc_impl, "build_drv_outputs", fake_build)

            async with anyio.create_task_group() as impl_tg:
                impl = NixConfigurableDispatchImpl(scope=scope, repo=tmp_path, impl_tg=impl_tg)
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(NixDispatcherProto)
                dispatcher = await dispatcher_ref.resolve(scope)

                await dispatcher.set_preset("commit1", "fxtr.uiPresets.p")
                assert impl.current_ref == ("commit1", "fxtr.uiPresets.p", None)

                hit = await dispatcher.resolve(type_ref, _DISPATCH_UUID)
                assert hit is not None
                loaded = await hit.renderer.load(scope)
                assert loaded.module == _MODULE_BYTES

                impl_tg.cancel_scope.cancel()


async def test_set_preset_builtin_idstring_mismatch_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``builtin`` entry whose carried idString disagrees with its preset key
    raises ``RendererMisconfigured`` (whole-preset reject), with no layer
    build."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="test") as scope:
            mjs = tmp_path / "renderer.mjs"
            mjs.write_bytes(_MODULE_BYTES)

            async def fake_eval(repo, commit, attr, flake_path=None):  # noqa: ANN001
                return _builtin_table_for("!key_pasted", builtin_id="!entity_says")

            async def fake_build(h, drvs: list[str]) -> dict[str, Materialized]:  # noqa: ANN001
                assert _TYPE_DRV not in drvs, drvs
                return {d: _mat(mjs) for d in drvs}

            monkeypatch.setattr(svc_impl, "eval_preset_table", fake_eval)
            monkeypatch.setattr(svc_impl, "build_drv_outputs", fake_build)

            async with anyio.create_task_group() as impl_tg:
                impl = NixConfigurableDispatchImpl(scope=scope, repo=tmp_path, impl_tg=impl_tg)
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(NixDispatcherProto)
                dispatcher = await dispatcher_ref.resolve(scope)

                with pytest.raises(IRException) as exc_info:
                    await dispatcher.set_preset("commit1", "fxtr.uiPresets.p")
                assert exc_info.value.category == "RendererMisconfigured"
                assert impl._table == {}  # whole-preset reject

                impl_tg.cancel_scope.cancel()
