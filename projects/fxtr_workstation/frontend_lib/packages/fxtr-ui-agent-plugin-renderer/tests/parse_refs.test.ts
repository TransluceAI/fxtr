/**
 * Pure-function tests for the ``@[shortid]`` tokenizer shared
 * between the send-box editor (decoration scanner + send-time
 * attached-list builder) and the receive-side
 * ``StructuredMessageBody`` renderer.
 */

import { describe, expect, it } from "vitest";

import { collectShortIds, tokenizeMessage } from "../src/send_box/parse_refs.js";

describe("tokenizeMessage", () => {
	it("returns a single text token for plain text", () => {
		expect(tokenizeMessage("hello")).toEqual([{ kind: "text", value: "hello" }]);
	});
	it("returns an empty array for an empty doc", () => {
		expect(tokenizeMessage("")).toEqual([]);
	});
	it("emits a single ref token for a bare @[shortid]", () => {
		expect(tokenizeMessage("@[e-abc]")).toEqual([
			{ kind: "ref", shortId: "e-abc", start: 0, end: 8 },
		]);
	});
	it("interleaves text and refs", () => {
		expect(tokenizeMessage("hi @[e-1] there")).toEqual([
			{ kind: "text", value: "hi " },
			{ kind: "ref", shortId: "e-1", start: 3, end: 9 },
			{ kind: "text", value: " there" },
		]);
	});
	it("emits adjacent refs without spurious text tokens between", () => {
		expect(tokenizeMessage("@[a]@[b]")).toEqual([
			{ kind: "ref", shortId: "a", start: 0, end: 4 },
			{ kind: "ref", shortId: "b", start: 4, end: 8 },
		]);
	});
	it("rejects empty / whitespace-only / disallowed-char refs", () => {
		// The regex's character class is [A-Za-z0-9._:-]+; whitespace
		// and brackets are rejected, so these inputs should pass
		// through as plain text.
		expect(tokenizeMessage("@[]")).toEqual([{ kind: "text", value: "@[]" }]);
		expect(tokenizeMessage("@[ ]")).toEqual([{ kind: "text", value: "@[ ]" }]);
		expect(tokenizeMessage("@[a b]")).toEqual([{ kind: "text", value: "@[a b]" }]);
	});
	it("accepts shortIds with dot / underscore / colon / dash", () => {
		expect(tokenizeMessage("@[a.b_c:d-e]")).toEqual([
			{ kind: "ref", shortId: "a.b_c:d-e", start: 0, end: 12 },
		]);
	});
	it("doesn't over-match a trailing @[", () => {
		expect(tokenizeMessage("@[ok] extra @[")).toEqual([
			{ kind: "ref", shortId: "ok", start: 0, end: 5 },
			{ kind: "text", value: " extra @[" },
		]);
	});
	it("preserves duplicate shortIds in document order", () => {
		const tokens = tokenizeMessage("@[a] and @[a]");
		expect(tokens.filter((t) => t.kind === "ref")).toEqual([
			{ kind: "ref", shortId: "a", start: 0, end: 4 },
			{ kind: "ref", shortId: "a", start: 9, end: 13 },
		]);
	});
});

describe("collectShortIds", () => {
	it("returns shortIds in document order, duplicates preserved", () => {
		expect(collectShortIds("@[x] hi @[y] then @[x]")).toEqual(["x", "y", "x"]);
	});
	it("returns empty array for plain text", () => {
		expect(collectShortIds("just words")).toEqual([]);
	});
});
