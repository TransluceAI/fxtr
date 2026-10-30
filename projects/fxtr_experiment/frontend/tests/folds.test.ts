/**
 * The per-family fold functions (`derive/folds.ts`): snapshot-as-first-
 * batch, keyed upserts/removes, replace sub-topics, recomputing/settle,
 * `subject_gone` reset semantics, log reset/append (+ the client-side
 * cap), and the ignore-unknown posture at the payload boundary.
 */

import { describe, expect, it } from "vitest";

import {
	CELLS_MIRROR_EMPTY,
	CELL_MIRROR_EMPTY,
	MAX_LOG_ENTRIES,
	WORKFLOW_MIRROR_EMPTY,
	foldCell,
	foldCells,
	foldWorkflow,
} from "../src/derive/folds.js";
import { PANEL_INITIAL, type PanelState, isPanelState } from "../src/panel_state.js";
import type {
	CellInfo,
	CellReport,
	GridMeta,
	GridSummary,
	LintReport,
	LogEntry,
} from "../src/wire/types.js";

function summaryRow(path: string, populated = 0): GridSummary {
	return {
		path,
		kind: "data",
		axes: [],
		// biome-ignore lint/suspicious/noExplicitAny: fold tests never dereference the stub
		config_id: null as any,
		grounding: null,
		grounding_reason: null,
		sourcing: null,
		populated,
		n_pending: 0,
		n_running: 0,
		n_complete: 0,
		n_errored: 0,
		n_interrupted: 0,
		n_stale: 0,
		n_unsettled: 0,
		targets_attached: 0,
		targets_dangling: 0,
	};
}

const EMPTY_LINT: LintReport = {
	dangling_sourcing: [],
	dangling_targets: [],
	target_findings: [],
	ungrounded: [],
};

function cellRow(coords: readonly (string | number)[]): CellInfo {
	return { coords, status: null, currency: null, reason: null, grounding: null, wanted: null };
}

const META: GridMeta = {
	kind: "meta",
	path: '["g"]',
	// biome-ignore lint/suspicious/noExplicitAny: fold tests never dereference the stub
	config_id: null as any,
	grounding: "grounded",
	grounding_reason: null,
	sourcing: null,
	grant: {
		token: "tok",
		kind: "data",
		axes: [],
		mode: "write",
		schema: null,
		outputs: null,
		value: null,
	},
	parent_token: "tok-parent",
};

const EMPTY_REPORT: CellReport = {
	status: null,
	currency: null,
	reason: null,
	results: null,
	value: null,
	grounding: null,
	grounding_reason: null,
	wanted: null,
	wanted_reason: null,
	bound: null,
};

function logEntry(message: string): LogEntry {
	return { ts: "t", entry_kind: "log", step: null, message };
}

describe("foldWorkflow", () => {
	it("folds the snapshot batch from the empty mirror", () => {
		const m = foldWorkflow(WORKFLOW_MIRROR_EMPTY, {
			kind: "batch",
			patches: [
				{ kind: "summary_upsert", rows: [summaryRow('["a"]'), summaryRow('["b"]')] },
				{ kind: "lint", report: EMPTY_LINT },
				{ kind: "targets", targets: [] },
			],
		});
		expect(m.snapshotted).toBe(true);
		expect(Object.keys(m.rows).sort()).toEqual(['["a"]', '["b"]']);
		expect(m.lint).toEqual(EMPTY_LINT);
	});

	it("upserts by path and removes by path", () => {
		let m = foldWorkflow(WORKFLOW_MIRROR_EMPTY, {
			kind: "batch",
			patches: [{ kind: "summary_upsert", rows: [summaryRow('["a"]', 1), summaryRow('["b"]')] }],
		});
		m = foldWorkflow(m, {
			kind: "batch",
			patches: [
				{ kind: "summary_upsert", rows: [summaryRow('["a"]', 5)] },
				{ kind: "summary_remove", paths: ['["b"]'] },
			],
		});
		expect(Object.keys(m.rows)).toEqual(['["a"]']);
		expect(m.rows['["a"]']?.populated).toBe(5);
	});

	it("sets recomputing and settles it on the next batch (even empty)", () => {
		let m = foldWorkflow(WORKFLOW_MIRROR_EMPTY, { kind: "recomputing" });
		expect(m.recomputing).toBe(true);
		expect(m.snapshotted).toBe(false);
		m = foldWorkflow(m, { kind: "batch", patches: [] });
		expect(m.recomputing).toBe(false);
		expect(m.snapshotted).toBe(true);
	});

	it("ignores malformed and unknown payloads", () => {
		for (const payload of [null, 42, "batch", {}, { kind: "mystery_variant" }, { kind: 7 }]) {
			expect(foldWorkflow(WORKFLOW_MIRROR_EMPTY, payload)).toBe(WORKFLOW_MIRROR_EMPTY);
		}
	});
});

describe("foldCells", () => {
	it("folds meta + rows keyed by coords text", () => {
		const m = foldCells(CELLS_MIRROR_EMPTY, {
			kind: "batch",
			patches: [META, { kind: "cells_upsert", cells: [cellRow(["a", 3]), cellRow([])] }],
		});
		expect(m.snapshotted).toBe(true);
		expect(m.meta?.grant.token).toBe("tok");
		expect(Object.keys(m.cells).sort()).toEqual(['["a",3]', "[]"]);
	});

	it("removes rows by coords tuple", () => {
		let m = foldCells(CELLS_MIRROR_EMPTY, {
			kind: "batch",
			patches: [META, { kind: "cells_upsert", cells: [cellRow(["a", 3]), cellRow(["b", 4])] }],
		});
		m = foldCells(m, { kind: "batch", patches: [{ kind: "cells_remove", coords: [["a", 3]] }] });
		expect(Object.keys(m.cells)).toEqual(['["b",4]']);
	});

	it("subject_gone resets the whole mirror (a re-creation re-upserts from empty)", () => {
		let m = foldCells(CELLS_MIRROR_EMPTY, {
			kind: "batch",
			patches: [META, { kind: "cells_upsert", cells: [cellRow(["a", 3])] }],
		});
		m = foldCells(m, { kind: "recomputing" });
		m = foldCells(m, { kind: "subject_gone" });
		expect(m).toMatchObject({ snapshotted: true, gone: true, recomputing: false, meta: null });
		expect(m.cells).toEqual({});
		// Re-creation: a fresh snapshot batch on the same live watch.
		m = foldCells(m, {
			kind: "batch",
			patches: [META, { kind: "cells_upsert", cells: [cellRow(["z", 1])] }],
		});
		expect(m.gone).toBe(false);
		expect(Object.keys(m.cells)).toEqual(['["z",1]']);
	});
});

describe("foldCell", () => {
	it("folds the drawer bundle patches", () => {
		const m = foldCell(CELL_MIRROR_EMPTY, {
			kind: "batch",
			patches: [
				{ kind: "report", report: { ...EMPTY_REPORT, status: "complete" } },
				{
					kind: "generation",
					report: {
						generation: "g1",
						status: "complete",
						executed_fn: null,
						failures: [],
						observations: [],
						unanswered_requests: [],
						checkpoints: [],
					},
				},
				{ kind: "observers", observers: [] },
			],
		});
		expect(m.snapshotted).toBe(true);
		expect(m.report?.status).toBe("complete");
		expect(m.generation?.generation).toBe("g1");
	});

	it("log_reset restarts entries; log_append extends them", () => {
		let m = foldCell(CELL_MIRROR_EMPTY, { kind: "log_reset", generation: "g1" });
		m = foldCell(m, { kind: "log_append", entries: [logEntry("one"), logEntry("two")] });
		expect(m.log).toMatchObject({ generation: "g1" });
		expect(m.log?.entries.map((e) => e.message)).toEqual(["one", "two"]);
		m = foldCell(m, { kind: "log_reset", generation: "g2" });
		expect(m.log).toEqual({ generation: "g2", entries: [] });
		m = foldCell(m, { kind: "log_append", entries: [logEntry("three")] });
		expect(m.log?.entries.map((e) => e.message)).toEqual(["three"]);
	});

	it("caps retained log entries (drop-oldest, mirroring the buffer bound)", () => {
		let m = foldCell(CELL_MIRROR_EMPTY, { kind: "log_reset", generation: "g1" });
		m = foldCell(m, {
			kind: "log_append",
			entries: Array.from({ length: MAX_LOG_ENTRIES + 5 }, (_, i) => logEntry(`m${i}`)),
		});
		expect(m.log?.entries).toHaveLength(MAX_LOG_ENTRIES);
		expect(m.log?.entries[0]?.message).toBe("m5");
	});

	it("subject_gone resets everything including the log", () => {
		let m = foldCell(CELL_MIRROR_EMPTY, {
			kind: "batch",
			patches: [{ kind: "report", report: { ...EMPTY_REPORT, status: "complete" } }],
		});
		m = foldCell(m, { kind: "log_reset", generation: "g1" });
		m = foldCell(m, { kind: "subject_gone" });
		expect(m).toMatchObject({ snapshotted: true, gone: true, report: null, log: null });
	});
});

describe("panel state shape check", () => {
	it("accepts the initial state and rejects stale shapes", () => {
		expect(isPanelState(PANEL_INITIAL)).toBe(true);
		// biome-ignore lint/suspicious/noExplicitAny: deliberately malformed inputs
		const bad = (v: any) => expect(isPanelState(v)).toBe(false);
		bad(null);
		bad({});
		// The previous shape (v7 tag): different kind ⇒ reset.
		bad({ ...PANEL_INITIAL, kind: "experiment-dashboard/v7" });
		// Tagged but structurally incomplete (the HMR window case).
		bad({ kind: PANEL_INITIAL.kind, actions: [], ui: {} });
		const missingSlot = {
			...PANEL_INITIAL,
			watches: { workflow: null, cells: null },
		} as unknown as PanelState;
		bad(missingSlot);
	});
});
