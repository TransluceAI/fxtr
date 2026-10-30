import { describe, expect, it } from "vitest";
import { flattenHistoryRoot } from "../../src/breadcrumb/flatten.js";
import {
	type FlatHistoryEntry,
	computeOpenNewFromHistoryEntry,
} from "../../src/breadcrumb/history.js";
import type { HistoryRoot } from "../../src/breadcrumb/types.js";

const NAMESPACE: HistoryRoot = { kind: "namespace" };

describe("flattenHistoryRoot", () => {
	it("returns an empty list for a namespace-only root", () => {
		expect(flattenHistoryRoot(NAMESPACE)).toEqual([]);
	});

	it("orders entries deepest-first and preserves segment boundaries", () => {
		// ((ns)>A>B)>C>D
		const root: HistoryRoot = {
			kind: "branched",
			prefix: ["C", "D"],
			parent: {
				kind: "branched",
				prefix: ["A", "B"],
				parent: NAMESPACE,
			},
		};
		const flat = flattenHistoryRoot(root);
		expect(flat.map((e) => e.entityIdString)).toEqual(["A", "B", "C", "D"]);
		expect(flat.map((e) => e.segmentIndex)).toEqual([0, 0, 1, 1]);
		expect(flat.map((e) => e.positionInSegment)).toEqual([0, 1, 0, 1]);
	});

	it("restoring from a deep entry preserves the unflattened structure", () => {
		// ((ns)>A>B)>C>D  →  click D
		const deepParent: HistoryRoot = {
			kind: "branched",
			prefix: ["A", "B"],
			parent: NAMESPACE,
		};
		const root: HistoryRoot = {
			kind: "branched",
			prefix: ["C", "D"],
			parent: deepParent,
		};
		const flat = flattenHistoryRoot(root);
		const entryForD = flat.find((e) => e.entityIdString === "D") as FlatHistoryEntry;
		expect(entryForD).toBeDefined();
		const restored = computeOpenNewFromHistoryEntry(entryForD);
		expect(restored.chain).toEqual(["C", "D"]);
		expect(restored.activeIndex).toBe(1);
		// historyRoot must be the parent segment, NOT a flattened A>B>C>D
		expect(restored.historyRoot).toEqual(deepParent);
	});

	it("restoring from a shallow entry drops the whole deeper segment", () => {
		const deepParent: HistoryRoot = {
			kind: "branched",
			prefix: ["A", "B"],
			parent: NAMESPACE,
		};
		const root: HistoryRoot = {
			kind: "branched",
			prefix: ["C", "D"],
			parent: deepParent,
		};
		const flat = flattenHistoryRoot(root);
		const entryForA = flat.find((e) => e.entityIdString === "A") as FlatHistoryEntry;
		const restored = computeOpenNewFromHistoryEntry(entryForA);
		expect(restored.chain).toEqual(["A"]);
		expect(restored.activeIndex).toBe(0);
		expect(restored.historyRoot).toEqual(NAMESPACE);
	});
});
