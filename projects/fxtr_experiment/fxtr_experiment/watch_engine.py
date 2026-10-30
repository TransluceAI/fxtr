"""The watch engine: server-side recompute-and-diff mirrors over the hint stream.

The delivery half of the watch subscriptions (``notes/event_system.md``;
the wire methods are `user_api.UserCapabilityProto.watch_workflow` /
``watch_cells`` / ``watch_cell``, the event vocabulary `watch_api.py`).
One :class:`WatchEngine` lives per capability mount
(`user_service.UserCapabilityImpl._fxtr_mount_lifespan` wires it to the
session's post-commit hint seam, the binding registry's change callback,
and the run-log registry's append listener).

Per active watch the engine holds the **last-sent state** and a
dedicated queue-mode :class:`~fxtr2.services.subscription_util.
SubscriberHub` (one subscription each — the hub's per-subscription
worker/holder/crash-backstop machinery is exactly the wanted lifecycle;
its FIFO losslessness is what makes diff delivery sound). The worker
loop: hints land in a window buffer → each watch's dirty set grows by
intersection → dirty watches recompute **through the capability's own
wire methods** (`summarize` / `list_cells` / `inspect_cell` / … — the
engine is a pure consumer of the same surface any client drives, minting
its grants through the same `_walk_path` walk) → the fresh result diffs
against last-sent → only the keyed patch publishes. The initial snapshot
is the first diff from the empty mirror (a fresh watch starts fully
dirty), so there is no separate snapshot path and no subscribe/publish
race: over-triggering and re-ordering of hints are harmless by
construction (diff-vs-last-sent is state reconciliation).

Event-native log routing bypasses the diff path: the run-log registry's
listener publishes resets/appends straight to the matching cell watches;
a fresh cell watch gets the buffered backlog immediately after its first
batch (published in the same no-await window that flips the watch live,
so the backlog and the live appends can neither overlap nor gap).

Failure posture: a recompute failure is logged and the watch's dirty set
cleared (the next hint re-triggers; a subscriber that suspects a wedged
mirror resubscribes — the same reconciliation lever as a broken
subscription). A delivery failure kills that subscription via the hub's
crash backstop and the watch is reaped on the next pass.

Payloads carry served ids (config ids, fingerprints, value ids) — the
wire methods the engine recomputes through re-attach them
(``WorkflowSession.ensure_loadable``), and publishes ride the runner
scope as origin, so deliveries carry their own residency per the hub's
origin-lease machinery. The engine holds no standing authorization of
its own; publish-time authorization is the runner scope's scope-level
base lease, valid from any context while the scope is open — the
precise invariant and the drain-window soundness argument are on
`_publish`.

A discipline note the recompute path depends on: every capability
method the engine drives must stay **dispatch-context-free** (session +
registry attributes only; no active-scope capture) — true of the whole
introspection surface today by scope discipline, and required for the
in-process calls here to mean the same thing they mean over the wire.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Iterable

import anyio
import anyio.abc
from frozendict import frozendict

from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.service_dialect import FxtrServiceData, encode_to_service_data
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import SubscriberHub

from fxtr_experiment.change_hints import Hint
from fxtr_experiment.encoding import decode_coords, decode_key_path
from fxtr_experiment_task.runner_api import ViewSel
from fxtr_experiment.user_api import (
    AxisCorrespondence,
    CellInfo,
    CellReport,
    GenerationReport,
    GraphEdge,
    GridGraph,
    GridSummary,
    InputGrantEdge,
)
from fxtr_experiment.watch_api import (
    BindingsUpdate,
    CellBatch,
    CellGenerationUpdate,
    CellGone,
    CellLogAppend,
    CellLogReset,
    CellObserversUpdate,
    CellPatch,
    CellReportUpdate,
    CellsBatch,
    CellsGone,
    CellsPatch,
    CellsRecomputing,
    CellsRemove,
    CellsUpsert,
    CellsWatchEvent,
    CellWatchEvent,
    GraphBatch,
    GraphRecomputing,
    GraphUpdate,
    GraphWatchEvent,
    GridMeta,
    LintUpdate,
    SourcingUpdate,
    SummaryRemove,
    SummaryUpsert,
    TargetsUpdate,
    WorkflowBatch,
    WorkflowPatch,
    WorkflowRecomputing,
    WorkflowWatchEvent,
)

if TYPE_CHECKING:
    from fxtr_experiment.user_service import UserCapabilityImpl

_log = logging.getLogger(__name__)

#: The coalescing window: hints landing within it recompute once.
_WINDOW_SECONDS = 0.05


class _WorkflowWatch:
    """One ``watch_workflow`` subscription's engine state.

    Sub-topics dirty independently off the hint kinds (`_grow_dirty`) but
    ride one stream; ``rows`` is the keyed mirror (path → row), the
    scalar sub-topics diff by whole-value equality. ``summary`` and
    ``lint`` are the slow-flagged passes (cold force-evaluation / the
    whole-workflow sweep) — a pass touching either emits ``recomputing``
    and always settles with a terminal batch.
    """

    EVENT_TYPE: ClassVar[Any] = WorkflowWatchEvent
    _ALL: ClassVar[frozenset[str]] = frozenset(
        {"summary", "lint", "targets", "sourcing", "bindings"}
    )

    def __init__(self, hub: "SubscriberHub[FxtrServiceData]") -> None:
        self.hub = hub
        self.dirty: set[str] = set(self._ALL)  # fresh watch: snapshot = full diff
        self._rows: dict[str, GridSummary] = {}
        self._lint: object | None = None
        self._targets: object | None = None
        self._sourcing: object | None = None
        self._bindings: object | None = None

    def grow_dirty(self, hints: "Iterable[Hint]") -> None:
        for kind, _path in hints:
            if kind in ("grids", "cells"):
                self.dirty.update(("summary", "lint"))
            elif kind == "intent":
                self.dirty.update(("targets", "sourcing", "lint"))
            elif kind == "bindings":
                self.dirty.add("bindings")

    @property
    def slow(self) -> bool:
        return bool(self.dirty & {"summary", "lint"})

    def recomputing_event(self) -> WorkflowWatchEvent:
        return WorkflowRecomputing()

    async def recompute(self, impl: "UserCapabilityImpl") -> list[WorkflowWatchEvent]:
        patches: list[WorkflowPatch] = []
        if "summary" in self.dirty:
            rows = {row.path: row for row in (await impl.summarize()).grids}
            upserts = tuple(row for path, row in rows.items() if self._rows.get(path) != row)
            removes = tuple(path for path in self._rows if path not in rows)
            if upserts:
                patches.append(SummaryUpsert(rows=upserts))
            if removes:
                patches.append(SummaryRemove(paths=removes))
            self._rows = rows
        if "lint" in self.dirty:
            lint = await impl.lint()
            if lint != self._lint:
                patches.append(LintUpdate(report=lint))
                self._lint = lint
        if "targets" in self.dirty:
            targets = await impl.list_targets()
            if targets != self._targets:
                patches.append(TargetsUpdate(targets=targets))
                self._targets = targets
        if "sourcing" in self.dirty:
            records = await impl.list_sourcing()
            if records != self._sourcing:
                patches.append(SourcingUpdate(records=records))
                self._sourcing = records
        if "bindings" in self.dirty:
            bindings = await impl.list_bindings()
            if bindings != self._bindings:
                patches.append(BindingsUpdate(bindings=bindings))
                self._bindings = bindings
        return [WorkflowBatch(patches=tuple(patches))]


class _CellsWatch:
    """One ``watch_cells`` subscription's engine state.

    ``_grant_key`` caches the walk's minted grants per stored config id:
    tokens must stay STABLE across recomputes (a re-mint every pass
    would make the meta diff endlessly) and re-mint exactly when the
    grid's stored config changes (re-create / config drift), which is
    also when the old spec may be stale. Slow-flagged: a grid recompute
    force-evaluates per-cell currency (`list_cells`), unbounded in the
    grid's population.
    """

    EVENT_TYPE: ClassVar[Any] = CellsWatchEvent

    def __init__(self, hub: "SubscriberHub[FxtrServiceData]", path: str) -> None:
        self.hub = hub
        self.path = path
        self.keys = decode_key_path(path)
        self.dirty: set[str] = {"all"}
        self._gone_sent = False
        self._meta: GridMeta | None = None
        self._rows: dict[tuple, CellInfo] = {}
        self._grant_key: object | None = None  # config id the cached grants were minted for
        self._grant = None
        self._parent_token = None

    def grow_dirty(self, hints: "Iterable[Hint]") -> None:
        for kind, path in hints:
            if kind == "grids" or kind == "intent" or (kind == "cells" and path == self.path):
                self.dirty.add("all")

    @property
    def slow(self) -> bool:
        # A known-gone pass is a cheap existence probe (recompute
        # short-circuits) that publishes nothing while the grid stays
        # deleted — announcing it would emit `recomputing` markers with
        # no settling event. The pass that DISCOVERS the deletion still
        # announces (``_gone_sent`` flips inside recompute); its
        # ``subject_gone`` tombstone is the settle signal.
        return bool(self.dirty) and not self._gone_sent

    def recomputing_event(self) -> CellsWatchEvent:
        return CellsRecomputing()

    def _reset_mirror(self) -> None:
        self._meta = None
        self._rows = {}
        self._grant_key = None
        self._grant = None
        self._parent_token = None

    async def recompute(self, impl: "UserCapabilityImpl") -> list[CellsWatchEvent]:
        # Subject existence first: a deleted grid is a tombstone, not an
        # empty mirror (and its old tokens are dead vocabulary).
        row = await impl._session.get_grid(self.path)
        if row is None:
            if self._gone_sent:
                return []
            self._reset_mirror()
            self._gone_sent = True
            return [CellsGone()]
        self._gone_sent = False
        _kind, config_id = row
        if self._grant_key != config_id:
            # (Re-)mint through the same walk attach uses; grants stay
            # cached while the stored config is unchanged, so meta diffs
            # are stable and served tokens stay valid mount-wide.
            grants = await impl._walk_path(self.keys)
            self._grant = grants[-1]
            self._parent_token = grants[-2].token
            self._grant_key = config_id
        assert self._grant is not None and self._parent_token is not None
        grounding = await impl._session.grid_grounding(self.path)
        meta = GridMeta(
            path=self.path,
            # Served id: re-attach so the event payload covers it (the
            # wire methods' convention; pinned by create_grid's persist).
            config_id=await impl._session.ensure_loadable(config_id),
            grounding=grounding.verdict if grounding is not None else None,  # type: ignore[arg-type]
            grounding_reason=grounding.reason if grounding is not None else None,
            sourcing=await impl._grid_sourcing_status(self.path),
            grant=self._grant,
            parent_token=self._parent_token,
        )
        cells = await impl.list_cells(ViewSel(token=self._grant.token))
        rows = {info.coords: info for info in cells}
        patches: list[CellsPatch] = []
        if meta != self._meta:
            patches.append(meta)
            self._meta = meta
        upserts = tuple(info for coords, info in rows.items() if self._rows.get(coords) != info)
        removes = tuple(coords for coords in self._rows if coords not in rows)
        if upserts:
            patches.append(CellsUpsert(cells=upserts))
        if removes:
            patches.append(CellsRemove(coords=removes))
        self._rows = rows
        return [CellsBatch(patches=tuple(patches))]


class _CellWatch:
    """One ``watch_cell`` subscription's engine state (the drawer
    bundle: report + generation + observers, plus the event-native log
    stream — `WatchEngine.note_log` routes live entries here once
    ``log_live`` flips, which happens in the same no-await window as the
    backlog publish, so backlog and live appends neither overlap nor
    gap)."""

    EVENT_TYPE: ClassVar[Any] = CellWatchEvent

    def __init__(self, hub: "SubscriberHub[FxtrServiceData]", path: str, coords: str) -> None:
        self.hub = hub
        self.path = path
        self.coords = coords
        self.keys = decode_key_path(path)
        self.dirty: set[str] = {"all"}
        self.log_live = False
        self._gone_sent = False
        self._report: CellReport | None = None
        self._generation: GenerationReport | None = None
        self._observers: object | None = None
        self._grant_key: object | None = None
        self._grant = None

    def grow_dirty(self, hints: "Iterable[Hint]") -> None:
        for kind, path in hints:
            if (
                kind == "grids"
                or kind == "bindings"  # a handle report's `bound` flag
                or (kind == "cells" and path == self.path)
            ):
                self.dirty.add("all")

    @property
    def slow(self) -> bool:
        return False  # single-cell queries; no recomputing marker

    def recomputing_event(self) -> CellWatchEvent:  # pragma: no cover - never emitted
        raise AssertionError("cell watches are not slow-flagged")

    def _reset_mirror(self) -> None:
        self._report = None
        self._generation = None
        self._observers = None
        self._grant_key = None
        self._grant = None
        self.log_live = False

    def bound_sel(self) -> ViewSel:
        assert self._grant is not None
        values = decode_coords(self.coords)
        return ViewSel(token=self._grant.token, bound=frozendict(enumerate(values)))

    async def recompute(self, impl: "UserCapabilityImpl") -> list[CellWatchEvent]:
        row = await impl._session.get_grid(self.path)
        if row is None:
            if self._gone_sent:
                return []
            self._reset_mirror()
            self._gone_sent = True
            return [CellGone()]
        self._gone_sent = False
        kind, config_id = row
        if self._grant_key != config_id:
            grants = await impl._walk_path(self.keys)
            self._grant = grants[-1]
            self._grant_key = config_id
        patches: list[CellPatch] = []
        report = await impl.inspect_cell(self.bound_sel())
        if report != self._report:
            patches.append(CellReportUpdate(report=report))
            self._report = report
        if kind == "task":
            generation = await impl.inspect_generation(self.bound_sel())
            if generation != self._generation:
                patches.append(CellGenerationUpdate(report=generation))
                self._generation = generation
            observers = await impl.list_observers(self.bound_sel())
            if observers != self._observers:
                patches.append(CellObserversUpdate(observers=observers))
                self._observers = observers
        return [CellBatch(patches=tuple(patches))]


class _GraphWatch:
    """One ``watch_graph`` subscription's engine state (the plate view's
    relational overlay).

    The overlay's two halves dirty independently (`notes/plate_view.md`):
    ``structure`` (input grants + axis correspondences — a function of the
    grid set alone, since a task rule is fixed at grid creation) recomputes
    on ``grids`` hints; ``edges`` (the aggregated relationship families)
    recomputes on ``grids`` and ``cells`` hints, since they turn over with
    generations. A dirty pass recomputes only its dirty halves, reuses the
    cached other half, and diffs the reassembled `GridGraph` — the wire
    stays a whole-value replace. Slow-flagged on ``structure`` (the
    full-config load + rule decode; an edges-only aggregation pass is cheap
    and stays quiet unless the edges actually changed).
    """

    EVENT_TYPE: ClassVar[Any] = GraphWatchEvent
    _ALL: ClassVar[frozenset[str]] = frozenset({"structure", "edges"})

    def __init__(self, hub: "SubscriberHub[FxtrServiceData]") -> None:
        self.hub = hub
        self.dirty: set[str] = set(self._ALL)  # fresh watch: snapshot = full diff
        self._edges: tuple[GraphEdge, ...] = ()
        self._grants: tuple[InputGrantEdge, ...] = ()
        self._corrs: tuple[AxisCorrespondence, ...] = ()
        self._graph: GridGraph | None = None

    def grow_dirty(self, hints: "Iterable[Hint]") -> None:
        for kind, _path in hints:
            if kind == "grids":
                self.dirty.update(("structure", "edges"))
            elif kind == "cells":
                self.dirty.add("edges")

    @property
    def slow(self) -> bool:
        return "structure" in self.dirty

    def recomputing_event(self) -> GraphWatchEvent:
        return GraphRecomputing()

    async def recompute(self, impl: "UserCapabilityImpl") -> list[GraphWatchEvent]:
        if "structure" in self.dirty:
            self._grants, self._corrs = await impl._graph_structure()
        if "edges" in self.dirty:
            self._edges = await impl._graph_edges()
        graph = GridGraph(edges=self._edges, input_grants=self._grants, correspondences=self._corrs)
        patches: list[GraphUpdate] = []
        if graph != self._graph:
            patches.append(GraphUpdate(graph=graph))
            self._graph = graph
        return [GraphBatch(patches=tuple(patches))]


class WatchEngine:
    """Per-mount watch machinery (see the module docstring).

    Wired by ``UserCapabilityImpl._fxtr_mount_lifespan``; the capability's
    ``watch_*`` methods reach it via ``_fxtr_get_lifespan_state()``.

    Args:
        task_group: The mount lifespan's task group — hosts the worker
            and every watch hub's per-subscription tasks; cancelling it
            is the bounded teardown.
        impl: The owning capability impl (the engine recomputes through
            its wire methods and mints grants through its walk; the
            runner scope + session ride along).
    """

    def __init__(self, task_group: anyio.abc.TaskGroup, impl: "UserCapabilityImpl") -> None:
        self._tg = task_group
        self._impl = impl
        self._scope: FxtrScope = impl._scope
        self._watches: list[_WorkflowWatch | _CellsWatch | _CellWatch | _GraphWatch] = []
        self._buf: set[Hint] = set()
        self._wake = anyio.Event()
        task_group.start_soon(self._worker)

    # -- the emission seams (same shapes as the session/registry listeners) -------

    def note_hints(self, hints: "frozenset[Hint]") -> None:
        """The session's post-commit flush (sync, non-blocking)."""
        self._buf |= hints
        self._wake.set()

    def note_bindings_changed(self) -> None:
        """The binding registry's change callback."""
        self.note_hints(frozenset({("bindings", "")}))

    def note_log(self, grid_path: str, coords: str, event: "CellLogReset | CellLogAppend") -> None:
        """The run-log registry's append listener: event-native routing
        straight to the matching live cell watches (no diffing; sync)."""
        for watch in self._watches:
            if (
                isinstance(watch, _CellWatch)
                and watch.log_live
                and watch.path == grid_path
                and watch.coords == coords
            ):
                self._publish(watch, event)

    # -- subscribe (the wire methods' bodies) --------------------------------------

    async def add_workflow_watch(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]", source_scope: FxtrScope
    ) -> "ServiceRef[UnsubscribeHandle]":
        return await self._add_watch(_WorkflowWatch, subscriber, source_scope)

    async def add_cells_watch(
        self,
        path: str,
        subscriber: "ServiceRef[Subscriber[FxtrServiceData]]",
        source_scope: FxtrScope,
    ) -> "ServiceRef[UnsubscribeHandle]":
        return await self._add_watch(_CellsWatch, subscriber, source_scope, path)

    async def add_cell_watch(
        self,
        path: str,
        coords: str,
        subscriber: "ServiceRef[Subscriber[FxtrServiceData]]",
        source_scope: FxtrScope,
    ) -> "ServiceRef[UnsubscribeHandle]":
        return await self._add_watch(_CellWatch, subscriber, source_scope, path, coords)

    async def add_graph_watch(
        self, subscriber: "ServiceRef[Subscriber[FxtrServiceData]]", source_scope: FxtrScope
    ) -> "ServiceRef[UnsubscribeHandle]":
        return await self._add_watch(_GraphWatch, subscriber, source_scope)

    async def _add_watch(
        self,
        cls: type,
        subscriber: "ServiceRef[Subscriber[FxtrServiceData]]",
        source_scope: FxtrScope,
        *args: str,
    ) -> "ServiceRef[UnsubscribeHandle]":
        """Register one watch: its own queue-mode hub + one subscription.

        The watch enters the table fully dirty AFTER its hub subscription
        is established, so its first recompute (the snapshot) covers
        every commit up to that pass and nothing can publish to the hub
        before the subscription exists.
        """
        hub: "SubscriberHub[FxtrServiceData]" = SubscriberHub(task_group=self._tg, mode="queue")
        handle = await hub.subscribe(subscriber, source_scope)
        watch = cls(hub, *args)
        self._watches.append(watch)
        self._wake.set()
        return handle

    # -- the worker -----------------------------------------------------------------

    def _publish(self, watch: Any, event: Any) -> None:
        """Encode + publish one event on ``watch``'s hub, with the runner
        scope as origin (payloads may carry served ids; see the module
        docstring's residency note).

        Origin authorization is the runner scope's **base** lease —
        scope-level state, resolvable from ANY task while the scope is
        open (`entity_ref_scope`'s two-layer design: ``clone_
        authorization`` resolves base-first; no context inheritance is
        involved). So the invariant is exactly: publishes succeed while
        the runner scope is open-and-not-draining, from every context
        the engine publishes in. The engine deliberately holds NO
        standing lease of its own — a retained clone would deadlock the
        runner scope's graceful drain (`FxtrScope.clone_authorization`'s
        documented lifetime consequence: mount teardown, which would
        release it, runs post-drain). A publish racing the drain window
        (base dropped, teardown not yet run) fails authorization and is
        dropped — sound, because the same drain's mount teardown then
        unconditionally force-breaks every subscription (`aclose`), so
        a subscriber that missed a drain-window event always observes
        the break; no mirror can go silently stale. (The except also
        covers the hub's own post-``aclose`` publish refusal — the same
        teardown-race class.)
        """
        wire = encode_to_service_data(event, watch.EVENT_TYPE)
        try:
            watch.hub.publish(wire, origin_scope=self._scope)
        except RuntimeError:
            _log.debug("watch publish dropped (runner scope draining?)", exc_info=True)

    def _swap_window(self) -> frozenset[Hint]:
        """Swap the window buffer out (sync — atomic wrt `note_hints`)."""
        hints = frozenset(self._buf)
        self._buf = set()
        self._wake = anyio.Event()
        return hints

    async def _worker(self) -> None:
        """One pass per quiet-ish window: grow dirt, recompute, publish,
        reap."""
        while True:
            await self._wake.wait()
            await anyio.sleep(_WINDOW_SECONDS)
            hints = self._swap_window()
            live: list[_WorkflowWatch | _CellsWatch | _CellWatch | _GraphWatch] = []
            for watch in self._watches:
                if watch.hub.live_subscription_count == 0:
                    # The subscription ended (unsubscribe / handle drop /
                    # delivery failure): reap — the hub's own teardown
                    # already ran per subscription.
                    continue
                live.append(watch)
                watch.grow_dirty(hints)
                if not watch.dirty:
                    continue
                slow = watch.slow
                if slow:
                    self._publish(watch, watch.recomputing_event())
                try:
                    events = await watch.recompute(self._impl)
                except Exception:
                    # Logged and dropped; the dirty set clears below —
                    # the next hint re-triggers, and resubscribing is
                    # the client-side reconciliation lever (module
                    # docstring, failure posture). A `recomputing`
                    # marker already published settles on the next
                    # successful batch.
                    _log.warning(
                        "watch recompute failed for %r", type(watch).__name__, exc_info=True
                    )
                    watch.dirty.clear()
                    continue
                watch.dirty.clear()
                for event in events:
                    # An empty batch publishes only as a settle marker.
                    is_empty_batch = (
                        isinstance(event, (WorkflowBatch, CellsBatch, CellBatch, GraphBatch))
                        and not event.patches
                    )
                    if is_empty_batch and not slow:
                        continue
                    self._publish(watch, event)
                if isinstance(watch, _CellWatch) and not watch.log_live and not watch._gone_sent:
                    self._flip_log_live(watch)
            self._watches = live

    def _flip_log_live(self, watch: _CellWatch) -> None:
        """Publish the run-log backlog and flip the watch live for
        `note_log` routing — one sync (no-await) window, so live appends
        can neither duplicate the backlog nor slip between them."""
        snap = self._impl._executor.run_log.snapshot(watch.path, watch.coords)
        if snap is not None:
            generation, entries = snap
            self._publish(watch, CellLogReset(generation=generation))
            if entries:
                self._publish(watch, CellLogAppend(entries=entries))
        watch.log_live = True

    async def aclose(self) -> None:
        """Mount teardown: flush + tear down every watch's subscriptions
        (each hub's `aclose`). The caller cancels the task group
        afterwards (ends the worker)."""
        for watch in self._watches:
            await watch.hub.aclose()
        self._watches = []
