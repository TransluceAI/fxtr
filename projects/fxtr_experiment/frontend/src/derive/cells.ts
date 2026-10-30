/**
 * Pure derivation for the cells table: pair each `CellInfo` with its
 * canonical coords text (compact JSON — matches the Python side's
 * `encode_coords`, so it doubles as the join key against
 * `TargetInfo.coords` / `SourcingInfo.coords`), then filter and sort
 * client-side (`notes/monitoring_ui.md`: no server paging in v0).
 */

import type { CellInfo } from "../wire/types.js";

export type CellRow = {
	readonly cell: CellInfo;
	/** Canonical coords text — the row identity and the targets join key. */
	readonly coordsText: string;
	/** Whether a real (populated) cell backs this row. `false` for a
	 * **phantom** row synthesized for a targeted cell that has not been
	 * populated yet ("targeted, not run") — value-free, but selectable
	 * so it can be run from the drawer. */
	readonly populated: boolean;
};

/** Parse a canonical coords-text (JSON array of str|int) back to the
 * coord tuple; null on anything malformed. */
function parseCoords(text: string): readonly (string | number)[] | null {
	try {
		const v: unknown = JSON.parse(text);
		if (!Array.isArray(v) || !v.every((x) => typeof x === "string" || typeof x === "number")) {
			return null;
		}
		return v;
	} catch {
		return null;
	}
}

export type CellSortKey =
	| { readonly kind: "coord"; readonly i: number }
	| { readonly kind: "status" };

export type CellSort = { readonly key: CellSortKey; readonly dir: 1 | -1 };

export function sameSortKey(a: CellSortKey, b: CellSortKey): boolean {
	return a.kind === "coord" && b.kind === "coord" ? a.i === b.i : a.kind === b.kind;
}

/** One badge in a cell's composite status column, colored by `kind`
 * (the caller owns the color scales). */
export type CellBadge = {
	readonly text: string;
	readonly kind: "status" | "currency" | "grounding";
};

/** The cell's status column, folded: the exec status (when present)
 * plus ONLY the abnormal currency/grounding — `fresh` and `grounded`
 * are the healthy defaults and are dropped, so a healthy task cell
 * reads just "complete" and a data cell reads nothing. */
export function cellBadges(cell: CellInfo): readonly CellBadge[] {
	const out: CellBadge[] = [];
	if (cell.status !== null) out.push({ text: cell.status, kind: "status" });
	if (cell.currency === "stale" || cell.currency === "unsettled") {
		out.push({ text: cell.currency, kind: "currency" });
	}
	if (cell.grounding === "unanchored" || cell.grounding === "orphaned") {
		out.push({ text: cell.grounding, kind: "grounding" });
	}
	return out;
}

/** The combined want/target indicator (color = targeted vs wanted;
 * fill = populated vs not — the tree pane's language):
 * - `targeted-run`: targeted and populated (green filled ●);
 * - `targeted-pending`: targeted, not yet run (green hollow ◌);
 * - `wanted`: wanted by a reader, not directly targeted — always
 *   populated, since a wanted-not-targeted cell is only visible once it
 *   exists (grey filled ●);
 * - null: neither.
 */
export type CellDemand = "targeted-run" | "targeted-pending" | "wanted" | null;

export function cellDemand(cell: CellInfo, targeted: boolean, populated: boolean): CellDemand {
	if (targeted) return populated ? "targeted-run" : "targeted-pending";
	if (cell.wanted === true) return "wanted";
	return null;
}

/** Filter (case-insensitive substring across coords text + status
 * fields + reason) and sort (default: coords text ascending).
 *
 * `targetCoordsTexts` are the grid's targeted coords (canonical text):
 * any not backed by a populated cell get a **phantom** row so
 * targeted-but-not-yet-run cells are visible (and selectable → runnable
 * from the drawer). A phantom is value-free with `wanted: true`
 * (targeting implies wanted). */
export function deriveCellRows(
	cells: readonly CellInfo[],
	targetCoordsTexts: readonly string[],
	filter: string,
	sort: CellSort | null,
): readonly CellRow[] {
	const needle = filter.trim().toLowerCase();
	const present = new Set<string>();
	let rows: CellRow[] = cells.map((cell): CellRow => {
		const coordsText = JSON.stringify(cell.coords);
		present.add(coordsText);
		return { cell, coordsText, populated: true };
	});
	for (const text of targetCoordsTexts) {
		if (present.has(text)) continue;
		const coords = parseCoords(text);
		if (coords === null) continue;
		present.add(text);
		rows.push({
			cell: {
				coords,
				status: null,
				currency: null,
				reason: null,
				grounding: null,
				wanted: true,
			},
			coordsText: text,
			populated: false,
		});
	}
	if (needle !== "") {
		rows = rows.filter(({ cell, coordsText }) =>
			[coordsText, cell.status, cell.currency, cell.grounding, cell.reason]
				.filter(Boolean)
				.some((s) => String(s).toLowerCase().includes(needle)),
		);
	}
	const cmp = (a: CellRow, b: CellRow): number => {
		if (sort === null)
			return a.coordsText < b.coordsText ? -1 : a.coordsText > b.coordsText ? 1 : 0;
		if (sort.key.kind === "coord") {
			const av = a.cell.coords[sort.key.i] ?? "";
			const bv = b.cell.coords[sort.key.i] ?? "";
			return (av < bv ? -1 : av > bv ? 1 : 0) * sort.dir;
		}
		const av = a.cell.status ?? "";
		const bv = b.cell.status ?? "";
		return (av < bv ? -1 : av > bv ? 1 : 0) * sort.dir;
	};
	return [...rows].sort(cmp);
}
