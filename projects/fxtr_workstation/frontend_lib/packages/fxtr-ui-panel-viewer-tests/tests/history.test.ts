/**
 * The breadcrumb/history pure transforms that drive the panel viewer's
 * navigation (`PanelViewerApi` calls these). No React, no dockview —
 * just `RefPanelChainState` in, `RefPanelChainState` out.
 *
 * Entries carry real `EntityRefStub` / `ServiceRefStub` instances
 * (minted via the workstation connector's concrete stub classes) because
 * `sameRef` disambiguates entity-vs-service via `instanceof`.
 */

import {
	WorkstationEntityRefStub,
	WorkstationServiceRefStub,
} from "@fxtr-ui/connector-workstation";
import {
	type Breadcrumb,
	type HistoryRoot,
	type PanelViewEntry,
	type RefPanelChainState,
	computeNextChainState,
	computeOpenNewFromChainEntry,
	computeOpenNewFromLink,
	computeSnapshotFromTrail,
} from "@fxtr-ui/panel-viewer";
import { atom } from "jotai";
import { describe, expect, it } from "vitest";

const BROKEN = atom<ReadonlySet<string>>(new Set<string>());

/** An entity entry (default display) with the given shortId. */
function ent(id: string): PanelViewEntry {
	return { stub: new WorkstationEntityRefStub(id), display: "default" };
}
/** A service entry (default display) with the given shortId. */
function svc(id: string): PanelViewEntry {
	return { stub: new WorkstationServiceRefStub(id, BROKEN), display: "default" };
}
/** A metadata-display entity entry. */
function meta(id: string): PanelViewEntry {
	return { stub: new WorkstationEntityRefStub(id), display: "metadata" };
}

const ROOTS: HistoryRoot = { kind: "roots" };

function state(
	chain: PanelViewEntry[],
	activeIndex: number,
	historyRoot: HistoryRoot = ROOTS,
): RefPanelChainState {
	return { chain, activeIndex, historyRoot, storages: chain.map(() => null) };
}

const ids = (s: RefPanelChainState): string[] => s.chain.map((e) => e.stub.shortId);

describe("computeNextChainState", () => {
	it("is a no-op when the target is already active", () => {
		const source = state([ent("a"), ent("b")], 0);
		const result = computeNextChainState(source, ent("a"));
		expect(result).toBe(source);
	});

	it("steps forward when the target is the next entry", () => {
		const result = computeNextChainState(state([ent("a"), ent("b")], 0), ent("b"));
		expect(ids(result)).toEqual(["a", "b"]);
		expect(result.activeIndex).toBe(1);
	});

	it("jumps back to an earlier entry without mutating the chain", () => {
		const result = computeNextChainState(state([ent("a"), ent("b"), ent("c")], 2), ent("a"));
		expect(ids(result)).toEqual(["a", "b", "c"]);
		expect(result.activeIndex).toBe(0);
	});

	it("appends a brand-new target at the end", () => {
		const result = computeNextChainState(state([ent("a"), ent("b")], 1), ent("c"));
		expect(ids(result)).toEqual(["a", "b", "c"]);
		expect(result.activeIndex).toBe(2);
		expect(result.storages.length).toBe(result.chain.length);
	});

	it("truncates the future before appending when active is mid-chain", () => {
		const result = computeNextChainState(state([ent("a"), ent("b"), ent("c")], 0), ent("d"));
		expect(ids(result)).toEqual(["a", "d"]);
		expect(result.activeIndex).toBe(1);
		expect(result.storages.length).toBe(2);
	});

	it("distinguishes entity vs service with the same shortId (kind matters)", () => {
		// active is entity "x"; target is service "x" — not the same ref,
		// so it's appended rather than treated as a no-op.
		const result = computeNextChainState(state([ent("x")], 0), svc("x"));
		expect(ids(result)).toEqual(["x", "x"]);
		expect(result.activeIndex).toBe(1);
	});

	it("distinguishes display modes for the same ref", () => {
		const result = computeNextChainState(state([ent("a")], 0), meta("a"));
		expect(result.chain).toHaveLength(2);
		expect(result.chain[0]?.display).toBe("default");
		expect(result.chain[1]?.display).toBe("metadata");
		expect(result.activeIndex).toBe(1);
	});
});

describe("computeOpenNewFromLink", () => {
	it("builds a fresh chain from the trail and captures the source prefix", () => {
		const source = state([ent("a"), ent("b"), ent("c")], 1);
		const trail: Breadcrumb[] = [
			{ from: ent("b"), to: ent("x"), via: "panel-ref" },
			{ from: ent("x"), to: ent("y"), via: "panel-ref" },
		];
		const result = computeOpenNewFromLink(source, trail);
		expect(ids(result)).toEqual(["x", "y"]);
		expect(result.activeIndex).toBe(1);
		expect(result.storages.length).toBe(2);
		expect(result.historyRoot.kind).toBe("branched");
		if (result.historyRoot.kind === "branched") {
			// prefix is source.chain[..activeIndex+1] = [a, b]
			expect(result.historyRoot.prefix.map((e) => e.stub.shortId)).toEqual(["a", "b"]);
			expect(result.historyRoot.parent).toBe(ROOTS);
		}
	});

	it("throws on an empty trail", () => {
		expect(() => computeOpenNewFromLink(state([ent("a")], 0), [])).toThrow();
	});
});

describe("computeOpenNewFromChainEntry", () => {
	it("rewinds to the given index, leaving the historyRoot untouched", () => {
		const root: HistoryRoot = { kind: "roots" };
		const source = state([ent("a"), ent("b"), ent("c")], 2, root);
		const result = computeOpenNewFromChainEntry(source, 1);
		expect(ids(result)).toEqual(["a", "b"]);
		expect(result.activeIndex).toBe(1);
		expect(result.historyRoot).toBe(root);
	});

	it("throws when the index is out of range", () => {
		const source = state([ent("a"), ent("b")], 1);
		expect(() => computeOpenNewFromChainEntry(source, 5)).toThrow();
		expect(() => computeOpenNewFromChainEntry(source, -1)).toThrow();
	});
});

describe("computeSnapshotFromTrail", () => {
	it("includes the first segment's `from` then every `to`", () => {
		const trail: Breadcrumb[] = [
			{ from: ent("a"), to: ent("b"), via: "roots-panel" },
			{ from: ent("b"), to: ent("c"), via: "panel-ref" },
		];
		const result = computeSnapshotFromTrail(trail);
		expect(ids(result)).toEqual(["a", "b", "c"]);
		expect(result.activeIndex).toBe(2);
		expect(result.historyRoot.kind).toBe("roots");
	});

	it("handles an entry-point tap with no `from`", () => {
		const result = computeSnapshotFromTrail([{ to: ent("a"), via: "roots-panel" }]);
		expect(ids(result)).toEqual(["a"]);
		expect(result.activeIndex).toBe(0);
	});

	it("passes a custom historyRoot through", () => {
		const hr: HistoryRoot = { kind: "branched", prefix: [ent("p")], parent: ROOTS };
		const result = computeSnapshotFromTrail([{ to: ent("a"), via: "panel-ref" }], hr);
		expect(result.historyRoot).toBe(hr);
	});

	it("throws on an empty trail", () => {
		expect(() => computeSnapshotFromTrail([])).toThrow();
	});
});
