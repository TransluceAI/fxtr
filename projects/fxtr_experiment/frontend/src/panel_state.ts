/**
 * The dashboard panel's `ownStorage` shape (versioned per the standard
 * validity-check convention: a stale shape from an older build resets
 * to defaults instead of crashing — bump `PANEL_KIND` on breaking
 * changes).
 *
 * `watches` holds the panel's three watch slots (`watch_driver.ts`
 * allocates and maintains them): the always-on `workflow` watch, plus
 * ONE `cells` watch (the selected grid) and ONE `cell` watch (the
 * selected cell's drawer) — single entries, not keyed maps, because
 * navigation drops a watch immediately (no LRU in v0), so at most one
 * of each is ever live. Each entry carries its spec, its inbox + the
 * `subscribe_to` invoke, and the folded mirror (`derive/folds.ts`);
 * dropping an entry from state is the unsubscribe. Grants are not
 * cached anywhere: mutation `ViewSel`s are built from the cells
 * mirror's `GridMeta` (grants ride the watch payloads and die with the
 * subscription — nothing token-shaped is durable in spirit, even
 * though the entry technically persists: a revived backend changes the
 * capability stub, which invalidates the entry wholesale).
 *
 * `actions` is the mutation log — each entry is one user gesture as a
 * short chain of capability calls (`ActionStep`s), the first already
 * allocated; the log's rows advance the chain as steps fill
 * (`actions.tsx`). `ui` is plain navigation state.
 */

import type { OpResultStub } from "@fxtr-ui/core";

import type { CellMirror, CellsMirror, GraphMirror, WorkflowMirror } from "./derive/folds.js";
import type { FrameLoads } from "./frame_table.js";
import type { WatchEntry } from "./watch_driver.js";
import type { TargetRecord, ViewSel, ViewToken } from "./wire/types.js";

export const PANEL_KIND = "experiment-dashboard/v10";

/** The workflow watch is always wanted; `true` is its whole spec. */
export type WorkflowSpec = true;

/** One grid's watch spec (`watch_cells(path)`). */
export type CellsSpec = { readonly path: string };

/** One cell's watch spec (`watch_cell(path, coords)`). */
export type CellSpec = { readonly path: string; readonly coords: string };

/** The grid-graph watch is workflow-wide; it is wanted only while the
 * plate tab is visible (`true` when subscribed, else the slot is null). */
export type GraphSpec = true;

export type WatchSlots = {
	readonly workflow: WatchEntry<WorkflowSpec, WorkflowMirror> | null;
	readonly cells: WatchEntry<CellsSpec, CellsMirror> | null;
	readonly cell: WatchEntry<CellSpec, CellMirror> | null;
	/** Live only on the plate tab (navigation drops it, unsubscribing). */
	readonly graph: WatchEntry<GraphSpec, GraphMirror> | null;
};

/** One capability call inside a mutation gesture. A discriminated
 * union (not `method` + loose args) so `allocateStep` can use the
 * typed invoke overload per method. Steps persist in `ownStorage`
 * (`ViewSel`s are plain data; `TargetRecord` carries a ref stub,
 * which `StateValue` supports), so a chain survives navigation. */
export type ActionStep =
	| { readonly kind: "add_target"; readonly sel: ViewSel; readonly adopt: boolean }
	| { readonly kind: "remove_target"; readonly sel: ViewSel }
	/** The user-demand channel (`read_cell_entity`): demands task
	 * outputs per the demand policy; requires an attached target. */
	| { readonly kind: "demand"; readonly sel: ViewSel }
	/** The by-record demand — the record round-trips VERBATIM from
	 * the targets mirror (the wire-symmetry contract). */
	| { readonly kind: "demand_target"; readonly record: TargetRecord }
	| { readonly kind: "clear_cells"; readonly sel: ViewSel }
	| { readonly kind: "delete_cell"; readonly sel: ViewSel }
	| {
			readonly kind: "delete_grid";
			readonly token: ViewToken;
			readonly key: string;
			readonly recursive: boolean;
	  }
	| { readonly kind: "source_grid"; readonly sel: ViewSel; readonly key: string }
	| {
			readonly kind: "revoke_grid_sourcing";
			readonly sel: ViewSel;
			readonly key: string;
			readonly force: boolean;
	  }
	| { readonly kind: "source_cell"; readonly sel: ViewSel }
	| { readonly kind: "revoke_cell_sourcing"; readonly sel: ViewSel };

/** One user gesture in the mutation log: `current` is the running
 * step's invoke; `pending` are the steps after it (advanced by the
 * log row when `current` fills; an errored step stops the chain). */
export type ActionEntry = {
	readonly id: number;
	readonly label: string;
	readonly current: OpResultStub<unknown>;
	readonly pending: readonly ActionStep[];
};

export type HeaderList = "targets" | "sourcing" | "bindings" | "lint";

export type PanelUiState = {
	/** Which visualization is showing: the tree/detail browser, or the
	 * plate-notation graph. */
	readonly tab: "tree" | "plates";
	/** The tree selection (a grid key-path text), driving the detail pane. */
	readonly selectedPath: string | null;
	/** The selected cell row in the detail pane (coords text), for the
	 * drawer. Reset on grid navigation — except that navigating to an
	 * axis-less grid selects its one possible cell (`"[]"`) outright. */
	readonly selectedCell: string | null;
	/** Collapsed tree nodes (path texts; default expanded). */
	readonly collapsed: readonly string[];
	/** Task grids whose internals subtrees are revealed in the tree. */
	readonly revealedInternals: readonly string[];
	/** Which workflow-level list is open under the header. */
	readonly openList: HeaderList | null;
	/** Whether the tree pane's symbol legend is expanded. */
	readonly legendOpen: boolean;
};

export type PanelState = {
	readonly kind: typeof PANEL_KIND;
	readonly watches: WatchSlots;
	readonly actions: readonly ActionEntry[];
	/** The drawer's frame-entity load slots (`frame_table.tsx`):
	 * entity shortId → `OpResultStub`, capped there. */
	readonly frameLoads: FrameLoads;
	readonly ui: PanelUiState;
};

export const PANEL_INITIAL: PanelState = {
	kind: PANEL_KIND,
	watches: { workflow: null, cells: null, cell: null, graph: null },
	actions: [],
	frameLoads: {},
	ui: {
		tab: "tree",
		selectedPath: null,
		selectedCell: null,
		collapsed: [],
		revealedInternals: [],
		openList: null,
		legendOpen: false,
	},
};

export function isPanelState(v: PanelState): boolean {
	// The kind tag plus the top-level shape. The structural half
	// matters under HMR: a mid-edit module (kind already bumped,
	// `PANEL_INITIAL` not yet grown) can persist a tagged-but-
	// incomplete object through the drivers' entry writes, and a
	// tag-only check would then accept it forever.
	return (
		v != null &&
		typeof v === "object" &&
		v.kind === PANEL_KIND &&
		typeof v.watches === "object" &&
		v.watches !== null &&
		"workflow" in v.watches &&
		"cells" in v.watches &&
		"cell" in v.watches &&
		"graph" in v.watches &&
		Array.isArray(v.actions) &&
		typeof v.frameLoads === "object" &&
		v.frameLoads !== null &&
		typeof v.ui === "object" &&
		v.ui !== null &&
		"tab" in v.ui
	);
}

export function toggle(list: readonly string[], item: string): readonly string[] {
	return list.includes(item) ? list.filter((x) => x !== item) : [...list, item];
}
