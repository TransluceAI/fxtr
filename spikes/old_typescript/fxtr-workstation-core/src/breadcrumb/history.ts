/**
 * Pure transformations over per-panel history state.
 *
 * Each function takes the relevant inputs and returns a new
 * `EntityPanelParams`. No side effects, no FxtrID loads. These are
 * the primitives the workstation API methods (`followLink`,
 * `activateChainEntry`, `restoreIntoNewPanel`) call when they need to
 * compute the next state of a panel.
 */

import type { Breadcrumb, EntityPanelParams, HistoryRoot } from "./types.js";

// ---------------------------------------------------------------------------
// replace-in-source: mutate a panel's chain in response to clicking a
// content link inside it.
// ---------------------------------------------------------------------------

/**
 * Apply the "click a content link" rule to an existing panel state.
 *
 * - target === chain[activeIndex]:        no-op (already there)
 * - target === chain[activeIndex + 1]:    activeIndex += 1 (press forward)
 * - target appears at some earlier index: jump there, keep future intact
 * - otherwise:                            truncate future past active,
 *                                         append target, activate it
 *
 * The historyRoot is never touched — it represents how the panel was
 * reached, not how it's been navigated within.
 */
export function computeNextChainState(
	source: EntityPanelParams,
	targetIdString: string,
): EntityPanelParams {
	const { chain, activeIndex, historyRoot } = source;
	if (chain[activeIndex] === targetIdString) return source;
	if (chain[activeIndex + 1] === targetIdString) {
		return { chain, activeIndex: activeIndex + 1, historyRoot };
	}
	const existing = chain.indexOf(targetIdString);
	if (existing !== -1) {
		return { chain, activeIndex: existing, historyRoot };
	}
	const newChain = [...chain.slice(0, activeIndex + 1), targetIdString];
	return { chain: newChain, activeIndex: newChain.length - 1, historyRoot };
}

// ---------------------------------------------------------------------------
// open-new-from-link: branch into a fresh panel by following a link
// from inside an existing entity panel.
// ---------------------------------------------------------------------------

/**
 * Derive a fresh panel state from a link-follow with a real source
 * panel.
 *
 * The new chain is the `to`-sequence of the trail (each entity visited
 * on the way from source.active to target, via any nested hover /
 * embed steps). The new historyRoot captures source's chain-up-to-
 * active and source's own historyRoot.
 *
 * `trail` must be non-empty; this is ensured by the callers
 * (`buildTrail` always appends the target as the last segment).
 */
export function computeOpenNewFromLink(
	source: EntityPanelParams,
	trail: Breadcrumb[],
): EntityPanelParams {
	if (trail.length === 0) {
		throw new Error("computeOpenNewFromLink: trail must be non-empty");
	}
	const newChain = trail.map((seg) => seg.toEntityId);
	return {
		chain: newChain,
		activeIndex: newChain.length - 1,
		historyRoot: {
			kind: "branched",
			prefix: source.chain.slice(0, source.activeIndex + 1),
			parent: source.historyRoot,
		},
	};
}

// ---------------------------------------------------------------------------
// open-new-from-chain-entry: drag / right-click a breadcrumb entry.
// ---------------------------------------------------------------------------

/**
 * Option (c) from the plan: a rewound copy of the source panel with
 * the historyRoot unchanged. New chain = source.chain[..chainIndex+1],
 * new activeIndex = chainIndex.
 */
export function computeOpenNewFromChainEntry(
	source: EntityPanelParams,
	chainIndex: number,
): EntityPanelParams {
	if (chainIndex < 0 || chainIndex >= source.chain.length) {
		throw new Error(
			`computeOpenNewFromChainEntry: chainIndex ${chainIndex} out of range for chain of length ${source.chain.length}`,
		);
	}
	return {
		chain: source.chain.slice(0, chainIndex + 1),
		activeIndex: chainIndex,
		historyRoot: source.historyRoot,
	};
}

// ---------------------------------------------------------------------------
// History-root menu: flat entries, click/drag restore.
// ---------------------------------------------------------------------------

/**
 * A single entry in the flattened history-root list. Each entry knows
 * its entity id, its position in the flat list (`segmentIndex` +
 * `positionInSegment`), and the owning segment's prefix + parent
 * historyRoot. The latter two are what `computeOpenNewFromHistoryEntry`
 * uses to reconstruct the restored panel's state.
 *
 * `segmentIndex === 0` is the deepest (nearest-to-namespace) segment;
 * higher indices are closer to the current panel's direct parent.
 */
export interface FlatHistoryEntry {
	entityIdString: string;
	segmentIndex: number;
	positionInSegment: number;
	segmentPrefix: string[];
	segmentParent: HistoryRoot;
}

/**
 * Restore a state from a flat history entry.
 *
 * The new panel's chain is the owning segment's prefix truncated at
 * this entry; the new historyRoot is the owning segment's parent
 * (i.e. what comes *above* this segment in the recursive tree). This
 * preserves the unflattened structure: restoring into an entry inside
 * segment `s` exposes everything above `s` as the new historyRoot.
 */
export function computeOpenNewFromHistoryEntry(entry: FlatHistoryEntry): EntityPanelParams {
	return {
		chain: entry.segmentPrefix.slice(0, entry.positionInSegment + 1),
		activeIndex: entry.positionInSegment,
		historyRoot: entry.segmentParent,
	};
}

// ---------------------------------------------------------------------------
// Non-panel origins: build a snapshot from a trail when there's no
// source panel to branch from (e.g. a click on a namespace pin).
// ---------------------------------------------------------------------------

/**
 * Build a fresh panel state from a trail alone.
 *
 * Distinct from `computeOpenNewFromLink`:
 *
 * - there's no source panel, so historyRoot defaults to
 *   `{ kind: "namespace" }` (callers can override if some other
 *   non-panel surface needs a different root);
 * - we include the trail's first `fromEntityId` in the chain if
 *   present. In the non-panel case that `from` is an actual
 *   entity the user passed through (e.g. a namespace pin row's
 *   pin entity reached via a hover), not a source-panel active
 *   that already lives in historyRoot. So it belongs in the chain.
 *
 * Trail must be non-empty.
 */
export function computeSnapshotFromTrail(
	trail: Breadcrumb[],
	historyRoot: HistoryRoot = { kind: "namespace" },
): EntityPanelParams {
	if (trail.length === 0) {
		throw new Error("computeSnapshotFromTrail: trail must be non-empty");
	}
	const ids: string[] = [];
	const first = trail[0];
	if (first && first.fromEntityId !== undefined) {
		ids.push(first.fromEntityId);
	}
	for (const seg of trail) {
		ids.push(seg.toEntityId);
	}
	return {
		chain: ids,
		activeIndex: ids.length - 1,
		historyRoot,
	};
}
