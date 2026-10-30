"""Type id + protocol for the behavior scenario's driver service.

The marker (host-importable, no-deps) half of ``behavior-demo-helper``:
the wire-stable SERVICE type id a driver-panel renderer dispatches on
(:data:`BEHAVIOR_DEMO_HELPER_TYPE_ID`) plus the driver protocol. The
concrete impl + mount helper live in
``examples/fxtr_experiment_demo/behavior/behavior_demo_helper_service.py``.

The driver builds the behavior-research workflow (grid namespace +
stage map: the scenario's plan overview under
``projects/fxtr_experiment/plans/extras/extra2_behavior_research_demo/``,
and the impl module's docstring) through the same user capability any
client speaks (``behavior-workflow``). The **stage buttons** walk the
narrative arc; the **run controls** operate the analysis. All methods
take no arguments (one button each) and return a small
``FxtrServiceData`` dict (touched grid paths, cell states, entity refs
for the interesting values); buttons are idempotent-ish — opens are
create-or-attach and rewriting identical values is a no-op success, so
re-pressing is safe. A cell that already holds a *different* value
makes the button error with the write conflict (surfaced in the panel;
replace edited cells explicitly through the capability/UI). Only
``stage_augment`` overwrites (the domain edit is the point), plus
``stage_structure``'s handle cells on a mode switch.

Stages:

- ``stage_structure`` — open every grid (tasks/services/configs/domain/
  evidence + the empty ``analyses/`` subtree) and write the handle keys
  the runner's current mode serves.
- ``stage_inputs`` — write the stage-2 configs (subject models / user
  sims / judges / rollout config) and the matching domain cells.
- ``stage_analysis`` — open ``analyses/sales_report`` wired over the
  judgements' full cross product + the domain cells. Opens only;
  targeting and running are the separate controls below.
- ``stage_augment`` — write the stage-4 configs and **overwrite** the
  domain cells (new subject/sim/judge active, more replicas): the
  analysis goes stale; existing evidence stays fresh.

Run controls:

- ``target_analysis`` — durably target the analysis cell.
- ``run_analysis`` — demand it in the background (the demand pulls
  every needed judgement, which pulls every needed rollout).
- ``clear_stale`` — the v0 stale-rerun gesture: clear every non-fresh
  task cell across ``evidence/`` + ``analyses/`` +
  ``transformations/``.
- ``status`` — one labeled snapshot: per-grid currency counts over the
  domain cross product, the analysis cell's state, and every
  background demand's state.
- ``crash_runner`` / ``revive_runner`` — the in-process interruption
  arc (cancel tracked background demands, tear the runner down, bring
  a fresh one up over the same durable state; re-run to resume through
  the durable checkpoints).

The transformation (`improve_sim` — the one method WITH args, all
required, entered in the panel's input row): builds a pinned-snapshot
``UserSimImprovementRequest`` from the selected evidence (complete
rollout+judgement pairs for the given sim/subject/judge, any
currency), writes it at
``transformations/user_sim_improve[request_id]/request``, and demands
the sibling ``flow`` task in the background — the ABR-style rewrite
flow that trials candidate personas with nested rollout+judge runs in
its own internals.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import service_protocol

#: Stable namespace UUID for the behavior-demo-helper service type id.
BEHAVIOR_DEMO_HELPER_NS = uuid.UUID("763b9474-4487-4358-a6e1-7d8572f72d32")

#: Wire-stable type id for the driver service (a renderer slice keys
#: off its id-string).
BEHAVIOR_DEMO_HELPER_TYPE_ID: FxtrID = system_entities.define_type(
    BEHAVIOR_DEMO_HELPER_NS, "behavior_demo_helper"
)


@service_protocol
class BehaviorDemoHelperProto(Protocol):
    """The behavior scenario's driver (see the module docstring)."""

    async def stage_structure(self) -> FxtrServiceData: ...

    async def stage_inputs(self) -> FxtrServiceData: ...

    async def stage_analysis(self) -> FxtrServiceData: ...

    async def stage_augment(self) -> FxtrServiceData: ...

    async def target_analysis(self) -> FxtrServiceData: ...

    async def run_analysis(self) -> FxtrServiceData: ...

    async def clear_stale(self) -> FxtrServiceData: ...

    async def status(self) -> FxtrServiceData: ...

    async def improve_sim(
        self, sim_id: str, subj_id: str, judge_id: str, instruction: str
    ) -> FxtrServiceData: ...

    async def crash_runner(self) -> FxtrServiceData: ...

    async def revive_runner(self) -> FxtrServiceData: ...
