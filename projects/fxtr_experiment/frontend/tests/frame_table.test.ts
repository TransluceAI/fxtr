/**
 * The frame-entity decode (`frame_table.tsx`): the pure half of the
 * frame renderer — tagged-schema/rows decoding (mirroring
 * `fxtr_experiment/frame_encoding.py`'s stored form), graceful nulls
 * on anything malformed (the component falls back to the ref link,
 * never throws into the panel), and cell display formatting.
 */

import { describe, expect, it } from "vitest";

import { decodeFrameValue, formatFrameCell } from "../src/frame_table.js";

const GOOD = {
	"$fxtr.type": { some: "ref stub" }, // extra keys tolerated
	schema: [
		["k", "str"],
		["n", "int", true],
	],
	rows: [
		["a", 1],
		["b", null],
	],
};

describe("decodeFrameValue", () => {
	it("decodes the stored form (nullable flag, extra keys)", () => {
		const model = decodeFrameValue(GOOD);
		expect(model).not.toBeNull();
		expect(model?.columns).toEqual([
			{ name: "k", kind: "str", nullable: false },
			{ name: "n", kind: "int", nullable: true },
		]);
		expect(model?.rows).toEqual([
			["a", 1],
			["b", null],
		]);
	});

	it("decodes an empty frame (zero rows is a valid value)", () => {
		const model = decodeFrameValue({ schema: [["k", "str"]], rows: [] });
		expect(model?.rows).toEqual([]);
		expect(model?.columns).toHaveLength(1);
	});

	it("returns null on malformed shapes, never throws", () => {
		expect(decodeFrameValue(null)).toBeNull();
		expect(decodeFrameValue("nope")).toBeNull();
		expect(decodeFrameValue([])).toBeNull();
		expect(decodeFrameValue({})).toBeNull();
		expect(decodeFrameValue({ schema: [], rows: [] })).toBeNull(); // zero columns
		expect(decodeFrameValue({ schema: [["k"]], rows: [] })).toBeNull(); // short column
		expect(decodeFrameValue({ schema: [["k", "str", false]], rows: [] })).toBeNull(); // bad flag
		expect(decodeFrameValue({ schema: [["k", "str"]], rows: [["a", 2]] })).toBeNull(); // arity
		expect(decodeFrameValue({ schema: [["k", "str"]], rows: ["a"] })).toBeNull(); // row shape
	});
});

describe("formatFrameCell", () => {
	it("formats scalars for display", () => {
		expect(formatFrameCell(null)).toBe("∅");
		expect(formatFrameCell("hi")).toBe("hi");
		expect(formatFrameCell(42)).toBe("42");
		expect(formatFrameCell(true)).toBe("true");
	});

	it("gists bytes (Uint8Array via the escape codec)", () => {
		expect(formatFrameCell(new Uint8Array([0x1a, 0x2b]))).toBe("0x1a2b (2B)");
		const long = new Uint8Array(12).fill(0xff);
		expect(formatFrameCell(long)).toBe(`0x${"ff".repeat(8)}… (12B)`);
	});
});
