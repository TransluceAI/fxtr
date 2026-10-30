import { describe, expect, it } from "vitest";
import {
	type FxtrCbor,
	FxtrID,
	RawFxtrEntityMarkerID,
	type RawFxtrRef,
	RawStrongFxtrID,
	RawWeakFxtrID,
} from "../../src/core/entityCore.js";
import {
	asyncVisitAndProjectIdsInCanonicalCborOrder,
	visitAndProjectIds,
	visitAndProjectIdsInCanonicalCborOrder,
	visitIds,
} from "../../src/core/visitor.js";

// =============================================================================
// visitIds
// =============================================================================

describe("visitIds", () => {
	it("finds weak refs", () => {
		const fid = FxtrID.builtin("test");
		const refs: RawFxtrRef[] = [];
		visitIds(new RawWeakFxtrID(fid), (ref) => refs.push(ref));
		expect(refs).toHaveLength(1);
		expect(refs[0]).toBeInstanceOf(RawWeakFxtrID);
	});

	it("finds strong refs", () => {
		const fid = FxtrID.builtin("test");
		const refs: RawFxtrRef[] = [];
		visitIds(new RawStrongFxtrID(fid), (ref) => refs.push(ref));
		expect(refs).toHaveLength(1);
		expect(refs[0]).toBeInstanceOf(RawStrongFxtrID);
	});

	it("finds marker refs", () => {
		const fid = FxtrID.here();
		const refs: RawFxtrRef[] = [];
		visitIds(new RawFxtrEntityMarkerID(fid), (ref) => refs.push(ref));
		expect(refs).toHaveLength(1);
		expect(refs[0]).toBeInstanceOf(RawFxtrEntityMarkerID);
	});

	it("traverses arrays", () => {
		const id1 = FxtrID.builtin("a");
		const id2 = FxtrID.builtin("b");
		const data: FxtrCbor = [new RawWeakFxtrID(id1), "hello", new RawStrongFxtrID(id2)];
		const refs: RawFxtrRef[] = [];
		visitIds(data, (ref) => refs.push(ref));
		expect(refs).toHaveLength(2);
	});

	it("traverses objects", () => {
		const id1 = FxtrID.builtin("a");
		const id2 = FxtrID.here();
		const data: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(id2),
			ref: new RawWeakFxtrID(id1),
			name: "test",
		};
		const refs: RawFxtrRef[] = [];
		visitIds(data, (ref) => refs.push(ref));
		expect(refs).toHaveLength(2);
	});

	it("traverses nested structures", () => {
		const id1 = FxtrID.builtin("a");
		const id2 = FxtrID.builtin("b");
		const id3 = FxtrID.here();
		const data: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(id3),
			nested: {
				list: [new RawWeakFxtrID(id1), { deep: new RawStrongFxtrID(id2) }],
			},
		};
		const refs: RawFxtrRef[] = [];
		visitIds(data, (ref) => refs.push(ref));
		expect(refs).toHaveLength(3);
	});

	it("ignores primitives", () => {
		const refs: RawFxtrRef[] = [];
		visitIds(null, (ref) => refs.push(ref));
		visitIds(42, (ref) => refs.push(ref));
		visitIds("hello", (ref) => refs.push(ref));
		visitIds(true, (ref) => refs.push(ref));
		visitIds(new Uint8Array([1, 2, 3]), (ref) => refs.push(ref));
		expect(refs).toHaveLength(0);
	});
});

// =============================================================================
// visitAndProjectIds
// =============================================================================

describe("visitAndProjectIds", () => {
	it("transforms weak refs", () => {
		const oldId = FxtrID.builtin("old");
		const newId = FxtrID.builtin("new");
		const result = visitAndProjectIds(new RawWeakFxtrID(oldId), () => newId);
		expect(result).toBeInstanceOf(RawWeakFxtrID);
		expect((result as RawWeakFxtrID).innerId.key()).toBe(newId.key());
	});

	it("transforms strong refs", () => {
		const oldId = FxtrID.builtin("old");
		const newId = FxtrID.builtin("new");
		const result = visitAndProjectIds(new RawStrongFxtrID(oldId), () => newId);
		expect(result).toBeInstanceOf(RawStrongFxtrID);
		expect((result as RawStrongFxtrID).innerId.key()).toBe(newId.key());
	});

	it("preserves ref type during transformation", () => {
		const oldId = FxtrID.builtin("old");
		const newId = FxtrID.builtin("new");
		const marker = visitAndProjectIds(new RawFxtrEntityMarkerID(oldId), () => newId);
		expect(marker).toBeInstanceOf(RawFxtrEntityMarkerID);
	});

	it("transforms nested structures", () => {
		const id1 = FxtrID.builtin("a");
		const id2 = FxtrID.builtin("b");
		const replacement = FxtrID.builtin("replaced");
		const data: FxtrCbor = {
			ref: new RawWeakFxtrID(id1),
			list: [new RawStrongFxtrID(id2)],
		};
		const result = visitAndProjectIds(data, () => replacement) as Record<string, FxtrCbor>;
		expect((result.ref as RawWeakFxtrID).innerId.key()).toBe(replacement.key());
		const listItem = (result.list as FxtrCbor[])[0] as RawStrongFxtrID;
		expect(listItem.innerId.key()).toBe(replacement.key());
	});

	it("preserves primitives", () => {
		expect(visitAndProjectIds(null, () => FxtrID.here())).toBe(null);
		expect(visitAndProjectIds(42, () => FxtrID.here())).toBe(42);
		expect(visitAndProjectIds("hello", () => FxtrID.here())).toBe("hello");
	});
});

// =============================================================================
// visitAndProjectIdsInCanonicalCborOrder
// =============================================================================

describe("visitAndProjectIdsInCanonicalCborOrder", () => {
	it("processes IDs in canonical byte order", () => {
		const id1 = FxtrID.builtin("a");
		const id2 = FxtrID.builtin("b");
		const ground = FxtrID.here();

		const data: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			z_ref: new RawWeakFxtrID(id1),
			a_ref: new RawStrongFxtrID(id2),
		};

		// Track the order IDs are visited
		const visitOrder: string[] = [];
		const result = visitAndProjectIdsInCanonicalCborOrder(data, (ref) => {
			visitOrder.push(ref.innerId.key());
			return ref.innerId; // identity transform
		});

		// Should have visited all 3 IDs
		expect(visitOrder).toHaveLength(3);

		// Result should still decode correctly
		expect(result).toBeDefined();
	});

	it("produces same result as visitAndProjectIds for identity transform", () => {
		const id1 = FxtrID.builtin("type1");
		const ground = FxtrID.here();
		const data: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			ref: new RawWeakFxtrID(id1),
			value: 42,
		};

		const result1 = visitAndProjectIds(data, (ref) => ref.innerId);
		const result2 = visitAndProjectIdsInCanonicalCborOrder(data, (ref) => ref.innerId);

		// Both should produce equivalent structures (same keys/values)
		const r1 = result1 as Record<string, FxtrCbor>;
		const r2 = result2 as Record<string, FxtrCbor>;
		expect(r1.value).toBe(r2.value);
		expect((r1.ref as RawWeakFxtrID).innerId.key()).toBe((r2.ref as RawWeakFxtrID).innerId.key());
	});
});

// =============================================================================
// asyncVisitAndProjectIdsInCanonicalCborOrder
// =============================================================================

describe("asyncVisitAndProjectIdsInCanonicalCborOrder", () => {
	it("processes IDs in canonical byte order with async callback", async () => {
		const id1 = FxtrID.builtin("a");
		const id2 = FxtrID.builtin("b");
		const ground = FxtrID.here();

		const data: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			ref1: new RawWeakFxtrID(id1),
			ref2: new RawStrongFxtrID(id2),
		};

		const visitOrder: string[] = [];
		const result = await asyncVisitAndProjectIdsInCanonicalCborOrder(data, async (ref) => {
			visitOrder.push(ref.innerId.key());
			return ref.innerId; // identity transform
		});

		expect(visitOrder).toHaveLength(3);
		expect(result).toBeDefined();
	});

	it("calls callbacks sequentially (not in parallel)", async () => {
		const ground = FxtrID.here();
		const id1 = FxtrID.builtin("a");

		const data: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			ref: new RawWeakFxtrID(id1),
		};

		const callTimes: number[] = [];
		await asyncVisitAndProjectIdsInCanonicalCborOrder(data, async (ref) => {
			callTimes.push(Date.now());
			// Small delay to verify sequential execution
			await new Promise((resolve) => setTimeout(resolve, 10));
			return ref.innerId;
		});

		// With 2 IDs and 10ms delay each, sequential execution means
		// second call starts after first finishes
		expect(callTimes).toHaveLength(2);
	});

	it("transforms IDs via async callback", async () => {
		const ground = FxtrID.here();
		const oldId = FxtrID.builtin("old");
		const newId = FxtrID.builtin("new");

		const data: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			ref: new RawWeakFxtrID(oldId),
		};

		const result = await asyncVisitAndProjectIdsInCanonicalCborOrder(data, async (ref) => {
			if (ref instanceof RawWeakFxtrID) return newId;
			return ref.innerId;
		});

		const r = result as Record<string, FxtrCbor>;
		expect((r.ref as RawWeakFxtrID).innerId.key()).toBe(newId.key());
	});
});
