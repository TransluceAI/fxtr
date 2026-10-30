/**
 * Thin helpers over the wire types: selection construction (the sorted
 * `bound` pairs contract) and the LintReport derived flags (Python-side
 * `@property`s that never cross the wire).
 */

import type { CoordValue, LintReport, ViewSel, ViewToken } from "./types.js";

/** Build a `ViewSel` in canonical wire form: `bound` as pairs sorted by
 * position (runner_api's `_BoundStrategy` contract). Positions are
 * indexes into the GRANT's `axes` order. */
export function viewSel(
	token: ViewToken,
	bound: Readonly<Record<number, CoordValue>> = {},
	output: string | null = null,
): ViewSel {
	const pairs = Object.entries(bound)
		.map(([k, v]) => [Number(k), v] as const)
		.sort((a, b) => a[0] - b[0]);
	return { token, bound: pairs, output };
}

/** The fully-bound selection map for one cell: its coords array (in
 * the grant's axes order — the full flattened frame, as served by
 * `list_cells`) as position → value. Feed to `viewSel`. */
export function boundByPosition(
	coords: readonly CoordValue[],
): Readonly<Record<number, CoordValue>> {
	return Object.fromEntries(coords.map((v, i) => [i, v]));
}

/** `LintReport.sound`: no dangling intent and no red targets. */
export function lintSound(report: LintReport): boolean {
	return (
		report.dangling_sourcing.length === 0 &&
		report.dangling_targets.length === 0 &&
		report.target_findings.length === 0
	);
}

/** `LintReport.clean`: sound, with an empty ungrounded inventory. */
export function lintClean(report: LintReport): boolean {
	return lintSound(report) && report.ungrounded.length === 0;
}
