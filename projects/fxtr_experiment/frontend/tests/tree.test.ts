/** Unit tests for the tree derivation (pure — no backend). */

import { describe, expect, it } from "vitest";

import { buildGridTree, parseKeyPath } from "../src/derive/tree.js";
import type { GridSummary } from "../src/wire/types.js";

function grid(
	path: string,
	kind: GridSummary["kind"],
	counts: Partial<GridSummary> = {},
): GridSummary {
	return {
		path,
		kind,
		axes: [],
		// biome-ignore lint/suspicious/noExplicitAny: tests never touch the stub
		config_id: null as any,
		grounding: "grounded",
		grounding_reason: null,
		sourcing: null,
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
		...counts,
	};
}

describe("buildGridTree", () => {
	it("folds paths into a sorted tree with byPath lookups", () => {
		const tree = buildGridTree([
			grid('["box"]', "subtree"),
			grid('["box","inner"]', "data", { populated: 2 }),
			grid('["a"]', "data", { populated: 1 }),
		]);
		expect(tree.roots.map((r) => r.key)).toEqual(["a", "box"]);
		const box = tree.byPath.get('["box"]');
		expect(box?.children.map((c) => c.key)).toEqual(["inner"]);
		expect(tree.byPath.get('["box","inner"]')?.summary?.populated).toBe(2);
	});

	it("aggregates descendant rollups into subtree nodes", () => {
		const tree = buildGridTree([
			grid('["box"]', "subtree"),
			grid('["box","t"]', "task", { populated: 3, n_errored: 1, targets_attached: 2 }),
			grid('["box","d"]', "data", { populated: 4 }),
		]);
		const box = tree.byPath.get('["box"]');
		expect(box?.agg).toMatchObject({ populated: 7, n_errored: 1, targets_attached: 2 });
		// Own rows stay intact on the leaves.
		expect(tree.byPath.get('["box","t"]')?.agg.populated).toBe(3);
	});

	it("marks grids under a task grid as that task's internals", () => {
		const tree = buildGridTree([
			grid('["t"]', "task", { populated: 1 }),
			grid('["t","steps"]', "checkpoint", { populated: 5 }),
			grid('["d"]', "data"),
		]);
		expect(tree.byPath.get('["t"]')?.internalsOf).toBeNull();
		expect(tree.byPath.get('["t","steps"]')?.internalsOf).toBe('["t"]');
		expect(tree.byPath.get('["d"]')?.internalsOf).toBeNull();
		// Internals still aggregate into their owner.
		expect(tree.byPath.get('["t"]')?.agg.populated).toBe(6);
	});

	it("synthesizes implicit intermediate nodes (no summary row)", () => {
		const tree = buildGridTree([grid('["a","b","c"]', "data", { populated: 1 })]);
		const a = tree.byPath.get('["a"]');
		expect(a?.summary).toBeNull();
		expect(a?.agg.populated).toBe(1);
		expect(a?.children[0]?.children[0]?.summary?.kind).toBe("data");
	});

	it("drops malformed paths instead of crashing", () => {
		const tree = buildGridTree([grid("not json", "data"), grid('["ok"]', "data")]);
		expect(tree.roots.map((r) => r.key)).toEqual(["ok"]);
		expect(parseKeyPath("not json")).toBeNull();
		expect(parseKeyPath('{"a":1}')).toBeNull();
	});
});
