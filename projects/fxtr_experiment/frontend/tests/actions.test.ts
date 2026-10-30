/**
 * The mutation-affordance doctrine (`src/derive/actions.ts`): which
 * buttons a cell offers per kind × mode × status × intent state —
 * never auto-target silently, run only on targeted cells, non-fresh
 * demands routed through "Clear + rerun", nothing on read grants or
 * running cells.
 */

import { describe, expect, it } from "vitest";

import { type CellActionCtx, deriveCellActions } from "../src/derive/actions.js";

function ctx(partial: Partial<CellActionCtx>): CellActionCtx {
	return {
		kind: "task",
		mode: "write",
		internals: false,
		status: null,
		currency: null,
		targeted: false,
		gridSourced: true,
		hasCellSourcing: false,
		populated: false,
		...partial,
	};
}

const ids = (c: CellActionCtx) => deriveCellActions(c).map((a) => a.id);

describe("deriveCellActions", () => {
	it("offers nothing on internals grids or running cells", () => {
		// The gate is internals-ness, not grant mode: task grants are
		// read-shaped by design, so a top-level task grid's read grant
		// must NOT hide its actions.
		expect(ids(ctx({ internals: true, targeted: true, populated: true }))).toEqual([]);
		expect(ids(ctx({ mode: "read", targeted: true }))).toEqual(["run", "untarget"]);
		expect(ids(ctx({ status: "running", targeted: true, populated: true }))).toEqual([]);
		// Data/handle mutations do additionally require a write grant.
		expect(ids(ctx({ kind: "data", mode: "read", populated: true }))).toEqual([]);
		expect(ids(ctx({ kind: "data", internals: true, populated: true }))).toEqual([]);
	});

	it("untargeted task cells: target+run / target, never a bare run", () => {
		expect(ids(ctx({}))).toEqual(["target_run", "target"]);
		expect(ids(ctx({ status: "errored", populated: true }))).toEqual([
			"target_run",
			"target",
			"clear",
		]);
	});

	it("targeting into an unsourced grid needs the adopt prompt", () => {
		const actions = deriveCellActions(ctx({ gridSourced: false }));
		expect(actions.map((a) => [a.id, a.needsAdopt ?? false])).toEqual([
			["target_run", true],
			["target", true],
		]);
		// A sourced grid prompts nothing.
		expect(deriveCellActions(ctx({})).every((a) => !a.needsAdopt)).toBe(true);
	});

	it("targeted task cells: run when serviceable, retry on errored, clear+rerun on non-fresh", () => {
		expect(ids(ctx({ targeted: true }))).toEqual(["run", "untarget"]);
		expect(
			ids(ctx({ targeted: true, status: "complete", currency: "fresh", populated: true })),
		).toEqual(["run", "clear", "untarget"]);
		expect(
			ids(ctx({ targeted: true, status: "errored", currency: "fresh", populated: true })),
		).toEqual(["retry", "clear_rerun", "clear", "untarget"]);
		expect(
			ids(ctx({ targeted: true, status: "complete", currency: "stale", populated: true })),
		).toEqual(["clear_rerun", "clear", "untarget"]);
		expect(
			ids(ctx({ targeted: true, status: "complete", currency: "unsettled", populated: true })),
		).toEqual(["clear_rerun", "clear", "untarget"]);
	});

	it("data/handle cells: target toggle + sourcing record + delete (confirmed)", () => {
		expect(ids(ctx({ kind: "data", populated: true }))).toEqual([
			"target",
			"source_cell",
			"delete_cell",
		]);
		expect(
			ids(ctx({ kind: "handle", targeted: true, populated: true, hasCellSourcing: true })),
		).toEqual(["untarget", "revoke_cell_sourcing", "delete_cell"]);
		// An empty cell has nothing to delete or source.
		expect(ids(ctx({ kind: "data" }))).toEqual(["target"]);
		const del = deriveCellActions(ctx({ kind: "data", populated: true })).find(
			(a) => a.id === "delete_cell",
		);
		expect(del?.confirm).toBe(true);
	});

	it("namespace kinds offer nothing", () => {
		expect(ids(ctx({ kind: "subtree", populated: true }))).toEqual([]);
		expect(ids(ctx({ kind: "checkpoint", populated: true }))).toEqual([]);
	});
});
