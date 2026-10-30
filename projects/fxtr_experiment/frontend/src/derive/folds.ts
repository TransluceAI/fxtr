/**
 * Pure folds: watch events → always-current mirrors (the client half of
 * `notes/event_system.md`). One mirror shape + fold per watch family;
 * the generic driver (`watch_driver.ts`) reduces delivered events
 * through the family's fold in order, and components read the mirror as
 * plain data — no refetching, no invalidation planning.
 *
 * Contract recap (the folds implement it, the engine guarantees it):
 * the first batch after subscribing is the snapshot (diff from the
 * empty mirror), so every mirror starts empty with `snapshotted:
 * false`; `recomputing` flags a slow pass until its terminal batch
 * (possibly empty) settles it; `subject_gone` resets the mirror to a
 * tombstone — the server resets its own mirror too, so a re-creation's
 * snapshot batch re-upserts everything (folding it into stale rows
 * would leak the dead grid's cells, hence the reset here). Log events
 * (cell watch) are event-native: `log_reset` restarts the entry list
 * per run, `log_append` extends it.
 *
 * Events arrive as unknown inbox payloads; each fold narrows by the
 * `kind` discriminant and IGNORES anything malformed or unknown (a
 * newer backend's extra variants must not crash the panel — the mirror
 * just misses that information until the frontend learns the variant).
 */

import type {
	BindingInfo,
	CellInfo,
	CellReport,
	CellWatchEvent,
	CellsWatchEvent,
	GenerationReport,
	GraphWatchEvent,
	GridGraph,
	GridMeta,
	GridSummary,
	LintReport,
	LogEntry,
	ObserverInfo,
	SourcingInfo,
	TargetInfo,
	WorkflowWatchEvent,
} from "../wire/types.js";

/** Client-side bound on retained log entries — mirrors the runner
 * buffer's own drop-oldest cap, so the two views agree. */
export const MAX_LOG_ENTRIES = 2000;

/** Narrow an unknown inbox payload to one of `kinds` (see the module
 * docstring's ignore-unknown posture). The cast is sound to the extent
 * the dialect pins hold — `tests/dialect.test.ts` pins every variant. */
function asEvent<T extends { readonly kind: string }>(
	payload: unknown,
	kinds: readonly T["kind"][],
): T | null {
	if (payload === null || typeof payload !== "object") return null;
	const kind = (payload as { kind?: unknown }).kind;
	if (typeof kind !== "string" || !kinds.includes(kind as T["kind"])) return null;
	return payload as T;
}

// ---- watch_workflow ----

export type WorkflowMirror = {
	/** The snapshot batch has been folded (before it: render loading). */
	readonly snapshotted: boolean;
	/** A slow-flagged recompute (summarize / lint) is in flight. */
	readonly recomputing: boolean;
	/** Grid summary rows, keyed by key-path text. */
	readonly rows: Readonly<Record<string, GridSummary>>;
	readonly lint: LintReport | null;
	readonly targets: readonly TargetInfo[];
	readonly sourcing: readonly SourcingInfo[];
	readonly bindings: readonly BindingInfo[];
};

export const WORKFLOW_MIRROR_EMPTY: WorkflowMirror = {
	snapshotted: false,
	recomputing: false,
	rows: {},
	lint: null,
	targets: [],
	sourcing: [],
	bindings: [],
};

export function foldWorkflow(mirror: WorkflowMirror, payload: unknown): WorkflowMirror {
	const event = asEvent<WorkflowWatchEvent>(payload, ["batch", "recomputing"]);
	if (event === null) return mirror;
	if (event.kind === "recomputing") return { ...mirror, recomputing: true };
	let { rows, lint, targets, sourcing, bindings } = mirror;
	for (const patch of event.patches) {
		switch (patch.kind) {
			case "summary_upsert": {
				const next = { ...rows };
				for (const row of patch.rows) next[row.path] = row;
				rows = next;
				break;
			}
			case "summary_remove": {
				const next = { ...rows };
				for (const path of patch.paths) delete next[path];
				rows = next;
				break;
			}
			case "lint":
				lint = patch.report;
				break;
			case "targets":
				targets = patch.targets;
				break;
			case "sourcing":
				sourcing = patch.records;
				break;
			case "bindings":
				bindings = patch.bindings;
				break;
		}
	}
	return { snapshotted: true, recomputing: false, rows, lint, targets, sourcing, bindings };
}

// ---- watch_cells ----

export type CellsMirror = {
	readonly snapshotted: boolean;
	readonly recomputing: boolean;
	/** The watched grid does not exist (tombstone — the watch stays
	 * live; a re-creation's snapshot batch flips this back). */
	readonly gone: boolean;
	/** The grid header bundle, incl. the mutation grants. */
	readonly meta: GridMeta | null;
	/** Cell rows keyed by coords text (compact JSON of the coords array
	 * — the `CellInfo` identity + targets join vocabulary). */
	readonly cells: Readonly<Record<string, CellInfo>>;
};

export const CELLS_MIRROR_EMPTY: CellsMirror = {
	snapshotted: false,
	recomputing: false,
	gone: false,
	meta: null,
	cells: {},
};

/** A cell's mirror key / row identity: compact-JSON coords text (the
 * same canonical form as the Python `encode_coords`). */
export function coordsText(coords: readonly unknown[]): string {
	return JSON.stringify(coords);
}

export function foldCells(mirror: CellsMirror, payload: unknown): CellsMirror {
	const event = asEvent<CellsWatchEvent>(payload, ["batch", "recomputing", "subject_gone"]);
	if (event === null) return mirror;
	if (event.kind === "recomputing") return { ...mirror, recomputing: true };
	if (event.kind === "subject_gone") {
		return { ...CELLS_MIRROR_EMPTY, snapshotted: true, gone: true };
	}
	let { meta, cells } = mirror;
	for (const patch of event.patches) {
		switch (patch.kind) {
			case "meta":
				meta = patch;
				break;
			case "cells_upsert": {
				const next = { ...cells };
				for (const cell of patch.cells) next[coordsText(cell.coords)] = cell;
				cells = next;
				break;
			}
			case "cells_remove": {
				const next = { ...cells };
				for (const coords of patch.coords) delete next[coordsText(coords)];
				cells = next;
				break;
			}
		}
	}
	return { snapshotted: true, recomputing: false, gone: false, meta, cells };
}

// ---- watch_cell ----

/** One run's log entries (restarted by `log_reset`; `generation` is
 * the run's public generation id). */
export type CellLogMirror = {
	readonly generation: string;
	readonly entries: readonly LogEntry[];
};

export type CellMirror = {
	readonly snapshotted: boolean;
	/** The watched cell's GRID does not exist (tombstone; cell
	 * emptiness is data — an empty report — never a tombstone). */
	readonly gone: boolean;
	readonly report: CellReport | null;
	/** Task cells only, like the two below. */
	readonly generation: GenerationReport | null;
	readonly observers: readonly ObserverInfo[];
	/** Null until a run's `log_reset` (no run since the runner came
	 * up); the backlog arrives right after the first batch. */
	readonly log: CellLogMirror | null;
};

export const CELL_MIRROR_EMPTY: CellMirror = {
	snapshotted: false,
	gone: false,
	report: null,
	generation: null,
	observers: [],
	log: null,
};

export function foldCell(mirror: CellMirror, payload: unknown): CellMirror {
	const event = asEvent<CellWatchEvent>(payload, [
		"batch",
		"recomputing", // in the union for vocabulary uniformity; the
		// engine never slow-flags single-cell recomputes, so no flag here
		"subject_gone",
		"log_reset",
		"log_append",
	]);
	if (event === null || event.kind === "recomputing") return mirror;
	switch (event.kind) {
		case "subject_gone":
			return { ...CELL_MIRROR_EMPTY, snapshotted: true, gone: true };
		case "log_reset":
			return { ...mirror, log: { generation: event.generation, entries: [] } };
		case "log_append": {
			// Defensive: a reset always precedes appends (the engine's
			// backlog window), but a missed one must not drop entries.
			const log = mirror.log ?? { generation: "", entries: [] };
			const entries = [...log.entries, ...event.entries].slice(-MAX_LOG_ENTRIES);
			return { ...mirror, log: { ...log, entries } };
		}
		case "batch": {
			let { report, generation, observers } = mirror;
			for (const patch of event.patches) {
				switch (patch.kind) {
					case "report":
						report = patch.report;
						break;
					case "generation":
						generation = patch.report;
						break;
					case "observers":
						observers = patch.observers;
						break;
				}
			}
			return { ...mirror, snapshotted: true, gone: false, report, generation, observers };
		}
	}
}

// ---- watch_graph ----

export type GraphMirror = {
	readonly snapshotted: boolean;
	/** A slow-flagged recompute (the structure half's config-load pass)
	 * is in flight. */
	readonly recomputing: boolean;
	/** The relational overlay (edges + grants + correspondences), or null
	 * before the snapshot. */
	readonly graph: GridGraph | null;
};

export const GRAPH_MIRROR_EMPTY: GraphMirror = {
	snapshotted: false,
	recomputing: false,
	graph: null,
};

export function foldGraph(mirror: GraphMirror, payload: unknown): GraphMirror {
	const event = asEvent<GraphWatchEvent>(payload, ["batch", "recomputing"]);
	if (event === null) return mirror;
	if (event.kind === "recomputing") return { ...mirror, recomputing: true };
	// Whole-value replace: the last GraphUpdate in the batch wins (there is
	// at most one; an empty batch is a pure settle marker).
	let { graph } = mirror;
	for (const patch of event.patches) graph = patch.graph;
	return { snapshotted: true, recomputing: false, graph };
}
