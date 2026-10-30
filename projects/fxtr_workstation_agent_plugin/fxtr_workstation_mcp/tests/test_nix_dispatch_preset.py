"""Nix-backed integration test for the git/nix UI-preset path.

The full real flow (no fakes): `prepare_workdir_repo` materializes
`basic_test_demo/agent_workdir` into a committed temp repo, then a
`NixConfigurableDispatchImpl` configured with that repo loads
`fxtr.uiPresets.demo_preset` via `set_preset` — eval'ing the
routing table, building the real `esbuildRenderer` ESM + the
`mkEntityFromPython` `typeEntity` layer on the nix builder, validating the
key, minting a `RendererEntity`, and broadcasting the reload. We drive it
through a resolved `NixDispatcherProto` proxy (the agent's wire path) and
through `resolve` (the frontend's wire path).

Slow + nix-gated: needs a reachable x86_64-linux builder. The heaviest cost
is the `demo_data` host venv (full fxtr2 via uv2nix) the `typeEntity` builds
on — cold that's minutes, hence the generous deadline; cached it's fast.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, EntityRef, FxtrScope, get_active_scope
from fxtr2.services.service_dialect import FxtrServiceData
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_workdir_harness import prepare_workdir_repo
from fxtr_workstation.dynamic_dispatch import (
    NixConfigurableDispatchImpl,
    NixDispatcherProto,
    RendererEntity,
)
from fxtr2.services.subscription_types import Subscriber

pytestmark = [pytest.mark.anyio, pytest.mark.nix_runtime]

# Paths + the agent workdir's demo_data package (deterministic type id, so the
# in-repo source and the prepared-copy flake agree on the preset key).
_TESTS = Path(__file__).resolve().parent
_AGENT_WORKDIR = _TESTS.parent.parent / "examples" / "basic_test_demo" / "agent_workdir"
sys.path.insert(0, str(_AGENT_WORKDIR / "packages" / "demo_data" / "src"))

from demo_data import DemoDataEntity  # noqa: E402

# The four shell dispatch-key UUIDs the demo preset routes (see the flake +
# `renderers/demo_data/index.tsx`).
_FULL_PANEL = "21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234"


class _RecordingSubscriberImpl(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Records every ``on_event`` + signals a fresh event per call so the
    test can ``await`` the reload without polling. (Copy of the unit-test
    helper.)"""

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


@pytest.mark.slow
@pytest.mark.deadline(seconds=900)
async def test_nix_preset_set_preset_resolves_mints_and_reloads(tmp_path: Path) -> None:
    prepared = await prepare_workdir_repo(_AGENT_WORKDIR, tmp_path / "agent_workdir")

    async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="test") as scope:
        # Store a DemoDataEntity instance so its type-descriptor closure (whose
        # id IS the type id) is tracked in `scope` — makes the type-ref below
        # wire-encodable as a resolve arg. The id_string also matches the
        # flake's hardcoded preset key (both from define_type, deterministic).
        DemoDataEntity(title="probe", body="probe").store(scope)
        type_ref = EntityRef(DemoDataEntity.fxtr_type, DemoDataEntity)

        async with anyio.create_task_group() as impl_tg:
            impl = NixConfigurableDispatchImpl(scope=scope, repo=prepared.path, impl_tg=impl_tg)
            dispatcher_ref = (await scope.mount(impl)).replace_service_type(NixDispatcherProto)
            listener = _RecordingSubscriberImpl()
            listener_ref = await scope.mount(listener)

            dispatcher = await dispatcher_ref.resolve(scope)
            await dispatcher.subscribe(listener_ref)

            # The real build + validate + mint + swap + reload.
            await dispatcher.set_preset(prepared.head_commit, "fxtr.uiPresets.demo_preset")
            await listener.wait_for_count(1)
            assert impl.current_ref == (
                prepared.head_commit,
                "fxtr.uiPresets.demo_preset",
                None,
            )

            # Resolve the demo_data type for FullPanel: a real RendererEntity
            # built from the esbuildRenderer ESM, loadable to real ESM bytes.
            hit = await dispatcher.resolve(type_ref, _FULL_PANEL)
            assert hit is not None
            assert hit.subtype_key == "demo_data"
            loaded = await hit.renderer.load(scope)
            assert loaded.module  # non-empty
            assert b"export" in loaded.module  # ESM output

            # Miss on an unknown type → None (NoMatchFallback on the frontend).
            other = RendererEntity(module=b"x").store(scope)
            assert await dispatcher.resolve(other, _FULL_PANEL) is None

            impl_tg.cancel_scope.cancel()
