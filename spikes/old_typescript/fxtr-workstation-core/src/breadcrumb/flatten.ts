/**
 * Flatten a recursive `HistoryRoot` into a list of entries suitable for
 * rendering in the history-root menu. The flat list is ordered
 * deepest-first (nearest-to-namespace at index 0); each entry knows
 * which segment it belongs to so that restoring preserves the
 * unflattened structure.
 *
 * See `computeOpenNewFromHistoryEntry` in `history.ts` — it takes a
 * `FlatHistoryEntry` and rebuilds the restored panel's state using the
 * segment's prefix + parent, not the flat list itself.
 */

import type { FlatHistoryEntry } from "./history.js";
import type { HistoryRoot } from "./types.js";

export function flattenHistoryRoot(root: HistoryRoot): FlatHistoryEntry[] {
	// Walk from the root toward the deepest parent, collecting segments.
	// The traversal sees them shallowest-first, so we unshift into a
	// deepest-first ordering.
	const segments: Array<{ prefix: string[]; parent: HistoryRoot }> = [];
	let node: HistoryRoot = root;
	while (node.kind === "branched") {
		segments.unshift({ prefix: node.prefix, parent: node.parent });
		node = node.parent;
	}
	const out: FlatHistoryEntry[] = [];
	segments.forEach((seg, segmentIndex) => {
		seg.prefix.forEach((entityIdString, positionInSegment) => {
			out.push({
				entityIdString,
				segmentIndex,
				positionInSegment,
				segmentPrefix: seg.prefix,
				segmentParent: seg.parent,
			});
		});
	});
	return out;
}
