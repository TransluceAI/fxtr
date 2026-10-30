/**
 * Pure derivation: fold `summarize`'s flat `GridSummary` rows (key-path
 * text like `["a","b"]`) into the dashboard's tree
 * (`notes/monitoring_ui.md` — nodes are key-paths/grids, not cells).
 *
 * Two wrinkles the fold owns:
 *
 * - **Aggregation**: subtree grids report zero rollups (their cells
 *   materialize through descendants), so every node carries `agg` —
 *   its own counts plus all descendants' — for the at-a-glance badges.
 * - **Internals marking**: task-cell internals are ordinary grids
 *   whose paths extend a task grid's path. The tree hides them by
 *   default behind a per-task reveal toggle (the drawer's "browse
 *   internals"), so each node under a task ancestor is marked with
 *   `internalsOf` = that task's path text.
 */

import type { GridSummary } from "../wire/types.js";

export type RollupCounts = {
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

export type GridTreeNode = {
	/** The grid's own key (last path segment). */
	readonly key: string;
	/** The grid's key-path text (the wire identity, e.g. `["a","b"]`). */
	readonly path: string;
	/** The summary row, when this grid exists (an implicit intermediate
	 * node — a path prefix with no row of its own — has `null`). */
	readonly summary: GridSummary | null;
	/** Own + descendant rollups. */
	readonly agg: RollupCounts;
	/** Set for grids inside a task grid's internals subtree: the owning
	 * task grid's path text. */
	readonly internalsOf: string | null;
	readonly children: readonly GridTreeNode[];
};

export type GridTree = {
	readonly roots: readonly GridTreeNode[];
	/** Path text → node, for selection lookups. */
	readonly byPath: ReadonlyMap<string, GridTreeNode>;
};

const ZERO: RollupCounts = {
	populated: 0,
	n_pending: 0,
	n_running: 0,
	n_complete: 0,
	n_errored: 0,
	n_interrupted: 0,
	n_stale: 0,
	n_unsettled: 0,
	targets_attached: 0,
	targets_dangling: 0,
};

function addCounts(a: RollupCounts, b: RollupCounts): RollupCounts {
	return {
		populated: a.populated + b.populated,
		n_pending: a.n_pending + b.n_pending,
		n_running: a.n_running + b.n_running,
		n_complete: a.n_complete + b.n_complete,
		n_errored: a.n_errored + b.n_errored,
		n_interrupted: a.n_interrupted + b.n_interrupted,
		n_stale: a.n_stale + b.n_stale,
		n_unsettled: a.n_unsettled + b.n_unsettled,
		targets_attached: a.targets_attached + b.targets_attached,
		targets_dangling: a.targets_dangling + b.targets_dangling,
	};
}

/** Parse key-path text (canonical JSON array of key strings). Returns
 * null on anything malformed — the caller drops the row rather than
 * crashing the tree. */
export function parseKeyPath(path: string): readonly string[] | null {
	try {
		const parsed: unknown = JSON.parse(path);
		if (!Array.isArray(parsed) || !parsed.every((k) => typeof k === "string")) return null;
		return parsed;
	} catch {
		return null;
	}
}

export function encodeKeyPath(keys: readonly string[]): string {
	return JSON.stringify(keys);
}

type MutableNode = {
	key: string;
	path: string;
	summary: GridSummary | null;
	internalsOf: string | null;
	children: Map<string, MutableNode>;
};

export function buildGridTree(grids: readonly GridSummary[]): GridTree {
	const rootChildren = new Map<string, MutableNode>();

	const nodeFor = (keys: readonly string[]): MutableNode => {
		let siblings = rootChildren;
		let node: MutableNode | undefined;
		for (let i = 0; i < keys.length; i++) {
			const key = keys[i] as string;
			node = siblings.get(key);
			if (node === undefined) {
				node = {
					key,
					path: encodeKeyPath(keys.slice(0, i + 1)),
					summary: null,
					internalsOf: null,
					children: new Map(),
				};
				siblings.set(key, node);
			}
			siblings = node.children;
		}
		// keys is nonempty for every summarize row (the root itself has
		// no row), so `node` is always assigned.
		return node as MutableNode;
	};

	for (const grid of grids) {
		const keys = parseKeyPath(grid.path);
		if (keys === null || keys.length === 0) continue;
		nodeFor(keys).summary = grid;
	}

	// Second pass: internals marking + aggregation, one DFS.
	const byPath = new Map<string, GridTreeNode>();

	const finalize = (node: MutableNode, taskAncestor: string | null): GridTreeNode => {
		const internalsOf = taskAncestor;
		const childTaskAncestor = taskAncestor ?? (node.summary?.kind === "task" ? node.path : null);
		const children = [...node.children.values()]
			.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
			.map((c) => finalize(c, childTaskAncestor));
		let agg = node.summary === null ? ZERO : addCounts(ZERO, node.summary);
		for (const c of children) agg = addCounts(agg, c.agg);
		const out: GridTreeNode = {
			key: node.key,
			path: node.path,
			summary: node.summary,
			agg,
			internalsOf,
			children,
		};
		byPath.set(out.path, out);
		return out;
	};

	const roots = [...rootChildren.values()]
		.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
		.map((n) => finalize(n, null));

	return { roots, byPath };
}
