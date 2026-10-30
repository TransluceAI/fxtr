/**
 * The selection helpers (`src/wire/helpers.ts`): the drawer's
 * fully-bound `ViewSel` construction — `boundByPosition` over a cell's
 * coords array through `viewSel`'s sorted-pairs contract.
 */

import { describe, expect, it } from "vitest";

import { boundByPosition, viewSel } from "../src/wire/helpers.js";

describe("boundByPosition", () => {
	it("binds every coord at its position, pairs sorted", () => {
		const sel = viewSel("tok", boundByPosition(["red", 3, "x"]));
		expect(sel).toEqual({
			token: "tok",
			bound: [
				[0, "red"],
				[1, 3],
				[2, "x"],
			],
			output: null,
		});
	});

	it("an empty coords array is the unsliced selection", () => {
		expect(viewSel("tok", boundByPosition([]))).toEqual({ token: "tok", bound: [], output: null });
	});
});
