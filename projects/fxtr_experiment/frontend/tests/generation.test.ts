/**
 * The drawer's generation merges (`derive/generation.ts`): the "step
 * checkpoints" merge (completed checkpoints + running/errored steps
 * from the log) and the observations+requests merge (pending rows).
 */

import { describe, expect, it } from "vitest";

import type { CellLogMirror } from "../src/derive/folds.js";
import { deriveObservationRows, deriveStepRows } from "../src/derive/generation.js";
import type { CheckpointInfo, LogEntry, ObservationInfo, RequestInfo } from "../src/wire/types.js";

// A non-null stand-in for a ref-stub leaf (the merges never dereference
// it; the tests only check presence/absence).
// biome-ignore lint/suspicious/noExplicitAny: opaque stub leaf
const STUB = { shortId: "x" } as any;

function checkpoint(coords: string): CheckpointInfo {
	return {
		grid_path: '["t","steps"]',
		coords,
		step_type_id: STUB,
		args_id: STUB,
		value_id: STUB,
	};
}

function logOf(
	entries: readonly { kind: LogEntry["entry_kind"]; coords: string; msg?: string }[],
): CellLogMirror {
	return {
		generation: "g1",
		entries: entries.map((e) => ({
			ts: "t",
			entry_kind: e.kind,
			step: { grid_path: '["t","steps"]', coords: e.coords },
			message: e.msg ?? "",
		})),
	};
}

describe("deriveStepRows", () => {
	it("marks checkpointed steps complete and log-only steps running", () => {
		const rows = deriveStepRows(
			[checkpoint("[0]")],
			logOf([
				{ kind: "step_hit", coords: "[0]" },
				{ kind: "step_started", coords: "[1]" },
			]),
		);
		const byCoords = new Map(rows.map((r) => [r.coords, r]));
		expect(byCoords.get("[0]")?.status).toBe("complete");
		expect(byCoords.get("[0]")?.checkpoint).not.toBeNull();
		expect(byCoords.get("[1]")?.status).toBe("running");
		expect(byCoords.get("[1]")?.checkpoint).toBeNull();
	});

	it("marks a step_errored step errored, with its message", () => {
		const rows = deriveStepRows(
			[],
			logOf([
				{ kind: "step_started", coords: "[0]" },
				{ kind: "step_errored", coords: "[0]", msg: "boom" },
			]),
		);
		expect(rows).toHaveLength(1);
		expect(rows[0]?.status).toBe("errored");
		expect(rows[0]?.error).toBe("boom");
	});

	it("lets a checkpoint win over a stale log entry for the same step", () => {
		const rows = deriveStepRows(
			[checkpoint("[0]")],
			logOf([{ kind: "step_started", coords: "[0]" }]),
		);
		expect(rows).toHaveLength(1);
		expect(rows[0]?.status).toBe("complete");
	});

	it("handles a null log (no run yet) — just the checkpoints", () => {
		const rows = deriveStepRows([checkpoint("[0]")], null);
		expect(rows.map((r) => r.status)).toEqual(["complete"]);
	});
});

describe("deriveObservationRows", () => {
	const obs: ObservationInfo = {
		subject_path: '["d"]',
		subject_coords: '["a"]',
		subject_output: "",
		kind: "cell_value",
		fingerprint: STUB,
		current: "changed",
	};
	const req: RequestInfo = { grid_path: '["u"]', coords: "[]", kind: "task_output" };

	it("carries observations' current-match and appends requests as pending", () => {
		const rows = deriveObservationRows([obs], [req]);
		expect(rows).toHaveLength(2);
		expect(rows[0]).toMatchObject({ grid_path: '["d"]', status: "changed", kind: "cell_value" });
		expect(rows[0]?.fingerprint).not.toBeNull();
		expect(rows[1]).toMatchObject({ grid_path: '["u"]', status: "pending", fingerprint: null });
	});
});
