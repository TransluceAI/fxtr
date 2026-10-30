/**
 * The shared cell-status badge vocabulary — one source of truth for the
 * symbols and colours used for execution/currency status counts, so the
 * tree view (`tree_view.tsx`) and the plate view (`plate_view.tsx`) render
 * them identically. Populated totals and target markers are rendered
 * per-view (they differ in placement); this covers the status counts.
 */

import type { RollupCounts } from "./tree.js";

export type StatusBadge = {
	/** The status glyph (matches the tree legend). */
	readonly sym: string;
	readonly color: string;
	readonly count: number;
	readonly label: string;
};

/** The nonzero status counts of `agg`, in a fixed order, each with its
 * canonical glyph + colour. */
export function statusBadges(agg: RollupCounts): StatusBadge[] {
	const out: StatusBadge[] = [];
	if (agg.n_running > 0)
		out.push({ sym: "▶", color: "#2266cc", count: agg.n_running, label: "running" });
	if (agg.n_errored > 0)
		out.push({ sym: "✕", color: "#c04040", count: agg.n_errored, label: "errored" });
	if (agg.n_interrupted > 0)
		out.push({ sym: "‼", color: "#b06010", count: agg.n_interrupted, label: "interrupted" });
	if (agg.n_stale > 0) out.push({ sym: "~", color: "#907000", count: agg.n_stale, label: "stale" });
	if (agg.n_unsettled > 0)
		out.push({ sym: "?", color: "#907000", count: agg.n_unsettled, label: "unsettled" });
	return out;
}
