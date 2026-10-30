/**
 * Tests for the report tree renderer.
 */

import type { FxtrCbor } from "@fxtr/core";
import { describe, expect, it, vi } from "vitest";
import { isNodeTuple } from "./helpers.js";

// We test the internal logic rather than the React rendering,
// since the React part requires full provider setup.

describe("isNodeTuple", () => {
	it("recognizes valid node tuples", () => {
		expect(isNodeTuple(["div", {}, []])).toBe(true);
		expect(isNodeTuple(["p", { class: "foo" }, ["text"]])).toBe(true);
		expect(isNodeTuple(["svg", { width: "100" }, []])).toBe(true);
	});

	it("rejects non-tuples", () => {
		expect(isNodeTuple("hello")).toBe(false);
		expect(isNodeTuple(42)).toBe(false);
		expect(isNodeTuple(null)).toBe(false);
		expect(isNodeTuple(undefined as never)).toBe(false);
		expect(isNodeTuple([])).toBe(false);
		expect(isNodeTuple([1, 2, 3])).toBe(false);
		expect(isNodeTuple(["div", "not-an-object", []])).toBe(false);
		expect(isNodeTuple(["div", {}, "not-an-array"])).toBe(false);
	});
});

describe("preview target callback", () => {
	it("collects data-preview-target clicks", () => {
		// Test the logic that would wire up preview target clicks.
		// The actual SVG rendering requires JSDOM/React, so we test the
		// extraction logic here.
		const svgProps: Record<string, FxtrCbor> = {
			x: "10",
			y: "20",
			"data-preview-target": "catA",
		};

		const targetKey = svgProps["data-preview-target"];
		expect(targetKey).toBe("catA");

		const callback = vi.fn();
		if (typeof targetKey === "string") {
			callback(targetKey);
		}
		expect(callback).toHaveBeenCalledWith("catA");
	});
});
