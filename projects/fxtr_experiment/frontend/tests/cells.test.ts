/**
 * The cells-table derivation (`src/derive/cells.ts`): coords-text row
 * identity (the targets join key — compact JSON, matching the Python
 * `encode_coords` form pinned by the dialect harness), filtering, and
 * the sort keys.
 */

import { describe, expect, it } from "vitest";

import {
	type CellSort,
	cellBadges,
	cellDemand,
	deriveCellRows,
	sameSortKey,
} from "../src/derive/cells.js";
import type { CellInfo } from "../src/wire/types.js";

function cell(partial: Partial<CellInfo> & { coords: CellInfo["coords"] }): CellInfo {
	return {
		status: null,
		currency: null,
		reason: null,
		grounding: null,
		wanted: null,
		...partial,
	};
}

const CELLS: readonly CellInfo[] = [
	cell({ coords: ["b", 1], status: "complete", currency: "stale", reason: "input moved" }),
	cell({ coords: ["a", 2], status: "errored", currency: "fresh", reason: "boom" }),
	cell({ coords: ["a", 1], status: "running", grounding: "orphaned" }),
];

describe("deriveCellRows", () => {
	it("pairs rows with compact-JSON coords text and sorts by it by default", () => {
		const rows = deriveCellRows(CELLS, [], "", null);
		expect(rows.map((r) => r.coordsText)).toEqual(['["a",1]', '["a",2]', '["b",1]']);
		// The join-key form: compact JSON, ints unquoted (encode_coords).
		expect(rows[0]?.cell.status).toBe("running");
	});

	it("filters case-insensitively across coords text, statuses, and reason", () => {
		expect(deriveCellRows(CELLS, [], "BOOM", null).map((r) => r.coordsText)).toEqual(['["a",2]']);
		expect(deriveCellRows(CELLS, [], "stale", null).map((r) => r.coordsText)).toEqual(['["b",1]']);
		expect(deriveCellRows(CELLS, [], '"b"', null).map((r) => r.coordsText)).toEqual(['["b",1]']);
		expect(deriveCellRows(CELLS, [], "orphaned", null)).toHaveLength(1);
		expect(deriveCellRows(CELLS, [], "nope", null)).toEqual([]);
	});

	it("sorts by a coord position in either direction", () => {
		const asc: CellSort = { key: { kind: "coord", i: 1 }, dir: 1 };
		expect(deriveCellRows(CELLS, [], "", asc).map((r) => r.cell.coords[1])).toEqual([1, 1, 2]);
		const desc: CellSort = { key: { kind: "coord", i: 0 }, dir: -1 };
		expect(deriveCellRows(CELLS, [], "", desc).map((r) => r.cell.coords[0])).toEqual([
			"b",
			"a",
			"a",
		]);
	});

	it("sorts by status (missing statuses sort as empty)", () => {
		const rows = deriveCellRows([...CELLS, cell({ coords: ["c", 0] })], [], "", {
			key: { kind: "status" },
			dir: 1,
		});
		expect(rows.map((r) => r.cell.status)).toEqual([null, "complete", "errored", "running"]);
	});

	it("adds phantom rows for targeted coords with no populated cell", () => {
		// One populated cell + two targets, one of which is already
		// populated (no phantom) and one that isn't (a phantom row).
		const rows = deriveCellRows(
			[cell({ coords: ["a", 1], status: "complete" })],
			['["a",1]', '["b",2]'],
			"",
			null,
		);
		const byCoords = new Map(rows.map((r) => [r.coordsText, r]));
		expect(byCoords.get('["a",1]')?.populated).toBe(true);
		const phantom = byCoords.get('["b",2]');
		expect(phantom?.populated).toBe(false);
		// The phantom carries the parsed coords + wanted (targeting implies
		// wanted), and no status.
		expect(phantom?.cell.coords).toEqual(["b", 2]);
		expect(phantom?.cell.wanted).toBe(true);
		expect(phantom?.cell.status).toBeNull();
	});

	it("sameSortKey distinguishes coord positions but not directions", () => {
		expect(sameSortKey({ kind: "coord", i: 0 }, { kind: "coord", i: 0 })).toBe(true);
		expect(sameSortKey({ kind: "coord", i: 0 }, { kind: "coord", i: 1 })).toBe(false);
		expect(sameSortKey({ kind: "status" }, { kind: "status" })).toBe(true);
		expect(sameSortKey({ kind: "status" }, { kind: "coord", i: 0 })).toBe(false);
	});
});

describe("cellBadges", () => {
	it("drops the healthy defaults (fresh currency, grounded)", () => {
		expect(cellBadges(cell({ coords: [], status: "complete", currency: "fresh" }))).toEqual([
			{ text: "complete", kind: "status" },
		]);
		expect(cellBadges(cell({ coords: [], status: "complete", grounding: "grounded" }))).toEqual([
			{ text: "complete", kind: "status" },
		]);
	});

	it("surfaces abnormal currency and grounding alongside the status", () => {
		expect(
			cellBadges(
				cell({ coords: [], status: "complete", currency: "stale", grounding: "orphaned" }),
			),
		).toEqual([
			{ text: "complete", kind: "status" },
			{ text: "stale", kind: "currency" },
			{ text: "orphaned", kind: "grounding" },
		]);
	});

	it("is empty for a plain data cell (no status, healthy)", () => {
		expect(cellBadges(cell({ coords: [] }))).toEqual([]);
	});
});

describe("cellDemand", () => {
	it("splits targeted by populated (run vs pending), else wanted / null", () => {
		expect(cellDemand(cell({ coords: [], wanted: true }), true, true)).toBe("targeted-run");
		expect(cellDemand(cell({ coords: [], wanted: true }), true, false)).toBe("targeted-pending");
		expect(cellDemand(cell({ coords: [], wanted: true }), false, true)).toBe("wanted");
		expect(cellDemand(cell({ coords: [], wanted: false }), false, true)).toBeNull();
		expect(cellDemand(cell({ coords: [], wanted: null }), false, true)).toBeNull();
		// Targeted implies wanted even if the flag hasn't caught up.
		expect(cellDemand(cell({ coords: [], wanted: null }), true, false)).toBe("targeted-pending");
	});
});
