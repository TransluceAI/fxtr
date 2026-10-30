"""Type id + protocol for the demo's host-side ``WorkstationDemoHelper``.

This is the marker (host-importable, no-deps) half of the helper: it carries the
wire-stable SERVICE type id the renderer dispatches on
(:data:`DEMO_HELPER_TYPE_ID`) plus the service protocol
(:class:`WorkstationDemoHelperProto`). The concrete impl + mount helper live in
``examples/fxtr_workstation_demo/demo_helper_service.py`` (host process); wire
dispatch is by method name, so the two only need to agree on method shapes.

The helper is a stand-in for "ask an agent to spawn service X and run Y": its
methods do the fiddly host-side plumbing a human can't do from the browser —
``spawn``-by-git-ref + ``connect`` (returning a live service ref), plus entity
factories for invoke args (notes / demo batch inputs). The browser-side driver
renderer (``renderers/demo_helper/``) calls these, then drives the *interesting*
invokes (``echo`` / ``ask`` / ``run_batched``) directly on the returned refs and
presents every result as a clickable ``RefLink``.

All methods return :data:`~fxtr2.services.service_cap_common.FxtrServiceData`
(scalars / entity refs / service refs / dicts thereof) — deliberately loose so
this marker package needs only ``fxtr2`` and the concrete per-service types stay
in the host impl. Frontend renderer dispatch keys off each *returned* service's
own ``_service_type_id()`` (from its metadata), not these annotations.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import service_protocol

#: Stable namespace UUID for the demo-helper service type id.
DEMO_HELPER_NS = uuid.UUID("b7c1e4a2-3d56-4f89-9a0b-1c2d3e4f5a6b")

#: Wire-stable type id for the ``WorkstationDemoHelper`` service. The renderer
#: slice (`renderers/demo_helper/fxtr_renderer.nix`) keys off its id-string,
#: routed through the same type-id table as entities + the other service
#: renderers (`renderers/delayed_echo/`).
DEMO_HELPER_TYPE_ID: FxtrID = system_entities.define_type(DEMO_HELPER_NS, "demo_helper")


@service_protocol
class WorkstationDemoHelperProto(Protocol):
    """Host-side helper for the self-contained demo driver.

    Method groups:

    - **Introspection** — ``workdir_path`` / ``current_commit`` (the runtime
      workdir the spawner resolves git refs against).
    - **Entity factories** — ``make_note`` (one ``presentables.Note`` ref),
      ``make_demo_inputs`` (a small *fixed* named dict of ``Note`` refs), and
      ``make_inputs`` (a named dict built from a caller-supplied list of
      ``{"title", "body"}`` specs — the configurable inputs the streaming driver
      uses to exercise caching, since ``Note`` refs are content-addressed and so
      stable across re-runs). These exist because the browser can't mint entities
      to pass as invoke args.
    - **Spawn + connect** — one method per scenario; each spawns the sandboxed
      service(s) by committed git ref and ``connect``s them, returning the live
      ref(s). Methods needing two services (a scheduler + a task) return a
      ``{"scheduler": ref, "task": ref}`` dict. Methods that need an LLM
      authorizer raise a clear error when the backend wasn't launched with
      ``--allow-llm-apis`` (the ``anthropic-authorizer`` is absent).
    """

    async def workdir_path(self) -> str: ...

    async def current_commit(self) -> str: ...

    async def make_note(self, title: str, body: str) -> FxtrServiceData: ...

    async def make_demo_inputs(self) -> FxtrServiceData: ...

    async def make_inputs(self, notes: FxtrServiceData) -> FxtrServiceData: ...

    async def spawn_delayed_echo(self, label: str) -> FxtrServiceData: ...

    async def spawn_simple_workflow(self) -> FxtrServiceData: ...

    async def spawn_streaming_workflow(self) -> FxtrServiceData: ...

    async def spawn_haiku(self) -> FxtrServiceData: ...

    async def spawn_vivid_workflow(self) -> FxtrServiceData: ...


__all__ = [
    "DEMO_HELPER_NS",
    "DEMO_HELPER_TYPE_ID",
    "WorkstationDemoHelperProto",
]
