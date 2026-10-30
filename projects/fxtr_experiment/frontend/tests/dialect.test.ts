/**
 * The live python↔typescript dialect round-trip test (the chunk-4
 * plan's settled design): spawns the dialect fixture backend (real
 * `fxtr_experiment` dataclasses through the real service-dialect
 * encode + http escape), connects with the real
 * `@fxtr-ui/connector-http`, and checks BOTH directions —
 *
 * - decode: every served dataclass family lands in our TS wire types
 *   (field names, null conventions, tuple→array, stub leaves);
 * - encode: a TS-built `ViewSel` (the sorted-pairs `bound` strategy)
 *   and a round-tripped `TargetRecord` are decoded Python-side through
 *   the same per-annotation dialect codec typed dispatch uses, and
 *   echoed back equal.
 *
 * Drift on either side fails here deterministically. (The typed
 * dispatch *binding* layer — method-name/kwarg resolution — is not in
 * this loop; the live demo exercises it continuously.) Requires local
 * `uv` + the experiment demo's env, like the connector-http and
 * connector-workstation integration suites.
 */

import { HttpConnector, type HttpServiceRecord, type HttpSession } from "@fxtr-ui/connector-http";
import type { EntityRefStub, OpResultStub } from "@fxtr-ui/core";
import { EntityRefStub as EntityRefStubClass, memoryStorage } from "@fxtr-ui/core";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { lintClean, lintSound, viewSel } from "../src/wire/helpers.js";
import type {
	BindingInfo,
	CellBatch,
	CellInfo,
	CellLogAppend,
	CellLogReset,
	CellReport,
	CellWatchEvent,
	CellsBatch,
	CellsWatchEvent,
	DeleteReport,
	GenerationReport,
	GraphBatch,
	GraphWatchEvent,
	GridGraph,
	GridMeta,
	KeyInfo,
	LintReport,
	ObserverInfo,
	SourcingInfo,
	TargetInfo,
	ViewGrant,
	ViewSel,
	WorkflowBatch,
	WorkflowSummary,
	WorkflowWatchEvent,
} from "../src/wire/types.js";
import { type FixtureBackend, startFixtureBackend } from "./_fixture_harness.js";

const tick = () => new Promise<void>((r) => setTimeout(r, 10));
async function waitFor(pred: () => boolean, label = "condition"): Promise<void> {
	for (let i = 0; i < 500; i++) {
		if (pred()) return;
		await tick();
	}
	throw new Error(`waitFor timed out: ${label}`);
}

/** Checked indexed access (noUncheckedIndexedAccess without non-null
 * assertions): throws if the element is absent. */
function at<T>(items: readonly T[], i: number): T {
	const v = items[i];
	if (v === undefined) throw new Error(`missing element ${i}`);
	return v;
}

let backend: FixtureBackend;

beforeAll(async () => {
	backend = await startFixtureBackend();
}, 90_000);

afterAll(async () => {
	await backend?.close();
}, 30_000);

const liveConnectors: HttpConnector[] = [];
afterEach(() => {
	for (const c of liveConnectors.splice(0)) c.close();
});

interface Ctx {
	readonly session: HttpSession;
	invoke<T>(method: string, args?: readonly unknown[]): Promise<T>;
}

/** One connector + session; `invoke` fires a typed-loose invoke on the
 * fixture service and awaits its decoded fill. */
async function open(): Promise<Ctx> {
	const c = new HttpConnector({ baseUrl: backend.baseUrl, storage: memoryStorage() });
	liveConnectors.push(c);
	await c.refresh();
	const svcs = c.store.get(c.listServices().records);
	const fixture = svcs.find((s) => s.name === "dialect-fixture") as HttpServiceRecord;
	expect(fixture).toBeTruthy();
	const session = (await c.createSession()) as HttpSession;
	let n = 0;
	const invoke = async <T>(method: string, args: readonly unknown[] = []): Promise<T> => {
		let op!: OpResultStub;
		const key = `op${n++}`;
		session.store.set(session.userState, (prev, ctx) => {
			// biome-ignore lint/suspicious/noExplicitAny: loose invoke (the harness pins wire shapes, not the proto)
			op = ctx.allocateInvoke(fixture.ref, method, args as any);
			return { ...((prev ?? {}) as object), [key]: op };
		});
		await waitFor(() => session.store.get(op.result).status !== "pending", method);
		const r = session.store.get(op.result);
		if (r.status === "errored") {
			throw new Error(`${method} errored: ${(r as { message?: string }).message}`);
		}
		return (r as { value: T }).value;
	};
	return { session, invoke };
}

describe("dialect round trip (live python fixture backend)", () => {
	it("decodes every report/summary dataclass family into the TS wire types", async () => {
		const ctx = await open();

		const grants = await ctx.invoke<readonly ViewGrant[]>("get_view_grants");
		expect(grants).toHaveLength(2);
		expect(at(grants, 0)).toMatchObject({ token: "tok-task", kind: "task", mode: "read" });
		expect(at(grants, 0).axes).toEqual([
			{ name: "x", kind: "str" },
			{ name: "i", kind: "int" },
		]);
		expect(at(grants, 0).value).toBeNull();
		expect(at(grants, 1)).toMatchObject({ kind: "coord", value: 7, axes: [] });

		const keys = await ctx.invoke<readonly KeyInfo[]>("get_key_infos");
		expect(at(keys, 0)).toMatchObject({ key: "t", kind: "task", grounding: "grounded" });
		expect(at(keys, 0).grounding_reason).toBeNull();
		expect(at(keys, 0).config_id).toBeInstanceOf(EntityRefStubClass);
		// The all-defaults form: mashumaro emits every field as null.
		expect(at(keys, 1)).toMatchObject({
			key: "d",
			kind: "data",
			axes: [],
			grounding: null,
			grounding_reason: null,
			config_id: null,
		});

		const cells = await ctx.invoke<readonly CellInfo[]>("get_cell_infos");
		expect(at(cells, 0)).toMatchObject({
			coords: ["a", 3],
			status: "errored",
			currency: "stale",
			reason: "boom",
			grounding: "orphaned",
			wanted: false,
		});
		expect(at(cells, 1)).toMatchObject({ coords: [], status: null, wanted: null });

		const reports = await ctx.invoke<readonly CellReport[]>("get_cell_reports");
		expect(at(reports, 0).status).toBe("complete");
		expect(at(reports, 0).results).toBeTruthy();
		const results = at(reports, 0).results as Record<string, EntityRefStub>;
		expect(Object.keys(results).sort()).toEqual(["extra", "result"]);
		expect(results.result).toBeInstanceOf(EntityRefStubClass);
		expect(at(reports, 1).value).toBeInstanceOf(EntityRefStubClass);
		expect(at(reports, 1).bound).toBe(true);
		expect(at(reports, 2)).toMatchObject({
			status: null,
			currency: null,
			value: null,
			bound: null,
		});

		const summary = await ctx.invoke<WorkflowSummary>("get_workflow_summary");
		expect(summary.grids).toHaveLength(1);
		const grid = at(summary.grids, 0);
		expect(grid).toMatchObject({
			path: '["t"]',
			kind: "task",
			grounding: "unanchored",
			sourcing: "attached",
			populated: 3,
			n_running: 1,
			n_stale: 2,
			n_unsettled: 1,
			targets_attached: 1,
			targets_dangling: 0,
		});
		expect(grid.config_id).toBeInstanceOf(EntityRefStubClass);

		const gen = await ctx.invoke<GenerationReport>("get_generation_report");
		expect(gen.status).toBe("interrupted");
		expect(at(gen.failures, 0)).toMatchObject({ kind: "fault", error_class: "RunnerCrash" });
		expect(at(gen.observations, 0)).toMatchObject({
			subject_path: '["d"]',
			kind: "cell_value",
			current: "changed",
		});
		expect(at(gen.observations, 0).fingerprint).toBeInstanceOf(EntityRefStubClass);
		expect(at(gen.unanswered_requests, 0)).toMatchObject({ kind: "task_output" });
		expect(at(gen.checkpoints, 0).step_type_id).toBeInstanceOf(EntityRefStubClass);

		const observers = await ctx.invoke<readonly ObserverInfo[]>("get_observer_infos");
		expect(at(observers, 0)).toMatchObject({ status: "running", currency: null });

		const targets = await ctx.invoke<readonly TargetInfo[]>("get_target_infos");
		expect(at(targets, 0).status).toBe("attached");
		expect(at(targets, 0).grid_grounding).toBe("grounded");
		expect(at(targets, 0)).toMatchObject({ grid_path: '["t"]', coords: '["a",3]' });
		expect(at(targets, 0).target.expected_config).toBeInstanceOf(EntityRefStubClass);

		// TODO(chunk 5 — plans/phase6_sandboxing/chunk5_monitoring.md): the
		// mirror is `{key, cells}` while the wire also carries `state`,
		// `deps`, `config` and `sandbox`. Widen it with the bindings panel
		// and assert the new fields here — decode is structural, so the
		// missing fields cannot fail this test on their own.
		const bindings = await ctx.invoke<readonly BindingInfo[]>("get_binding_infos");
		expect(bindings).toHaveLength(2);
		expect(at(bindings, 0).key).toBeInstanceOf(EntityRefStubClass);
		expect(at(bindings, 0).cells).toEqual([{ grid_path: '["fns"]', coords: '["solo"]' }]);
		expect(at(bindings, 1).cells).toEqual([]);

		const sourcing = await ctx.invoke<readonly SourcingInfo[]>("get_sourcing_infos");
		expect(at(sourcing, 0)).toMatchObject({ grid_path: '["d"]', status: "attached" });

		const lint = await ctx.invoke<LintReport>("get_lint_report");
		expect(at(lint.dangling_sourcing, 0).status).toBe("dangling_missing");
		expect(at(lint.dangling_targets, 0).status).toBe("dangling_mismatched");
		expect(at(lint.target_findings, 0)).toMatchObject({ verdict: "unanchored" });
		expect(at(lint.ungrounded, 0)).toMatchObject({
			kind: "task_cell",
			verdict: "orphaned",
			reason: null,
		});
		expect(lintSound(lint)).toBe(false);
		expect(lintClean(lint)).toBe(false);

		const del = await ctx.invoke<DeleteReport>("get_delete_report");
		expect(del.broken_referrers).toEqual(['["t"]', '["u"]']);
	});

	it("decodes every watch-event union variant into the TS event mirrors", async () => {
		const ctx = await open();

		// -- watch_workflow --
		const wf = await ctx.invoke<readonly WorkflowWatchEvent[]>("get_workflow_watch_events");
		expect(wf.map((e) => e.kind)).toEqual(["batch", "recomputing", "batch"]);
		const wfBatch = at(wf, 0) as WorkflowBatch;
		expect(wfBatch.patches.map((p) => p.kind)).toEqual([
			"summary_upsert",
			"summary_remove",
			"lint",
			"targets",
			"sourcing",
			"bindings",
		]);
		const upsert = at(wfBatch.patches, 0);
		if (upsert.kind !== "summary_upsert") throw new Error("expected summary_upsert");
		expect(at(upsert.rows, 0)).toMatchObject({ path: '["t"]', kind: "task", populated: 3 });
		expect(at(upsert.rows, 0).config_id).toBeInstanceOf(EntityRefStubClass);
		const remove = at(wfBatch.patches, 1);
		if (remove.kind !== "summary_remove") throw new Error("expected summary_remove");
		expect(remove.paths).toEqual(['["gone"]']);
		const lintPatch = at(wfBatch.patches, 2);
		if (lintPatch.kind !== "lint") throw new Error("expected lint");
		expect(lintSound(lintPatch.report)).toBe(false);
		const targetsPatch = at(wfBatch.patches, 3);
		if (targetsPatch.kind !== "targets") throw new Error("expected targets");
		expect(at(targetsPatch.targets, 0).target.expected_config).toBeInstanceOf(EntityRefStubClass);
		// The settle-marker form: a legal empty batch.
		expect((at(wf, 2) as WorkflowBatch).patches).toEqual([]);

		// -- watch_cells --
		const cs = await ctx.invoke<readonly CellsWatchEvent[]>("get_cells_watch_events");
		expect(cs.map((e) => e.kind)).toEqual(["batch", "batch", "recomputing", "subject_gone"]);
		const csBatch = at(cs, 0) as CellsBatch;
		expect(csBatch.patches.map((p) => p.kind)).toEqual(["meta", "cells_upsert", "cells_remove"]);
		const meta = at(csBatch.patches, 0) as GridMeta;
		expect(meta).toMatchObject({
			path: '["t"]',
			grounding: "grounded",
			sourcing: "attached",
			parent_token: "tok-parent",
		});
		expect(meta.config_id).toBeInstanceOf(EntityRefStubClass);
		expect(meta.grant.token).toBe("tok-grid");
		expect(Object.keys(meta.grant.outputs ?? {})).toEqual(["result"]);
		const csUpsert = at(csBatch.patches, 1);
		if (csUpsert.kind !== "cells_upsert") throw new Error("expected cells_upsert");
		expect(at(csUpsert.cells, 0).coords).toEqual(["a", 3]);
		const csRemove = at(csBatch.patches, 2);
		if (csRemove.kind !== "cells_remove") throw new Error("expected cells_remove");
		expect(csRemove.coords).toEqual([["a", 3], []]);
		const rootMeta = at((at(cs, 1) as CellsBatch).patches, 0) as GridMeta;
		expect(rootMeta.parent_token).toBeNull();

		// -- watch_cell --
		const ce = await ctx.invoke<readonly CellWatchEvent[]>("get_cell_watch_events");
		expect(ce.map((e) => e.kind)).toEqual([
			"batch",
			"recomputing",
			"subject_gone",
			"log_reset",
			"log_append",
		]);
		const ceBatch = at(ce, 0) as CellBatch;
		expect(ceBatch.patches.map((p) => p.kind)).toEqual(["report", "generation", "observers"]);
		const reportPatch = at(ceBatch.patches, 0);
		if (reportPatch.kind !== "report") throw new Error("expected report");
		expect(reportPatch.report.status).toBe("complete");
		const genPatch = at(ceBatch.patches, 1);
		if (genPatch.kind !== "generation") throw new Error("expected generation");
		expect(genPatch.report.status).toBe("interrupted");
		expect(at(ce, 3) as CellLogReset).toMatchObject({ generation: "1".repeat(32) });
		const logAppend = at(ce, 4) as CellLogAppend;
		expect(logAppend.entries).toEqual([
			{
				ts: "t0",
				entry_kind: "step_started",
				step: { grid_path: '["t","steps"]', coords: "[0]" },
				message: "step started",
			},
			{
				ts: "t1",
				entry_kind: "log",
				step: { grid_path: '["t","steps"]', coords: "[0]" },
				message: "hi from the step",
			},
			{ ts: "t2", entry_kind: "wrote_cell", step: null, message: 'wrote ["d"]' },
			{
				ts: "t3",
				entry_kind: "step_errored",
				step: { grid_path: '["t","steps"]', coords: "[1]" },
				message: "step raised: boom",
			},
		]);
	});

	it("decodes the grid-graph overlay and watch_graph variants", async () => {
		const ctx = await open();

		const g = await ctx.invoke<GridGraph>("get_grid_graph");
		expect(g.edges.map((e) => [e.observer_path, e.subject_path, e.kind])).toEqual([
			['["t"]', '["d"]', "read"],
			['["t"]', '["fns"]', "read"],
			['["t"]', '["out"]', "wrote_cell"],
			['["t"]', '["made"]', "grid_assertion"],
		]);
		expect(at(g.edges, 0).count).toBe(2);
		expect(at(g.input_grants, 0)).toMatchObject({
			target_path: '["fns"]',
			is_task_fn: true,
			kept: false,
		});
		expect(at(g.input_grants, 1)).toMatchObject({
			arg_name: "inp",
			is_task_fn: false,
			mode: "read",
			kept: true,
		});
		expect(at(g.correspondences, 0)).toEqual({
			grid_a: '["d"]',
			axis_a: "x",
			grid_b: '["t"]',
			axis_b: "x",
		});

		const gw = await ctx.invoke<readonly GraphWatchEvent[]>("get_graph_watch_events");
		expect(gw.map((e) => e.kind)).toEqual(["batch", "recomputing", "batch"]);
		const gwBatch = at(gw, 0) as GraphBatch;
		expect(gwBatch.patches.map((p) => p.kind)).toEqual(["graph"]);
		expect(at(gwBatch.patches, 0).graph.correspondences).toHaveLength(1);
		// The settle-marker form: a legal empty batch.
		expect((at(gw, 2) as GraphBatch).patches).toEqual([]);
	});

	it("encodes a TS-built ViewSel that the Python dialect decodes (echoed equal)", async () => {
		const ctx = await open();
		// Deliberately built with unsorted input; viewSel canonicalizes.
		const sel = viewSel("tok-task", { 1: 3, 0: "a" }, "result");
		expect(sel.bound).toEqual([
			[0, "a"],
			[1, 3],
		]);
		const echoed = await ctx.invoke<ViewSel>("echo_sel", [sel]);
		expect(echoed).toEqual(sel);

		// The empty form (no bound positions, no output).
		const empty = viewSel("tok-task");
		const echoedEmpty = await ctx.invoke<ViewSel>("echo_sel", [empty]);
		expect(echoedEmpty).toEqual(empty);
	});

	it("round-trips a TargetRecord verbatim (the demand_target contract)", async () => {
		const ctx = await open();
		const targets = await ctx.invoke<readonly TargetInfo[]>("get_target_infos");
		const record = at(targets, 0).target;
		const echoed = await ctx.invoke<TargetInfo["target"]>("echo_target", [record]);
		// Stub leaves intern per connector, so the echoed record is
		// structurally identical including the stub instance.
		expect(echoed).toEqual(record);
		expect(echoed.expected_config).toBe(record.expected_config);
	});
});
