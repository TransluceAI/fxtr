/**
 * Tests for report entity definitions: round-trip store/load for each entity.
 */

import { type FxtrScope, GLOBAL_SCOPE, withFxtrScope } from "@fxtr/core";
import { describe, expect, it } from "vitest";
import { LinkedSVGFigure, RootReport, SimpleComment, SubReport } from "../src/entities.js";

async function withTestScope(fn: (scope: FxtrScope) => Promise<void>): Promise<void> {
	await withFxtrScope({ include: [GLOBAL_SCOPE] }, fn);
}

describe("RootReport", () => {
	it("round-trips store/load", async () => {
		await withTestScope(async (scope) => {
			const report = RootReport.create({
				title: "My Report",
				summary: "A brief summary",
				contents: [
					["h1", {}, ["Hello"]],
					["p", {}, ["World"]],
				],
			});
			expect(report.title).toBe("My Report");
			expect(report.summary).toBe("A brief summary");
			expect(report.contents).toHaveLength(2);

			const ref = await report.store(scope);
			const loaded = await ref.load(scope);
			expect(loaded.title).toBe("My Report");
			expect(loaded.summary).toBe("A brief summary");
			expect(loaded.contents).toHaveLength(2);
			expect(loaded.contents[0]).toEqual(["h1", {}, ["Hello"]]);
		});
	});
});

describe("SubReport", () => {
	it("round-trips store/load", async () => {
		await withTestScope(async (scope) => {
			const sub = SubReport.create({
				label: "Section A",
				overview: [["p", {}, ["Overview text"]]],
				detail: [["p", {}, ["Detailed text"]]],
			});
			const ref = await sub.store(scope);
			const loaded = await ref.load(scope);
			expect(loaded.label).toBe("Section A");
			expect(loaded.overview).toEqual([["p", {}, ["Overview text"]]]);
			expect(loaded.detail).toEqual([["p", {}, ["Detailed text"]]]);
		});
	});
});

describe("SimpleComment", () => {
	it("round-trips store/load", async () => {
		await withTestScope(async (scope) => {
			// Create a target entity to comment on
			const target = await RootReport.create({
				title: "Target",
				summary: "Target report",
				contents: [],
			}).store(scope);

			const comment = SimpleComment.create({
				author: "Alice",
				comment: "Great report!",
				target: target.entityId,
			});
			const ref = await comment.store(scope);
			const loaded = await ref.load(scope);
			expect(loaded.author).toBe("Alice");
			expect(loaded.comment).toBe("Great report!");
			expect(loaded.target.key()).toBe(target.entityId.key());
		});
	});
});

describe("LinkedSVGFigure", () => {
	it("round-trips store/load", async () => {
		await withTestScope(async (scope) => {
			// Create some entities to link to
			const sub1 = await SubReport.create({
				label: "Cat A",
				overview: [["p", {}, ["Category A"]]],
				detail: [],
			}).store(scope);
			const sub2 = await SubReport.create({
				label: "Cat B",
				overview: [["p", {}, ["Category B"]]],
				detail: [],
			}).store(scope);

			const figure = LinkedSVGFigure.create({
				label: "My Chart",
				svg: [
					"svg",
					{ width: "400", height: "200" },
					[
						[
							"rect",
							{
								x: "10",
								y: "10",
								width: "100",
								height: "80",
								"data-preview-target": "catA",
							},
							[],
						],
						[
							"rect",
							{
								x: "120",
								y: "10",
								width: "100",
								height: "60",
								"data-preview-target": "catB",
							},
							[],
						],
					],
				],
				links: {
					catA: sub1.entityId,
					catB: sub2.entityId,
				},
			});
			const ref = await figure.store(scope);
			const loaded = await ref.load(scope);
			expect(loaded.label).toBe("My Chart");
			expect(loaded.links.catA?.key()).toBe(sub1.entityId.key());
			expect(loaded.links.catB?.key()).toBe(sub2.entityId.key());
			// SVG is stored as unknown FxtrCbor — verify it round-trips
			const [type, props] = loaded.svg as [string, Record<string, unknown>];
			expect(type).toBe("svg");
			expect(props.width).toBe("400");
		});
	});
});
