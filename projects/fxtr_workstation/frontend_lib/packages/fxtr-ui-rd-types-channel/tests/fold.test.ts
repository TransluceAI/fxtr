/**
 * Unit tests for the pure folds the channel renderers derive from:
 * `foldChannelUpdates` (full stream → `ChannelViewState`) and
 * `latestChannelState` (state stream → latest event).
 */

import { describe, expect, it } from "vitest";
import {
	type ChannelStateEventWire,
	type ChannelUpdateWire,
	foldChannelUpdates,
	latestChannelState,
} from "../src/types.js";
import { TestEntityRefStub } from "./_mocks.js";

const resultRef = new TestEntityRefStub("e-result");

function fold(...events: ChannelUpdateWire[]) {
	return foldChannelUpdates(events);
}

describe("foldChannelUpdates", () => {
	it("no events → connecting", () => {
		expect(fold()).toEqual({ phase: "connecting" });
	});

	it("open snapshot then batched updates accumulate in order, threading status", () => {
		const view = fold(
			{ kind: "snapshot_open", status: "running", values: ["a", "b"] },
			{ kind: "update", events: ["c", resultRef], status: "running", replace_history: false },
			{ kind: "update", events: ["d"], status: "working", replace_history: false },
		);
		expect(view).toEqual({
			phase: "open",
			values: ["a", "b", "c", resultRef, "d"],
			status: "working",
		});
	});

	it("replace_history resets the accumulated log to the batch", () => {
		const view = fold(
			{ kind: "snapshot_open", status: "running", values: ["a", "b", "c"] },
			{ kind: "update", events: ["condensed"], status: "running", replace_history: true },
			{ kind: "update", events: ["d"], status: "running", replace_history: false },
		);
		expect(view).toEqual({ phase: "open", values: ["condensed", "d"], status: "running" });
	});

	it("a status-only update (empty events) still threads the new status", () => {
		const view = fold(
			{ kind: "snapshot_open", status: "running", values: ["a"] },
			{ kind: "update", events: [], status: "idle", replace_history: false },
		);
		expect(view).toEqual({ phase: "open", values: ["a"], status: "idle" });
	});

	it("complete snapshot alone (subscriber joined a finished channel)", () => {
		expect(fold({ kind: "snapshot_complete", status: "done", result: resultRef })).toEqual({
			phase: "complete",
			result: resultRef,
			status: "done",
		});
	});

	it("abort snapshot alone (subscriber joined an aborted channel)", () => {
		expect(fold({ kind: "snapshot_abort", status: "failed", reason: "scope closed" })).toEqual({
			phase: "aborted",
			reason: "scope closed",
			status: "failed",
		});
	});

	it("aborted_before_start folds to a statusless aborted view", () => {
		expect(fold({ kind: "aborted_before_start", reason: "never started" })).toEqual({
			phase: "aborted",
			reason: "never started",
			status: null,
		});
	});

	it("complete after updates drops the values, carries the final status", () => {
		const view = fold(
			{ kind: "snapshot_open", status: "running", values: [] },
			{ kind: "update", events: ["a"], status: "running", replace_history: false },
			{ kind: "complete", result: resultRef, status: "done" },
		);
		expect(view).toEqual({ phase: "complete", result: resultRef, status: "done" });
	});

	it("abort mid-stream carries the last status", () => {
		const view = fold(
			{ kind: "snapshot_open", status: "running", values: ["a"] },
			{ kind: "abort", status: "running", reason: "boom" },
		);
		expect(view).toEqual({ phase: "aborted", reason: "boom", status: "running" });
	});

	it("a mid-stream snapshot replaces the state (defensive)", () => {
		const view = fold(
			{ kind: "snapshot_open", status: "running", values: ["a"] },
			{ kind: "snapshot_open", status: "running", values: ["x"] },
			{ kind: "update", events: ["y"], status: "running", replace_history: false },
		);
		expect(view).toEqual({ phase: "open", values: ["x", "y"], status: "running" });
	});

	it("unknown kind folds to a diagnostic abort instead of throwing", () => {
		const bogus = { kind: "telemetry", value: 1 } as unknown as ChannelUpdateWire;
		const view = fold({ kind: "snapshot_open", status: "running", values: [] }, bogus);
		expect(view.phase).toBe("aborted");
		if (view.phase === "aborted") {
			expect(view.reason).toContain("telemetry");
			expect(view.status).toBe("running");
		}
	});

	it("delta before any snapshot folds to a statusless diagnostic abort", () => {
		const view = fold({ kind: "update", events: ["a"], status: "x", replace_history: false });
		expect(view.phase).toBe("aborted");
		if (view.phase === "aborted") {
			expect(view.status).toBeNull();
			expect(view.reason).toContain("update");
		}
	});

	it("events after a terminal state fold to a diagnostic abort", () => {
		const view = fold(
			{ kind: "snapshot_complete", status: "done", result: "done" },
			{ kind: "update", events: ["late"], status: "done", replace_history: false },
		);
		expect(view.phase).toBe("aborted");
	});

	it("nothing past an abort is folded", () => {
		const view = fold(
			{ kind: "snapshot_abort", status: "failed", reason: "first" },
			{ kind: "snapshot_open", status: "running", values: ["x"] },
		);
		expect(view).toEqual({ phase: "aborted", reason: "first", status: "failed" });
	});
});

describe("latestChannelState", () => {
	it("no events → null", () => {
		expect(latestChannelState([])).toBeNull();
	});

	it("returns the latest event (coalesce / replace-only)", () => {
		const events: ChannelStateEventWire[] = [
			{ kind: "open", status: "running" },
			{ kind: "open", status: "working" },
			{ kind: "complete", status: "done" },
		];
		expect(latestChannelState(events)).toEqual({ kind: "complete", status: "done" });
	});

	it("surfaces the statusless aborted_before_start variant", () => {
		expect(latestChannelState([{ kind: "aborted_before_start", reason: "never started" }])).toEqual(
			{ kind: "aborted_before_start", reason: "never started" },
		);
	});
});
