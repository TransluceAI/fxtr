/**
 * Pure-function tests for the per-event-type summary helpers used
 * by the ``<CollapsibleCard>`` row's inline collapsed summary.
 */

import { describe, expect, it } from "vitest";

import {
	type CallbackNotificationValue,
	type InvokeErrorValue,
	type InvokeResultValue,
	OP_KIND_FORGET,
	OP_KIND_PRESENT_TO_USER,
	OP_KIND_SCHEDULE_INVOKE,
	type ScriptExecutionValue,
	type ScriptOpRecord,
} from "../src/data_shapes.js";
import {
	summarizeCallbackNotif,
	summarizeInvokeError,
	summarizeInvokeResult,
	summarizeScript,
	summarizeValue,
	truncate,
} from "../src/entries/summarize.js";
import { TestEntityRefStub, TestServiceRefStub } from "./_mock_session.js";

describe("truncate", () => {
	it("returns empty string for empty input", () => {
		expect(truncate("")).toBe("");
	});
	it("collapses whitespace and trims", () => {
		expect(truncate("  hello\n\tworld  ")).toBe("hello world");
	});
	it("truncates at n with ellipsis", () => {
		expect(truncate("abcdefghij", 5)).toBe("abcde…");
	});
	it("returns flat input unchanged when shorter than n", () => {
		expect(truncate("short")).toBe("short");
	});
});

describe("summarizeValue", () => {
	it("handles primitives", () => {
		expect(summarizeValue(null)).toBe("null");
		expect(summarizeValue(42)).toBe("42");
		expect(summarizeValue(true)).toBe("true");
		expect(summarizeValue("hi")).toBe('"hi"');
	});
	it("truncates long strings", () => {
		const s = "x".repeat(80);
		expect(summarizeValue(s)).toBe(`"${"x".repeat(40)}…"`);
	});
	it("summarizes entity / service refs", () => {
		const e = new TestEntityRefStub("e-abc");
		const s = new TestServiceRefStub("s-123");
		expect(summarizeValue(e)).toBe("<entity e-abc>");
		expect(summarizeValue(s)).toBe("<service s-123>");
	});
	it("describes lists by cardinality", () => {
		expect(summarizeValue([1, 2, 3])).toBe("list[3]");
		expect(summarizeValue([])).toBe("list[0]");
	});
	it("describes dicts by key count", () => {
		expect(summarizeValue({ a: 1, b: 2 })).toBe("{2 keys}");
	});
	it("describes bytes by length", () => {
		expect(summarizeValue(new Uint8Array([1, 2, 3, 4]))).toBe("bytes(4)");
	});
});

describe("summarizeScript", () => {
	function makeScript(over: Partial<ScriptExecutionValue> = {}): ScriptExecutionValue {
		return {
			script_src: "",
			preamble: new TestEntityRefStub("e-pre"),
			stdout: "",
			stderr: "",
			error: null,
			rejected: null,
			ops: [],
			committed_seq: null,
			...over,
		};
	}
	it("ran (N ops) on success", () => {
		const ops: ScriptOpRecord[] = [
			{ kind: OP_KIND_FORGET, slot_key: "a" },
			{ kind: OP_KIND_FORGET, slot_key: "b" },
		];
		expect(summarizeScript(makeScript({ ops }))).toBe("ran (2 ops)");
		expect(summarizeScript(makeScript({ ops: [] }))).toBe("ran (0 ops)");
		expect(summarizeScript(makeScript({ ops: [{ kind: OP_KIND_FORGET, slot_key: "x" }] }))).toBe(
			"ran (1 op)",
		);
	});
	it("excludes present_to_user ops from the count", () => {
		const ops: ScriptOpRecord[] = [
			{ kind: OP_KIND_FORGET, slot_key: "a" },
			{ kind: OP_KIND_PRESENT_TO_USER, payload: "shown" },
			{ kind: OP_KIND_PRESENT_TO_USER, payload: "shown again" },
		];
		expect(summarizeScript(makeScript({ ops }))).toBe("ran (1 op)");
	});
	it("surfaces errored / rejected reasons", () => {
		expect(summarizeScript(makeScript({ error: "boom!" }))).toBe("errored: boom!");
		expect(summarizeScript(makeScript({ rejected: "nope" }))).toBe("rejected: nope");
	});
	it("error wins over rejected when both present", () => {
		expect(summarizeScript(makeScript({ error: "e", rejected: "r" }))).toBe("errored: e");
	});
});

describe("summarizeInvokeResult / summarizeInvokeError", () => {
	const baseResult: InvokeResultValue = {
		slot_key: "load_42",
		slot_n: 42,
		seq: 99,
		payload: "hi",
	};
	it("formats slot identity + payload repr", () => {
		expect(summarizeInvokeResult(baseResult)).toBe('load_42:42 → "hi"');
	});
	it("formats slot identity + error first line", () => {
		const v: InvokeErrorValue = {
			slot_key: "k",
			slot_n: 7,
			seq: 1,
			message: "service exploded",
		};
		expect(summarizeInvokeError(v)).toBe("k:7: service exploded");
	});
});

describe("summarizeCallbackNotif", () => {
	function makeCb(over: Partial<CallbackNotificationValue> = {}): CallbackNotificationValue {
		return {
			slot_key: "workstation_inbox",
			slot_n: 0,
			seq: 1,
			payload: "",
			...over,
		};
	}
	it("workstation_inbox: bare string falls through to truncated text", () => {
		expect(summarizeCallbackNotif(makeCb({ payload: "hello world" }))).toBe("hello world");
	});
	it("workstation_inbox: structured {message, attached} surfaces message", () => {
		expect(
			summarizeCallbackNotif(
				makeCb({
					payload: { message: "tag along", attached: [] },
				}),
			),
		).toBe("tag along");
	});
	it("workstation_inbox: object without message falls through to summarizeValue", () => {
		expect(summarizeCallbackNotif(makeCb({ payload: { foo: 1 } }))).toBe("{1 keys}");
	});
	it("non-workstation-inbox slot uses slot identity format", () => {
		expect(
			summarizeCallbackNotif(makeCb({ slot_key: "other", slot_n: 5, payload: [1, 2, 3] })),
		).toBe("other:5: list[3]");
	});
	it("present_to_user constant matches Python", () => {
		// Mostly a guard that SCHEDULE_INVOKE / PRESENT_TO_USER haven't
		// drifted; ensures the summary helpers' constants stay aligned
		// with the data_shapes module.
		expect(OP_KIND_PRESENT_TO_USER).toBe("present_to_user");
		expect(OP_KIND_SCHEDULE_INVOKE).toBe("schedule_invoke");
	});
});
