/**
 * TS mirrors of the wire forms the dashboard speaks — the mashumaro
 * service-dialect encodings of `fxtr_experiment`'s `user_api` /
 * `runner_api` / `watch_api` dataclasses, as they arrive AFTER the transport's
 * wire-escape decode (so `FxtrID` leaves are `EntityRefStub`s, and
 * `frozendict`s are plain objects). Hand-mirrored; pinned live by the
 * dialect round-trip harness in `tests/` (both directions), so drift
 * on either side fails a test.
 *
 * Conventions worth knowing:
 * - mashumaro emits EVERY field (including `None`s), so optional
 *   fields are typed `| null` rather than `?:`.
 * - Python tuples arrive as arrays.
 * - `ViewSel.bound` crosses as sorted `[position, value]` pairs (int
 *   keys are not legal wire mapping keys — see runner_api's
 *   `_BoundStrategy`); build selections with `viewSel()` from
 *   `helpers.ts` rather than by hand.
 * - `TargetRecord` round-trips VERBATIM into `demand_target` /
 *   `remove_target`; treat it as opaque apart from the display
 *   helpers.
 */

import type { EntityRefStub, ResultValue } from "@fxtr-ui/core";

// ---- literal unions (mirror the Python `Literal` aliases) ----

export type GridKind = "data" | "dataframe" | "handle" | "task" | "checkpoint" | "subtree";
export type ViewKind = GridKind | "coord";
export type AxisKind = "str" | "int";
export type SliceMode = "read" | "write";
export type ExecStatus = "pending" | "running" | "complete" | "errored" | "interrupted";
export type Currency = "fresh" | "stale" | "unsettled";
export type GroundingVerdict = "grounded" | "unanchored" | "orphaned";
export type ObservationKind = "cell_value" | "wrote_cell" | "grid_config";
export type CurrentMatch = "match" | "changed" | "missing";
export type FailureKind = "error" | "fault";
export type RequestKind = "task_output" | "data";
export type FactKind = "grid" | "data_cell" | "task_cell";
export type TargetStatus = "attached" | "dangling_missing" | "dangling_mismatched";
export type SourcingStatus = "attached" | "dangling_missing" | "dangling_mismatched";

export type CoordValue = string | number;

// ---- addressing (runner_api) ----

/** Opaque wire identity of one granted view root (ephemeral per
 * capability mount — a re-derivable cache, never durable state). */
export type ViewToken = string;

export type AxisSpec = {
	readonly name: string;
	readonly kind: AxisKind;
};

/** `runner_api.ViewGrant` — token + the local shadow. `schema` /
 * `outputs` are typed loosely until the dashboard needs their
 * internals (dataframe rendering). */
export type ViewGrant = {
	readonly token: ViewToken;
	readonly kind: ViewKind;
	readonly axes: readonly AxisSpec[];
	readonly mode: SliceMode;
	readonly schema: ResultValue | null;
	readonly outputs: Readonly<Record<string, ResultValue>> | null;
	readonly value: CoordValue | null;
};

/** `runner_api.ViewSel`'s wire form. Construct via `viewSel()`. */
export type ViewSel = {
	readonly token: ViewToken;
	readonly bound: readonly (readonly [number, CoordValue])[];
	readonly output: string | null;
};

// ---- records (round-trip verbatim) ----

declare const TARGET_RECORD_BRAND: unique symbol;

/** `model.TargetRecord` as decoded from the wire. Opaque by doctrine:
 * the UI passes it back verbatim (`demand_target` / `remove_target`)
 * and only the lint/targets *Info* wrappers carry display locations.
 * (`expected_config` is an id leaf → a stub after decode.) */
export type TargetRecord = {
	readonly slice: ResultValue;
	readonly expected_config: EntityRefStub;
} & { readonly [TARGET_RECORD_BRAND]?: never };

// ---- presence + inspection (user_api result dataclasses) ----

export type KeyInfo = {
	readonly key: string;
	readonly kind: GridKind;
	readonly axes: readonly AxisSpec[];
	readonly grounding: GroundingVerdict | null;
	readonly grounding_reason: string | null;
	readonly config_id: EntityRefStub | null;
};

export type CellInfo = {
	readonly coords: readonly CoordValue[];
	readonly status: ExecStatus | null;
	readonly currency: Currency | null;
	readonly reason: string | null;
	readonly grounding: GroundingVerdict | null;
	readonly wanted: boolean | null;
};

export type CellReport = {
	readonly status: ExecStatus | null;
	readonly currency: Currency | null;
	readonly reason: string | null;
	readonly results: Readonly<Record<string, EntityRefStub>> | null;
	readonly value: EntityRefStub | null;
	readonly grounding: GroundingVerdict | null;
	readonly grounding_reason: string | null;
	readonly wanted: boolean | null;
	readonly wanted_reason: string | null;
	readonly bound: boolean | null;
};

export type GridSummary = {
	readonly path: string;
	readonly kind: GridKind;
	readonly axes: readonly AxisSpec[];
	readonly config_id: EntityRefStub;
	readonly grounding: GroundingVerdict | null;
	readonly grounding_reason: string | null;
	readonly sourcing: SourcingStatus | null;
	readonly populated: number;
	readonly n_pending: number;
	readonly n_running: number;
	readonly n_complete: number;
	readonly n_errored: number;
	readonly n_interrupted: number;
	readonly n_stale: number;
	readonly n_unsettled: number;
	readonly targets_attached: number;
	readonly targets_dangling: number;
};

export type WorkflowSummary = {
	readonly grids: readonly GridSummary[];
};

export type ObservationInfo = {
	readonly subject_path: string;
	readonly subject_coords: string;
	readonly subject_output: string;
	readonly kind: ObservationKind;
	readonly fingerprint: EntityRefStub;
	readonly current: CurrentMatch;
};

export type FailureInfo = {
	readonly kind: FailureKind;
	readonly error_class: string;
	readonly message: string;
	readonly ts: string;
};

export type CheckpointInfo = {
	readonly grid_path: string;
	readonly coords: string;
	readonly step_type_id: EntityRefStub;
	readonly args_id: EntityRefStub;
	readonly value_id: EntityRefStub;
};

export type RequestInfo = {
	readonly grid_path: string;
	readonly coords: string;
	readonly kind: RequestKind;
};

export type GenerationReport = {
	readonly generation: string | null;
	readonly status: ExecStatus | null;
	readonly executed_fn: EntityRefStub | null;
	readonly failures: readonly FailureInfo[];
	readonly observations: readonly ObservationInfo[];
	readonly unanswered_requests: readonly RequestInfo[];
	readonly checkpoints: readonly CheckpointInfo[];
};

export type ObserverInfo = {
	readonly grid_path: string;
	readonly coords: string;
	readonly status: ExecStatus;
	readonly currency: Currency | null;
};

export type TargetInfo = {
	readonly target: TargetRecord;
	readonly status: TargetStatus;
	/** Absolute location of the wanted cell (resolvable for dangling
	 * records too — the location is a property of the record). */
	readonly grid_path: string;
	readonly coords: string;
	readonly grid_grounding: GroundingVerdict | null;
};

export type SourcingInfo = {
	readonly grid_path: string;
	readonly coords: string;
	readonly fingerprint: EntityRefStub;
	readonly status: SourcingStatus;
};

/** An absolute cell address (root-frame transparency). */
export type CellLocation = {
	readonly grid_path: string;
	readonly coords: string;
};

/** One live handle binding + the handle cells currently holding its
 * key (possibly none — bindings survive cell deletion). */
// TODO(chunk 5 — plans/phase6_sandboxing/chunk5_monitoring.md): narrower
// than the wire — `user_api.BindingInfo` also carries `state`
// (BindingState), `deps`, `config` and `sandbox`. Widen with the bindings
// panel; the dialect harness will not flag this, since decode is
// structural and unmirrored fields are simply ignored.
export type BindingInfo = {
	readonly key: EntityRefStub;
	readonly cells: readonly CellLocation[];
};

export type TargetFinding = {
	readonly target: TargetRecord;
	readonly grid_path: string;
	readonly coords: string;
	readonly verdict: GroundingVerdict;
	readonly reason: string | null;
};

export type UngroundedFact = {
	readonly kind: FactKind;
	readonly grid_path: string;
	readonly coords: string;
	readonly verdict: GroundingVerdict;
	readonly reason: string | null;
};

/** `user_api.LintReport`'s FIELDS. The Python side's `sound` / `clean`
 * are `@property`s and never cross the wire — use `lintSound()` /
 * `lintClean()` from `helpers.ts`. */
export type LintReport = {
	readonly dangling_sourcing: readonly SourcingInfo[];
	readonly dangling_targets: readonly TargetInfo[];
	readonly target_findings: readonly TargetFinding[];
	readonly ungrounded: readonly UngroundedFact[];
};

export type DeleteReport = {
	readonly broken_referrers: readonly string[];
};

// ---- watch events (`watch_api.py` — the `watch_*` streams) ----
//
// Each `on_event` delivery is ONE member of the family's union,
// wire-encoded by the watch engine (sender-encodes). Shared contract
// (`notes/event_system.md`): the first batch after subscribing is the
// snapshot (the diff from the empty mirror); an empty batch is the
// settle marker after a `recomputing`; `subject_gone` is a tombstone
// (the watch stays live — re-creation resumes with a fresh snapshot);
// log events (cell watch, task cells) are event-native appends, with
// the backlog delivered right after the first batch.

export type LogEntryKind =
	| "log"
	| "wrote_cell"
	| "read_cell"
	| "demand"
	| "step_started"
	| "step_hit"
	| "step_errored";

/** `watch_api.StepRef` — a step's identity: the checkpoint cell it runs
 * into, in the same `(grid_path, coords)` vocabulary as `CheckpointInfo`
 * (so the drawer keys log step-lifecycle notes to checkpoint entries by
 * tuple). */
export type StepRef = {
	readonly grid_path: string;
	readonly coords: string;
};

/** `watch_api.LogEntry` — one run-log line (task-authored `log()` or
 * an engine lifecycle note); `step` is the step it belongs to, null for
 * the task fn's outer level. */
export type LogEntry = {
	readonly ts: string;
	readonly entry_kind: LogEntryKind;
	readonly step: StepRef | null;
	readonly message: string;
};

// -- the workflow watch (watch_workflow) --

export type SummaryUpsert = {
	readonly kind: "summary_upsert";
	/** Whole-row replacements, keyed by `row.path`. */
	readonly rows: readonly GridSummary[];
};

export type SummaryRemove = {
	readonly kind: "summary_remove";
	readonly paths: readonly string[];
};

export type LintUpdate = { readonly kind: "lint"; readonly report: LintReport };
export type TargetsUpdate = { readonly kind: "targets"; readonly targets: readonly TargetInfo[] };
export type SourcingUpdate = {
	readonly kind: "sourcing";
	readonly records: readonly SourcingInfo[];
};
export type BindingsUpdate = {
	readonly kind: "bindings";
	readonly bindings: readonly BindingInfo[];
};

export type WorkflowPatch =
	| SummaryUpsert
	| SummaryRemove
	| LintUpdate
	| TargetsUpdate
	| SourcingUpdate
	| BindingsUpdate;

export type WorkflowBatch = { readonly kind: "batch"; readonly patches: readonly WorkflowPatch[] };
export type WorkflowRecomputing = { readonly kind: "recomputing" };
export type WorkflowWatchEvent = WorkflowBatch | WorkflowRecomputing;

// -- the grid watch (watch_cells) --

/** `watch_api.GridMeta` — the watched grid's header bundle, carrying
 * the grants the mutation surface builds `ViewSel`s from: the grid's
 * own `grant` plus its `parent_token` (null for a root grid —
 * grid-level mutations address parent + key, so none exist there).
 * Token lifetimes align with the watch: a mount death breaks the
 * subscription, and the resubscribe delivers fresh tokens. */
export type GridMeta = {
	readonly kind: "meta";
	readonly path: string;
	readonly config_id: EntityRefStub;
	readonly grounding: GroundingVerdict | null;
	readonly grounding_reason: string | null;
	readonly sourcing: SourcingStatus | null;
	readonly grant: ViewGrant;
	readonly parent_token: ViewToken | null;
};

export type CellsUpsert = {
	readonly kind: "cells_upsert";
	/** Whole-row replacements, keyed by `row.coords` (coords-text form:
	 * compact JSON of the array — the `CellInfo` identity vocabulary). */
	readonly cells: readonly CellInfo[];
};

export type CellsRemove = {
	readonly kind: "cells_remove";
	readonly coords: readonly (readonly CoordValue[])[];
};

export type CellsPatch = GridMeta | CellsUpsert | CellsRemove;

export type CellsBatch = { readonly kind: "batch"; readonly patches: readonly CellsPatch[] };
export type CellsRecomputing = { readonly kind: "recomputing" };
export type CellsGone = { readonly kind: "subject_gone" };
export type CellsWatchEvent = CellsBatch | CellsRecomputing | CellsGone;

// -- the cell watch (watch_cell) --

export type CellReportUpdate = { readonly kind: "report"; readonly report: CellReport };
export type CellGenerationUpdate = {
	readonly kind: "generation";
	readonly report: GenerationReport;
};
export type CellObserversUpdate = {
	readonly kind: "observers";
	readonly observers: readonly ObserverInfo[];
};

export type CellPatch = CellReportUpdate | CellGenerationUpdate | CellObserversUpdate;

export type CellBatch = { readonly kind: "batch"; readonly patches: readonly CellPatch[] };
export type CellRecomputing = { readonly kind: "recomputing" };
export type CellGone = { readonly kind: "subject_gone" };

/** A new run restarted the cell's run-log buffer: drop accumulated
 * entries (appends for the new run follow). */
export type CellLogReset = { readonly kind: "log_reset"; readonly generation: string };

/** Appended run-log entries (event-native — published straight from
 * the buffer, not diffed); the backlog arrives as one of these right
 * after the first batch. */
export type CellLogAppend = { readonly kind: "log_append"; readonly entries: readonly LogEntry[] };

export type CellWatchEvent = CellBatch | CellRecomputing | CellGone | CellLogReset | CellLogAppend;

// ---- the grid graph (plate view overlay — `user_api` GridGraph) ----
//
// The relational overlay the plate view joins onto the workflow summary
// (nodes / axes / status stay the summary's job). Deliberately id-free —
// every leaf is a path / name / bool / int, no `EntityRefStub`s.

export type GraphEdgeKind = "read" | "wrote_cell" | "grid_assertion";

/** One grid-pair relationship edge, aggregated over current generations. */
export type GraphEdge = {
	readonly observer_path: string;
	readonly subject_path: string;
	readonly kind: GraphEdgeKind;
	readonly count: number;
};

/** One static rule-wiring grant (the `task_fn` slice or a named input);
 * `kept` marks a `KeepAxis` slice ("batched over" rather than vmapped). */
export type InputGrantEdge = {
	readonly task_path: string;
	readonly target_path: string;
	readonly arg_name: string;
	readonly is_task_fn: boolean;
	readonly mode: SliceMode;
	readonly kept: boolean;
};

/** One provable axis identity from a `Param` (vmap) binding, resolved to
 * the owning grid + own-axis name on both sides (canonically ordered). */
export type AxisCorrespondence = {
	readonly grid_a: string;
	readonly axis_a: string;
	readonly grid_b: string;
	readonly axis_b: string;
};

export type GridGraph = {
	readonly edges: readonly GraphEdge[];
	readonly input_grants: readonly InputGrantEdge[];
	readonly correspondences: readonly AxisCorrespondence[];
};

// -- the grid-graph watch (watch_graph) --

/** Whole-value overlay replace (the overlay ships whole, not as keyed
 * patches — it changes rarely; diff-vs-last-sent suppresses no-ops). */
export type GraphUpdate = { readonly kind: "graph"; readonly graph: GridGraph };

export type GraphBatch = { readonly kind: "batch"; readonly patches: readonly GraphUpdate[] };
export type GraphRecomputing = { readonly kind: "recomputing" };
export type GraphWatchEvent = GraphBatch | GraphRecomputing;
