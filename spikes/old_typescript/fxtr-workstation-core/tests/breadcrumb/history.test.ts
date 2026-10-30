import { describe, expect, it } from "vitest";
import {
	type FlatHistoryEntry,
	computeNextChainState,
	computeOpenNewFromChainEntry,
	computeOpenNewFromHistoryEntry,
	computeOpenNewFromLink,
	computeSnapshotFromTrail,
} from "../../src/breadcrumb/history.js";
import type { Breadcrumb, EntityPanelParams, HistoryRoot } from "../../src/breadcrumb/types.js";

const NAMESPACE: HistoryRoot = { kind: "namespace" };

function panel(chain: string[], activeIndex: number, historyRoot: HistoryRoot = NAMESPACE) {
	return { chain, activeIndex, historyRoot } satisfies EntityPanelParams;
}

describe("computeNextChainState", () => {
	it("no-op when target is already the active entry", () => {
		const before = panel(["A", "B", "C"], 1);
		expect(computeNextChainState(before, "B")).toBe(before);
	});

	it("advances the pointer when target matches the next entry (press-forward)", () => {
		const before = panel(["A", "B", "C", "D"], 1);
		const after = computeNextChainState(before, "C");
		expect(after.chain).toEqual(["A", "B", "C", "D"]);
		expect(after.activeIndex).toBe(2);
	});

	it("jumps to an earlier occurrence without trimming the future", () => {
		const before = panel(["A", "B", "C", "D"], 2);
		const after = computeNextChainState(before, "A");
		expect(after.chain).toEqual(["A", "B", "C", "D"]);
		expect(after.activeIndex).toBe(0);
	});

	it("truncates the future and appends when target is new", () => {
		const before = panel(["A", "B", "C", "D"], 1);
		const after = computeNextChainState(before, "X");
		expect(after.chain).toEqual(["A", "B", "X"]);
		expect(after.activeIndex).toBe(2);
	});

	it("does not mutate the source's historyRoot", () => {
		const root: HistoryRoot = { kind: "branched", prefix: ["A"], parent: NAMESPACE };
		const before = panel(["B"], 0, root);
		const after = computeNextChainState(before, "C");
		expect(after.historyRoot).toBe(root);
	});
});

describe("computeOpenNewFromLink", () => {
	it("uses the trail's to-sequence as the new chain", () => {
		const source = panel(["A", "B", "C"], 2);
		const trail: Breadcrumb[] = [
			{ fromEntityId: "C", toEntityId: "D", via: "panel-ref" },
			{ fromEntityId: "D", toEntityId: "E", via: "hover-ref" },
			{ fromEntityId: "E", toEntityId: "F", via: "hover-ref" },
		];
		const next = computeOpenNewFromLink(source, trail);
		expect(next.chain).toEqual(["D", "E", "F"]);
		expect(next.activeIndex).toBe(2);
	});

	it("captures source's chain-up-to-active as the historyRoot prefix", () => {
		const parentRoot: HistoryRoot = { kind: "branched", prefix: ["X"], parent: NAMESPACE };
		const source = panel(["A", "B", "C", "D"], 2, parentRoot);
		const trail: Breadcrumb[] = [{ fromEntityId: "C", toEntityId: "Z", via: "panel-ref" }];
		const next = computeOpenNewFromLink(source, trail);
		expect(next.historyRoot).toEqual({
			kind: "branched",
			prefix: ["A", "B", "C"],
			parent: parentRoot,
		});
	});

	it("throws on an empty trail", () => {
		const source = panel(["A"], 0);
		expect(() => computeOpenNewFromLink(source, [])).toThrow();
	});
});

describe("computeOpenNewFromChainEntry", () => {
	it("rewinds the chain to a prefix and keeps the historyRoot", () => {
		const root: HistoryRoot = { kind: "branched", prefix: ["X", "Y"], parent: NAMESPACE };
		const source = panel(["A", "B", "C", "D"], 3, root);
		const next = computeOpenNewFromChainEntry(source, 1);
		expect(next.chain).toEqual(["A", "B"]);
		expect(next.activeIndex).toBe(1);
		expect(next.historyRoot).toBe(root);
	});

	it("throws on an out-of-range index", () => {
		const source = panel(["A", "B"], 0);
		expect(() => computeOpenNewFromChainEntry(source, -1)).toThrow();
		expect(() => computeOpenNewFromChainEntry(source, 2)).toThrow();
	});
});

describe("computeOpenNewFromHistoryEntry", () => {
	it("restores preserving the unflattened historyRoot structure", () => {
		// [[[ns]>A>B]>[C>D]]>[E] — click D (inside the C-D segment)
		const deeperParent: HistoryRoot = {
			kind: "branched",
			prefix: ["A", "B"],
			parent: NAMESPACE,
		};
		const segmentParent = deeperParent;
		const entry: FlatHistoryEntry = {
			entityIdString: "D",
			segmentIndex: 1,
			positionInSegment: 1,
			segmentPrefix: ["C", "D"],
			segmentParent,
		};
		const next = computeOpenNewFromHistoryEntry(entry);
		expect(next.chain).toEqual(["C", "D"]);
		expect(next.activeIndex).toBe(1);
		// historyRoot should be the parent segment, i.e. [[ns]>A>B],
		// NOT a flattened [ns]>A>B>C>D.
		expect(next.historyRoot).toEqual(deeperParent);
	});

	it("truncates within the segment when clicking an earlier position", () => {
		const entry: FlatHistoryEntry = {
			entityIdString: "A",
			segmentIndex: 0,
			positionInSegment: 0,
			segmentPrefix: ["A", "B", "C"],
			segmentParent: NAMESPACE,
		};
		const next = computeOpenNewFromHistoryEntry(entry);
		expect(next.chain).toEqual(["A"]);
		expect(next.activeIndex).toBe(0);
		expect(next.historyRoot).toEqual(NAMESPACE);
	});
});

describe("computeSnapshotFromTrail", () => {
	it("treats a single namespace-panel click as a length-1 chain", () => {
		const trail: Breadcrumb[] = [{ toEntityId: "A", via: "namespace-panel" }];
		const next = computeSnapshotFromTrail(trail);
		expect(next.chain).toEqual(["A"]);
		expect(next.activeIndex).toBe(0);
		expect(next.historyRoot).toEqual(NAMESPACE);
	});

	it("includes the first `from` entity in the chain (non-panel origins have no historyRoot prefix to absorb it)", () => {
		// Pin row hovered → e1's hover card → e2's hover card → click e3.
		const trail: Breadcrumb[] = [
			{ fromEntityId: "pinEntity", toEntityId: "e2", via: "hover-ref" },
			{ fromEntityId: "e2", toEntityId: "e3", via: "hover-ref" },
		];
		const next = computeSnapshotFromTrail(trail);
		expect(next.chain).toEqual(["pinEntity", "e2", "e3"]);
		expect(next.activeIndex).toBe(2);
	});

	it("throws on an empty trail", () => {
		expect(() => computeSnapshotFromTrail([])).toThrow();
	});
});
