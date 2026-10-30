/**
 * Pure transformations over per-panel chain state.
 *
 * Each function takes the relevant inputs and returns a new
 * `RefPanelChainState`. No side effects, no ref resolution
 * against the connector — callers thread in stub-bearing
 * `PanelViewEntry`s, and the transforms just shuffle them.
 *
 * Chain entries are stub-bearing `PanelViewEntry`s. Entry equality
 * compares stub identity (and stub shortId + kind as a defensive
 * fallback) AND `display` — the metadata view of a ref is a
 * distinct chain entry from its default view, which is what lets
 * "Open metadata here" on the currently-active ref append a new
 * entry rather than no-op'ing.
 */

import { EntityRefStub } from "@fxtr-ui/core";
import type { PanelViewEntry } from "../types.js";
import type { Breadcrumb, HistoryRoot, RefPanelChainState } from "./types.js";

/**
 * Panel-view-entry equality. Two entries match when they refer to
 * the same stub instance (and therefore the same kind + shortId,
 * since stubs are connector-interned) and have the same display
 * mode. The display field matters because the metadata view of a
 * ref is a separate user-facing entry from its default view.
 *
 * We compare on `stub` identity (`a.stub === b.stub`) when both
 * stubs are minted on the same connector; we fall back to a
 * shortId + kind check for the cross-decoder case (e.g. a stub
 * that came back through `unescapeWire` after a snapshot decode,
 * which interns through the same connector and so resolves to the
 * same instance — but the fallback covers a hypothetical
 * mismatch defensively).
 */
function sameRef(a: PanelViewEntry, b: PanelViewEntry): boolean {
	if (a.display !== b.display) return false;
	if (a.stub === b.stub) return true;
	return a.stub.shortId === b.stub.shortId && sameKind(a.stub, b.stub);
}

function sameKind(a: PanelViewEntry["stub"], b: PanelViewEntry["stub"]): boolean {
	return a instanceof EntityRefStub === b instanceof EntityRefStub;
}

/**
 * Index of `target` in `chain`, or `-1` if absent.
 */
function indexOfRef(chain: ReadonlyArray<PanelViewEntry>, target: PanelViewEntry): number {
	for (let i = 0; i < chain.length; i++) {
		const entry = chain[i];
		if (entry !== undefined && sameRef(entry, target)) return i;
	}
	return -1;
}

// ---------------------------------------------------------------------------
// replace-in-source: mutate a panel's chain in response to clicking a
// content link inside it.
// ---------------------------------------------------------------------------

/**
 * Apply the "click a content link" rule to an existing panel
 * state.
 *
 *   - target === chain[activeIndex]:        no-op (already there)
 *   - target === chain[activeIndex + 1]:    activeIndex += 1 (forward)
 *   - target appears at some earlier index: jump there, keep future intact
 *   - otherwise:                            truncate future past active,
 *                                            append target, activate it
 *
 * The historyRoot is never touched — it represents how the panel
 * was reached, not how it's been navigated within.
 */
export function computeNextChainState(
	source: RefPanelChainState,
	target: PanelViewEntry,
): RefPanelChainState {
	const { chain, activeIndex, historyRoot, storages } = source;
	const active = chain[activeIndex];
	if (active !== undefined && sameRef(active, target)) return source;
	const forward = chain[activeIndex + 1];
	if (forward !== undefined && sameRef(forward, target)) {
		return { chain, activeIndex: activeIndex + 1, historyRoot, storages };
	}
	const existing = indexOfRef(chain, target);
	if (existing !== -1) {
		return { chain, activeIndex: existing, historyRoot, storages };
	}
	const newChain = [...chain.slice(0, activeIndex + 1), target];
	const newStorages = [...storages.slice(0, activeIndex + 1), null];
	return {
		chain: newChain,
		activeIndex: newChain.length - 1,
		historyRoot,
		storages: newStorages,
	};
}

// ---------------------------------------------------------------------------
// open-new-from-link: branch into a fresh panel by following a link
// from inside an existing ref panel.
// ---------------------------------------------------------------------------

/**
 * Derive a fresh panel state from a link-follow with a real
 * source panel.
 *
 * The new chain is the `to`-sequence of the trail (each ref
 * visited on the way from `source.active` to `target`, via any
 * nested hover / embed steps). The new historyRoot captures the
 * source's chain-up-to-active and the source's own historyRoot.
 *
 * `trail` must be non-empty; callers ensure this via `buildTrail`,
 * which always appends the target as the last segment.
 */
export function computeOpenNewFromLink(
	source: RefPanelChainState,
	trail: ReadonlyArray<Breadcrumb>,
): RefPanelChainState {
	if (trail.length === 0) {
		throw new Error("computeOpenNewFromLink: trail must be non-empty");
	}
	const newChain = trail.map((seg) => seg.to);
	return {
		chain: newChain,
		activeIndex: newChain.length - 1,
		historyRoot: {
			kind: "branched",
			prefix: source.chain.slice(0, source.activeIndex + 1),
			parent: source.historyRoot,
		},
		storages: newChain.map(() => null),
	};
}

// ---------------------------------------------------------------------------
// open-new-from-chain-entry: drag / right-click a breadcrumb entry.
// ---------------------------------------------------------------------------

/**
 * A rewound copy of the source panel with the historyRoot
 * unchanged. New chain = `source.chain[..chainIndex+1]`, new
 * activeIndex = `chainIndex`.
 */
export function computeOpenNewFromChainEntry(
	source: RefPanelChainState,
	chainIndex: number,
): RefPanelChainState {
	if (chainIndex < 0 || chainIndex >= source.chain.length) {
		throw new Error(
			`computeOpenNewFromChainEntry: chainIndex ${chainIndex} out of range for chain of length ${source.chain.length}`,
		);
	}
	// The new chain opens in a fresh panel session; storages from
	// the source would contain `OpResultStub`s scoped to the source's
	// session, which the destination's flush walk would substitute
	// to broken-sentinels anyway. Start with empties so the
	// destination renderers allocate fresh.
	const newChain = source.chain.slice(0, chainIndex + 1);
	return {
		chain: newChain,
		activeIndex: chainIndex,
		historyRoot: source.historyRoot,
		storages: newChain.map(() => null),
	};
}

// ---------------------------------------------------------------------------
// History-root menu: flat entries, click/drag restore.
// ---------------------------------------------------------------------------

/**
 * A single entry in the flattened history-root list. Each entry
 * knows its ref, its position in the flat list (`segmentIndex`
 * + `positionInSegment`), and the owning segment's prefix +
 * parent historyRoot. The latter two are what
 * `computeOpenNewFromHistoryEntry` uses to reconstruct the
 * restored panel's state.
 *
 * `segmentIndex === 0` is the deepest (nearest-to-roots) segment;
 * higher indices are closer to the current panel's direct parent.
 */
export interface FlatHistoryEntry {
	readonly ref: PanelViewEntry;
	readonly segmentIndex: number;
	readonly positionInSegment: number;
	readonly segmentPrefix: ReadonlyArray<PanelViewEntry>;
	readonly segmentParent: HistoryRoot;
}

/**
 * Restore a state from a flat history entry.
 *
 * The new panel's chain is the owning segment's prefix truncated
 * at this entry; the new historyRoot is the owning segment's
 * parent (i.e. what comes *above* this segment in the recursive
 * tree). This preserves the unflattened structure: restoring
 * into an entry inside segment `s` exposes everything above `s`
 * as the new historyRoot.
 */
export function computeOpenNewFromHistoryEntry(entry: FlatHistoryEntry): RefPanelChainState {
	const newChain = entry.segmentPrefix.slice(0, entry.positionInSegment + 1);
	return {
		chain: newChain,
		activeIndex: entry.positionInSegment,
		historyRoot: entry.segmentParent,
		storages: newChain.map(() => null),
	};
}

// ---------------------------------------------------------------------------
// Non-panel origins: build a snapshot from a trail when there's no
// source panel to branch from (e.g. a click on a roots row).
// ---------------------------------------------------------------------------

/**
 * Build a fresh panel state from a trail alone.
 *
 * Distinct from `computeOpenNewFromLink`:
 *
 *   - there's no source panel, so historyRoot defaults to
 *     `{ kind: "roots" }` (callers can override if some other
 *     non-panel surface needs a different root);
 *   - we include the trail's first `from` ref in the chain if
 *     present. In the non-panel case that `from` is an actual
 *     ref the user passed through (e.g. a roots row reached via
 *     a hover), not a source-panel active that already lives in
 *     historyRoot. So it belongs in the chain.
 *
 * Trail must be non-empty.
 */
export function computeSnapshotFromTrail(
	trail: ReadonlyArray<Breadcrumb>,
	historyRoot: HistoryRoot = { kind: "roots" },
): RefPanelChainState {
	if (trail.length === 0) {
		throw new Error("computeSnapshotFromTrail: trail must be non-empty");
	}
	const refs: PanelViewEntry[] = [];
	const first = trail[0];
	if (first !== undefined && first.from !== undefined) {
		refs.push(first.from);
	}
	for (const seg of trail) {
		refs.push(seg.to);
	}
	return {
		chain: refs,
		activeIndex: refs.length - 1,
		historyRoot,
		storages: refs.map(() => null),
	};
}
