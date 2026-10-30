"""Type id + protocol for the experiment demo's driver service.

The marker (host-importable, no-deps) half of ``experiment-demo-helper``:
the wire-stable SERVICE type id the scenario-picker renderer dispatches
on (:data:`EXPERIMENT_DEMO_HELPER_TYPE_ID`) plus the driver protocol.
The concrete impl + mount helper live in
``examples/fxtr_experiment_demo/experiment_demo_helper_service.py``.

The driver's scenario methods build real workflow state through the
same user capability any client uses (``experiment-workflow``), so an
operator can push buttons in the browser and watch the workflow react
— each method returns :data:`~fxtr2.services.service_cap_common.
FxtrServiceData` (a small dict: touched grid paths, statuses, entity
refs for the interesting values) so the panel can show where to look.

Scenario map (each exercising a distinct part of the status
inventory in ``projects/fxtr_experiment/notes/monitoring_ui.md``):

- ``scenario_pipeline`` — data → task → downstream task, three cells
  each: complete / fresh / grounded / targeted.
- ``overwrite_input`` / ``revert_input`` — staleness propagation
  through the pipeline and resurrection on revert.
- ``clear_and_rerun_pipeline`` — the v0 stale-rerun gesture: demands
  fault on stale by design, so this clears the pipeline's non-fresh
  task cells and re-demands the sinks (fresh generations over the
  edited inputs).
- ``scenario_slow`` — a multi-step checkpointed task started in the
  background: running now; crash mid-run → interrupted; ``rerun_slow``
  resumes through the durable checkpoints.
- ``crash_runner`` / ``revive_runner`` — the in-process interruption
  arc: forcibly tear the workflow runner down (cancellation
  mid-whatever-it's-doing; running generations poison to interrupted,
  durable checkpoints survive) and bring a fresh one up over the same
  state. The driver itself survives (it holds the supervisor, not the
  runner), so the panel keeps working; other scenario buttons fail
  with a clear message while the runner is down.
- ``scenario_flaky`` — first demand errors (the fn's one planted
  failure); ``retry_flaky`` re-demands and completes.
- ``scenario_unbound`` / ``bind_extra`` / ``release_extra`` /
  ``rerun_unbound`` — the handle-boundness arc: demand against an
  unbound handle faults → bind → re-demand completes.
- ``scenario_subtree`` — a subtree workspace (``variants``, axis
  ``v``) whose child grids span the subtree's axis: subtree rows in
  the dashboard's tree and flattened-prefix coords in its tables.
- ``scenario_nested`` — a task that creates grids at run time: the
  ``planned`` run opens a ``draft`` data grid and a nested ``polish``
  task grid inside its own internals (internals browsing over
  run-created grids, task grid included).
- ``scenario_unsettled`` — the ``echoed`` task reads ONLY ``shout``'s
  output; after ``overwrite_input``, its *b* cell is *unsettled*
  (observations match, support stale) rather than stale.
- ``scenario_rail`` — a task INSIDE a ``sweep`` subtree that vmaps the
  top-level ``inputs`` over ``x``, so the ``x`` axis plate spans two
  parents (root + the subtree) and renders in the plate view as two
  boxes tied by a rail (a non-laminar grant-class).
"""

from __future__ import annotations

import uuid
from typing import Protocol

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import service_protocol

#: Stable namespace UUID for the experiment-demo-helper service type id.
EXPERIMENT_DEMO_HELPER_NS = uuid.UUID("a41c86f0-9e2d-4b73-8c5a-6de1f0b2c397")

#: Wire-stable type id for the driver service. The scenario-picker
#: renderer slice keys off its id-string.
EXPERIMENT_DEMO_HELPER_TYPE_ID: FxtrID = system_entities.define_type(
    EXPERIMENT_DEMO_HELPER_NS, "experiment_demo_helper"
)


@service_protocol
class ExperimentDemoHelperProto(Protocol):
    """The demo driver (see the module docstring's scenario map).

    All methods take no arguments (one button each in the panel) and
    return a small ``FxtrServiceData`` dict. Scenario methods are
    idempotent-ish: opens are create-or-attach and writes rewrite the
    same values, so pressing a button twice is safe.
    """

    async def scenario_pipeline(self) -> FxtrServiceData: ...

    async def clear_and_rerun_pipeline(self) -> FxtrServiceData: ...

    async def overwrite_input(self) -> FxtrServiceData: ...

    async def revert_input(self) -> FxtrServiceData: ...

    async def scenario_slow(self) -> FxtrServiceData: ...

    async def rerun_slow(self) -> FxtrServiceData: ...

    async def crash_runner(self) -> FxtrServiceData: ...

    async def revive_runner(self) -> FxtrServiceData: ...

    async def scenario_flaky(self) -> FxtrServiceData: ...

    async def retry_flaky(self) -> FxtrServiceData: ...

    async def scenario_unbound(self) -> FxtrServiceData: ...

    async def bind_extra(self) -> FxtrServiceData: ...

    async def release_extra(self) -> FxtrServiceData: ...

    async def rerun_unbound(self) -> FxtrServiceData: ...

    async def scenario_subtree(self) -> FxtrServiceData: ...

    async def scenario_nested(self) -> FxtrServiceData: ...

    async def scenario_unsettled(self) -> FxtrServiceData: ...

    async def scenario_rail(self) -> FxtrServiceData: ...
