import { EntityRefStub } from "@fxtr-ui/core";
import {
	type FxtrCbor,
	FxtrID,
	RawFxtrEntityMarkerID,
	RawStrongFxtrID,
	RawWeakFxtrID,
} from "@fxtr/core";
import { describe, expect, it } from "vitest";

import { cborToResultValue } from "../src/entity_decode";

class TestEntityStub extends EntityRefStub {
	constructor(readonly shortId: string) {
		super();
	}
}

/**
 * An `entityRefForIdString` that interns one stub per id_string and
 * records the ids it was asked for. The shortId is just the id_string
 * here — the real connector maps through its shortId table.
 */
function makeInterner() {
	const stubs = new Map<string, TestEntityStub>();
	const seen: string[] = [];
	return {
		seen,
		entityRefForIdString: (idString: string) => {
			seen.push(idString);
			let s = stubs.get(idString);
			if (!s) {
				s = new TestEntityStub(idString);
				stubs.set(idString, s);
			}
			return s;
		},
	};
}

const ID_A = FxtrID.builtin("type-a");
const ID_B = FxtrID.builtin("type-b");

describe("cborToResultValue: primitives + bytes", () => {
	it("passes primitives through unchanged", () => {
		const i = makeInterner();
		for (const v of [null, true, false, 0, 42, -1.5, "", "hi"] as FxtrCbor[]) {
			expect(cborToResultValue(v, i)).toBe(v);
		}
		expect(i.seen).toEqual([]);
	});

	it("passes a Uint8Array through by identity (no base64 round trip)", () => {
		const i = makeInterner();
		const bytes = new Uint8Array([1, 2, 3]);
		expect(cborToResultValue(bytes, i)).toBe(bytes);
	});
});

describe("cborToResultValue: ref leaves", () => {
	it("maps all three Raw* tag variants to an interned entity stub", () => {
		for (const ref of [
			new RawWeakFxtrID(ID_A),
			new RawStrongFxtrID(ID_A),
			new RawFxtrEntityMarkerID(ID_A),
		]) {
			const i = makeInterner();
			const out = cborToResultValue(ref, i);
			expect(out).toBeInstanceOf(EntityRefStub);
			expect((out as TestEntityStub).shortId).toBe(ID_A.idString());
			expect(i.seen).toEqual([ID_A.idString()]);
		}
	});

	it("interns repeated ids to the same stub instance", () => {
		const i = makeInterner();
		const out = cborToResultValue(
			[new RawWeakFxtrID(ID_A), new RawStrongFxtrID(ID_A)],
			i,
		) as ResultArr;
		expect(out[0]).toBe(out[1]); // same id_string → same interned stub
	});
});

type ResultArr = readonly (EntityRefStub | unknown)[];

describe("cborToResultValue: containers", () => {
	it("recurses dicts and lists, preserving shape; refs become stubs", () => {
		const i = makeInterner();
		const tree: FxtrCbor = {
			name: "root",
			child: new RawStrongFxtrID(ID_A),
			items: [1, new RawWeakFxtrID(ID_B), "x"],
			nested: { ref: new RawWeakFxtrID(ID_A) },
		};
		const out = cborToResultValue(tree, i) as Record<string, unknown>;
		expect(out.name).toBe("root");
		expect((out.child as TestEntityStub).shortId).toBe(ID_A.idString());
		const items = out.items as ResultArr;
		expect(items[0]).toBe(1);
		expect((items[1] as TestEntityStub).shortId).toBe(ID_B.idString());
		expect(items[2]).toBe("x");
		// ID_A appears twice (child + nested.ref) → interned to one stub.
		expect(out.child as TestEntityStub).toBe(
			(out.nested as Record<string, unknown>).ref as TestEntityStub,
		);
	});

	it("keeps $fxtr.ctx / $fxtr.type entries as dict keys with stub values", () => {
		const i = makeInterner();
		const tree: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ID_A),
			"$fxtr.type": new RawWeakFxtrID(ID_B),
			payload: 7,
		};
		const out = cborToResultValue(tree, i) as Record<string, unknown>;
		expect((out["$fxtr.ctx"] as TestEntityStub).shortId).toBe(ID_A.idString());
		expect((out["$fxtr.type"] as TestEntityStub).shortId).toBe(ID_B.idString());
		expect(out.payload).toBe(7);
	});
});
