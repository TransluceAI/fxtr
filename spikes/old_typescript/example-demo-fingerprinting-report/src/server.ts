/**
 * Fingerprint report server: reads judge results, conversations, and simulator data,
 * builds an entity graph, and serves it via fxtr HTTP + Vite dev server.
 *
 * Usage: tsx src/server.ts <data-dir>
 *
 * The data directory must contain:
 *   results/                  — Judge result JSON files (one per transcript)
 *     {id}.json               — { transcript_id, metadata: { sim_key, family_name, subject_model_name, ... },
 *                                 results: { [dimension]: { score: { [category]: 0|10 }, reasoning } } }
 *   data/conversations/       — Raw conversation JSON files
 *     {id}.json               — { settings: {...}, transcript: [{ role, content }, ...] }
 *   data/user_simulators_uwbot.json
 *                             — { [sim_key]: { name, description, tags, botchat_template__user_bio,
 *                                              botchat_template__initial_user_message } }
 *   mh_judge_lib_pkg/mh_judge_lib/data/multi_fingerprint_specs.toml
 *                             — [[multi_fingerprint_specs]] array with { name, description,
 *                                valid_categories, is_example }
 */

import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { type FxtrCbor, type FxtrID, GLOBAL_SCOPE, RawWeakFxtrID, withFxtrScope } from "@fxtr/core";
import { createFxtrApp } from "@fxtr/http-server";
import { type Component, evaluateMdx, passthroughCustomComponent } from "@fxtr/mdx-transform";
import { LinkedSVGFigure, RootReport, SubReport } from "@fxtr/reports-entities";
import { parse as parseTOML } from "smol-toml";
import { createServer as createViteServer } from "vite";
import { DIM_COLORS } from "./dimColors.js";
import {
	CategoryAggregate,
	FingerprintGraderResult,
	FingerprintedTranscript,
	RubricText,
	SimulatorBio,
	Transcript,
} from "./entities.js";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const DATA_DIR = process.argv[2];
if (!DATA_DIR) {
	console.error("Usage: tsx src/server.ts <data-dir>");
	console.error("");
	console.error("The data directory must contain:");
	console.error("  results/*.json                 — Judge result files");
	console.error("  data/conversations/*.json      — Raw conversation files");
	console.error("  data/user_simulators_uwbot.json");
	console.error("  mh_judge_lib_pkg/mh_judge_lib/data/multi_fingerprint_specs.toml");
	process.exit(1);
}

const RESULTS_DIR = path.join(DATA_DIR, "results");
const CONVERSATIONS_DIR = path.join(DATA_DIR, "data/conversations");
const SPECS_PATH = path.join(
	DATA_DIR,
	"mh_judge_lib_pkg/mh_judge_lib/data/multi_fingerprint_specs.toml",
);
const SIMULATORS_PATH = path.join(DATA_DIR, "data/user_simulators_uwbot.json");

// ---------------------------------------------------------------------------
// Data types for JSON loading
// ---------------------------------------------------------------------------

interface JudgeOutput {
	score: number | Record<string, number>;
	reasoning: string;
}

interface ResultData {
	transcript_id: string;
	metadata: Record<string, unknown>;
	results: Record<string, JudgeOutput>;
}

interface ConversationData {
	settings: Record<string, unknown>;
	transcript: Array<{ role: string; content: string }>;
}

interface FingerprintSpec {
	name: string;
	description: string;
	valid_categories: string[];
	is_example: boolean;
}

interface SimulatorData {
	name: string;
	description: string;
	tags: string[];
	botchat_template__user_bio: string;
	botchat_template__initial_user_message: string | null;
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function loadResults(): Promise<ResultData[]> {
	const files = await fs.readdir(RESULTS_DIR);
	const jsonFiles = files.filter((f) => f.endsWith(".json")).sort();
	const results: ResultData[] = [];
	for (const file of jsonFiles) {
		const data = JSON.parse(await fs.readFile(path.join(RESULTS_DIR, file), "utf-8"));
		results.push(data);
	}
	console.log(`  Loaded ${results.length} result files`);
	return results;
}

async function loadConversations(neededIds: Set<string>): Promise<Map<string, ConversationData>> {
	const convs = new Map<string, ConversationData>();
	for (const id of neededIds) {
		const filePath = path.join(CONVERSATIONS_DIR, `${id}.json`);
		try {
			const data = JSON.parse(await fs.readFile(filePath, "utf-8"));
			convs.set(id, data);
		} catch {
			// Skip missing conversations
		}
	}
	console.log(`  Loaded ${convs.size} conversations`);
	return convs;
}

async function loadSpecs(): Promise<FingerprintSpec[]> {
	const raw = await fs.readFile(SPECS_PATH, "utf-8");
	const parsed = parseTOML(raw) as unknown as { multi_fingerprint_specs: FingerprintSpec[] };
	const specs = parsed.multi_fingerprint_specs.filter((s) => !s.is_example);
	console.log(`  Loaded ${specs.length} fingerprint specs`);
	return specs;
}

async function loadSimulators(): Promise<Record<string, SimulatorData>> {
	const raw = await fs.readFile(SIMULATORS_PATH, "utf-8");
	const sims = JSON.parse(raw);
	console.log(`  Loaded ${Object.keys(sims).length} simulators`);
	return sims;
}

// ---------------------------------------------------------------------------
// SVG bar chart builder
// ---------------------------------------------------------------------------

interface CategoryInfo {
	name: string;
	dimension: string;
	prevalence: number;
	color: string;
	count: number;
}

function buildBarChartSVG(categories: CategoryInfo[]): unknown {
	// Layout: one column per dimension, scrolls horizontally.
	// Each column has a header, then horizontal bars for its categories.
	const barHeight = 18;
	const barMaxWidth = 140;
	const labelWidth = 180;
	const colPadding = 16;
	const colWidth = labelWidth + barMaxWidth + 50;
	const headerHeight = 50;
	const colGap = 8;

	// Group categories by dimension, preserving order
	const dimGroups: Array<{ dimension: string; color: string; cats: CategoryInfo[] }> = [];
	let currentGroup: (typeof dimGroups)[number] | null = null;
	for (const cat of categories) {
		if (!currentGroup || currentGroup.dimension !== cat.dimension) {
			currentGroup = { dimension: cat.dimension, color: cat.color, cats: [] };
			dimGroups.push(currentGroup);
		}
		currentGroup.cats.push(cat);
	}

	// Compute total SVG size
	const maxCatsInCol = Math.max(...dimGroups.map((g) => g.cats.length));
	const svgHeight = headerHeight + maxCatsInCol * (barHeight + 2) + 20;
	const svgWidth = dimGroups.length * (colWidth + colGap) - colGap;

	const children: unknown[] = [];

	// Background
	children.push([
		"rect",
		{
			x: "0",
			y: "0",
			width: String(svgWidth),
			height: String(svgHeight),
			fill: "#fafafa",
			rx: "4",
		},
		[],
	]);

	// Columns
	for (let gi = 0; gi < dimGroups.length; gi++) {
		const group = dimGroups[gi];
		if (!group) continue;
		const colX = gi * (colWidth + colGap);

		// Column background
		children.push([
			"rect",
			{
				x: String(colX),
				y: "0",
				width: String(colWidth),
				height: String(svgHeight),
				fill: gi % 2 === 0 ? "#fafafa" : "#f4f4f4",
				rx: "4",
			},
			[],
		]);

		// Dimension header
		children.push([
			"text",
			{
				x: String(colX + colPadding),
				y: "20",
				fontSize: "11",
				fontWeight: "bold",
				fill: group.color,
			},
			[group.dimension.replaceAll("_", " ")],
		]);

		// Category count subtitle
		children.push([
			"text",
			{
				x: String(colX + colPadding),
				y: "35",
				fontSize: "9",
				fill: "#999",
			},
			[`${group.cats.length} categories`],
		]);

		// Bars
		for (let ci = 0; ci < group.cats.length; ci++) {
			const cat = group.cats[ci];
			if (!cat) continue;
			const barY = headerHeight + ci * (barHeight + 2);
			const barW = Math.max(1, cat.prevalence * barMaxWidth);

			// Bar
			children.push([
				"rect",
				{
					x: String(colX + labelWidth),
					y: String(barY),
					width: String(barW),
					height: String(barHeight - 2),
					fill: group.color,
					rx: "2",
					"data-preview-target": cat.name,
				},
				[],
			]);

			// Label
			children.push([
				"text",
				{
					x: String(colX + labelWidth - 4),
					y: String(barY + barHeight - 5),
					textAnchor: "end",
					fontSize: "9",
					fill: "#333",
				},
				[cat.name.replaceAll("_", " ")],
			]);

			// Percentage
			children.push([
				"text",
				{
					x: String(colX + labelWidth + barW + 3),
					y: String(barY + barHeight - 5),
					fontSize: "8",
					fill: "#888",
				},
				[`${(cat.prevalence * 100).toFixed(0)}%`],
			]);
		}
	}

	return [
		"svg",
		{
			width: String(svgWidth),
			height: String(svgHeight),
			viewBox: `0 0 ${svgWidth} ${svgHeight}`,
			xmlns: "http://www.w3.org/2000/svg",
			style: { maxWidth: "none" },
		},
		children,
	];
}

// ---------------------------------------------------------------------------
// Main server
// ---------------------------------------------------------------------------

await withFxtrScope(
	{ name: "fingerprint_report_server", include: [GLOBAL_SCOPE] },
	async (scope) => {
		const vite = await createViteServer({
			server: { middlewareMode: true },
			appType: "custom",
		});

		// Helper: compile MDX
		async function mdx(
			source: string,
			opts?: { components?: Record<string, Component>; props?: Record<string, unknown> },
		) {
			return evaluateMdx(source, {
				...opts,
				props: { fxtr_scope: scope, ...opts?.props },
			});
		}

		// ---- Load data ----
		console.log("Loading data...");
		const [results, specs, simulators] = await Promise.all([
			loadResults(),
			loadSpecs(),
			loadSimulators(),
		]);

		// Collect transcript IDs we need conversations for
		const transcriptIds = new Set(results.map((r) => r.transcript_id));
		const conversations = await loadConversations(transcriptIds);

		// ---- Build dimension color map ----
		const dimColorMap: Record<string, string> = {};
		for (let i = 0; i < specs.length; i++) {
			const spec = specs[i];
			if (spec) dimColorMap[spec.name] = DIM_COLORS[i % DIM_COLORS.length] ?? "#999";
		}

		// ---- Create SimulatorBio entities ----
		console.log("Creating entities...");
		const simBioEntities = new Map<string, { entityId: FxtrID }>();
		const neededSims = new Set<string>();
		for (const r of results) {
			const simKey = r.metadata.sim_key as string | undefined;
			if (simKey) neededSims.add(simKey);
		}

		for (const simKey of neededSims) {
			const sim = simulators[simKey];
			const bio = sim
				? SimulatorBio.create({
						label: simKey,
						bio: sim.botchat_template__user_bio ?? "",
						description: sim.description ?? "",
						tags: sim.tags ?? [],
						initialMessage: sim.botchat_template__initial_user_message ?? "",
					})
				: SimulatorBio.create({
						label: simKey,
						bio: "(simulator data not found)",
						description: "",
						tags: [],
						initialMessage: "",
					});
			const ref = await bio.store(scope);
			simBioEntities.set(simKey, ref);
		}
		console.log(`  Created ${simBioEntities.size} SimulatorBio entities`);

		// ---- Create RubricText entities (one per dimension, needed before grader results) ----
		const rubricEntities = new Map<string, { entityId: FxtrID }>();
		for (const spec of specs) {
			const rubric = RubricText.create({
				label: spec.name.replaceAll("_", " "),
				categoryName: "",
				text: spec.description,
			});
			const ref = await rubric.store(scope);
			rubricEntities.set(spec.name, ref);
		}
		console.log(`  Created ${rubricEntities.size} RubricText entities`);

		// ---- Create Transcript entities (raw conversation data) ----
		const rawTranscriptEntities = new Map<string, { entityId: FxtrID }>();
		for (const result of results) {
			const tid = result.transcript_id;
			const conv = conversations.get(tid);
			const simKey = (result.metadata.sim_key as string) ?? "unknown";
			const familyName = (result.metadata.family_name as string) ?? "unknown";
			const modelName = (result.metadata.subject_model_name as string) ?? "unknown";
			const messages = conv?.transcript ?? [];
			const simBio = simBioEntities.get(simKey);
			const shortSim = simKey.length > 40 ? `${simKey.slice(0, 40)}...` : simKey;

			const t = Transcript.create({
				label: `${tid} (${familyName} / ${shortSim})`,
				transcriptId: tid,
				simKey,
				familyName,
				modelName,
				messages: messages.map((m) => ({
					role: m.role,
					content: typeof m.content === "string" ? m.content : JSON.stringify(m.content),
				})),
				simulatorBio:
					simBio?.entityId ?? (simBioEntities.values().next().value?.entityId as FxtrID),
			});
			const ref = await t.store(scope);
			rawTranscriptEntities.set(tid, ref);
		}
		console.log(`  Created ${rawTranscriptEntities.size} Transcript entities`);

		// ---- Create FingerprintGraderResult + FingerprintedTranscript entities ----
		let graderResultCount = 0;
		const transcriptEntities = new Map<string, { entityId: FxtrID }>();
		for (const result of results) {
			const tid = result.transcript_id;
			const simKey = (result.metadata.sim_key as string) ?? "unknown";
			const familyName = (result.metadata.family_name as string) ?? "unknown";
			const modelName = (result.metadata.subject_model_name as string) ?? "unknown";
			const shortSim = simKey.length > 40 ? `${simKey.slice(0, 40)}...` : simKey;
			const simBio = simBioEntities.get(simKey);
			const rawTranscript = rawTranscriptEntities.get(tid);
			if (!rawTranscript) continue;

			// Create grader results for each fingerprint dimension
			const graderResultIds: FxtrID[] = [];
			for (const spec of specs) {
				const dimResult = result.results[spec.name];
				if (!dimResult || typeof dimResult.score !== "object") continue;

				const activeCategories = Object.entries(dimResult.score)
					.filter(([, val]) => val >= 10.0)
					.map(([cat]) => cat);

				const rubricRef = rubricEntities.get(spec.name);
				const gr = FingerprintGraderResult.create({
					label: `${spec.name.replaceAll("_", " ")} — transcript ${tid}`,
					dimensionName: spec.name,
					reasoning: dimResult.reasoning ?? "",
					activeCategories,
					allCategories: spec.valid_categories,
					rubric: rubricRef?.entityId as FxtrID,
					transcript: rawTranscript.entityId,
				});
				const ref = await gr.store(scope);
				graderResultIds.push(ref.entityId);
				graderResultCount++;
			}

			const ft = FingerprintedTranscript.create({
				label: `${tid} (${familyName} / ${shortSim})`,
				transcriptId: tid,
				simKey,
				familyName,
				modelName,
				transcript: rawTranscript.entityId,
				simulatorBio:
					simBio?.entityId ?? (simBioEntities.values().next().value?.entityId as FxtrID),
				graderResults: graderResultIds,
			});
			const ref = await ft.store(scope);
			transcriptEntities.set(tid, ref);
		}
		console.log(`  Created ${transcriptEntities.size} FingerprintedTranscript entities`);
		console.log(`  Created ${graderResultCount} FingerprintGraderResult entities`);

		// ---- Compute prevalence per category ----
		interface CatData {
			name: string;
			dimension: string;
			prevalence: number;
			matchingTranscriptIds: string[];
		}

		const categoryData: CatData[] = [];
		for (const spec of specs) {
			for (const catName of spec.valid_categories) {
				const matching: string[] = [];
				for (const result of results) {
					const dimResult = result.results[spec.name];
					if (dimResult && typeof dimResult.score === "object") {
						const val = dimResult.score[catName];
						if (val !== undefined && val >= 10.0) {
							matching.push(result.transcript_id);
						}
					}
				}
				categoryData.push({
					name: catName,
					dimension: spec.name,
					prevalence: results.length > 0 ? matching.length / results.length : 0,
					matchingTranscriptIds: matching,
				});
			}
		}

		// ---- Create CategoryAggregate entities ----
		// Build lookup: dimension name → spec description (for rubric excerpts)
		const specByDim = new Map(specs.map((s) => [s.name, s.description]));

		function extractRubricExcerpt(dimName: string, catName: string): string {
			const desc = specByDim.get(dimName) ?? "";
			// Look for a line containing "category_name" (in quotes)
			const needle = `"${catName}"`;
			for (const line of desc.split("\n")) {
				if (line.includes(needle)) {
					return line.trim().replace(/^- /, "");
				}
			}
			return "";
		}

		const catAggEntities = new Map<string, { entityId: FxtrID }>();
		for (const cat of categoryData) {
			const rubricRef = rubricEntities.get(cat.dimension);
			const transcriptRefs = cat.matchingTranscriptIds
				.map((tid) => transcriptEntities.get(tid)?.entityId)
				.filter((id): id is FxtrID => id !== undefined);

			const agg = CategoryAggregate.create({
				label: cat.name.replaceAll("_", " "),
				dimensionName: cat.dimension.replaceAll("_", " "),
				prevalence: cat.prevalence,
				transcriptCount: transcriptRefs.length,
				rubricExcerpt: extractRubricExcerpt(cat.dimension, cat.name),
				rubric: rubricRef?.entityId as FxtrID,
				transcripts: transcriptRefs,
			});
			const ref = await agg.store(scope);
			catAggEntities.set(cat.name, ref);
		}
		console.log(`  Created ${catAggEntities.size} CategoryAggregate entities`);

		// ---- Build SVG bar chart ----
		const categoryInfos: CategoryInfo[] = categoryData.map((cat) => ({
			name: cat.name,
			dimension: cat.dimension,
			prevalence: cat.prevalence,
			color: dimColorMap[cat.dimension] ?? "#999",
			count: cat.matchingTranscriptIds.length,
		}));

		const svgLinks: Record<string, FxtrID> = {};
		for (const cat of categoryData) {
			const agg = catAggEntities.get(cat.name);
			if (agg) svgLinks[cat.name] = agg.entityId;
		}

		const svgData = buildBarChartSVG(categoryInfos);
		const svgFigure = await LinkedSVGFigure.create({
			label: "Category Prevalence",
			svg: svgData as FxtrCbor,
			links: svgLinks,
		}).store(scope);
		console.log("  Created LinkedSVGFigure (bar chart)");

		// ---- Create methodology SubReport ----
		const methodologyReport = await SubReport.create({
			label: "Methodology",
			overview: await mdx(
				[
					"Each fingerprint judge evaluates a single **dimension of variation** about the user in a conversation.",
					`There are **${specs.length} dimensions** with a total of **${categoryData.length} categories** across **${results.length} transcripts**.`,
					"",
					"An LLM judge reads the full transcript and decides, for each category, whether there is convincing evidence the user belongs to that category.",
					"Categories within a dimension are *not* mutually exclusive.",
				].join("\n"),
			),
			detail: await mdx(
				[
					"## Dimensions",
					"",
					...specs.map(
						(s) => `- **${s.name.replaceAll("_", " ")}** (${s.valid_categories.length} categories)`,
					),
					"",
					"## Scoring",
					"",
					"One LLM call per dimension per transcript. The judge outputs `true`/`false` for every category, stored as `10.0` (positive) or `0.0` (negative).",
					"",
					"## Data",
					"",
					`- ${results.length} transcripts evaluated`,
					`- ${neededSims.size} unique simulators`,
					`- Results read from \`${RESULTS_DIR}\``,
				].join("\n"),
			),
		}).store(scope);

		// ---- Create RootReport ----
		const rootContents = await mdx(
			[
				"This report visualizes the results of multi-fingerprint behavioral judges run on mental health conversation transcripts.",
				"The conversations are between simulated users (with detailed personas) and LLM assistants.",
				"",
				"## Category Prevalence",
				"",
				"The chart below shows how often each behavioral category was detected across all transcripts. **Click on a bar** to see which transcripts matched that category.",
				"",
				"<RenderFigure entity={props.svgFigure} />",
				"",
				"## About This Analysis",
				"",
				"<RenderFigure entity={props.methodology} />",
			].join("\n"),
			{
				components: {
					RenderFigure: passthroughCustomComponent("RenderFigure"),
				},
				props: {
					svgFigure: new RawWeakFxtrID(svgFigure.entityId),
					methodology: new RawWeakFxtrID(methodologyReport.entityId),
				},
			},
		);

		const rootReport = await RootReport.create({
			title: "Mental Health Fingerprint Analysis",
			summary: `Behavioral fingerprint analysis across ${results.length} transcripts and ${specs.length} dimensions.`,
			contents: rootContents,
		}).store(scope);

		// ---- Pins ----
		const pins: Record<string, FxtrID> = {
			"reports/fingerprint-overview": rootReport.entityId,
		};

		console.log("\nCreated entities:");
		for (const [name, id] of Object.entries(pins)) {
			console.log(`  ${name} → ${id.idString()}`);
		}

		// ---- HTTP server ----
		const fxtrApp = createFxtrApp(scope, pins);
		const indexHtmlPath = path.resolve(import.meta.dirname, "../index.html");

		const server = http.createServer((req, res) => {
			const url = req.url ?? "/";

			if (
				url.startsWith("/pin/") ||
				url.startsWith("/data/") ||
				url.startsWith("/preview/") ||
				url === "/pins" ||
				url.startsWith("/pins?")
			) {
				Promise.resolve(
					fxtrApp.fetch(
						new Request(`http://localhost${url}`, {
							method: req.method ?? "GET",
						}),
					),
				)
					.then(async (response: Response) => {
						const headers: Record<string, string> = {};
						response.headers.forEach((v, k) => {
							headers[k] = v;
						});
						res.writeHead(response.status, headers);
						const body = await response.arrayBuffer();
						res.end(Buffer.from(body));
					})
					.catch((err: unknown) => {
						res.writeHead(500);
						res.end(String(err));
					});
				return;
			}

			vite.middlewares(req, res, async () => {
				try {
					const rawHtml = await fs.readFile(indexHtmlPath, "utf-8");
					const html = await vite.transformIndexHtml(url, rawHtml);
					res.writeHead(200, { "Content-Type": "text/html" });
					res.end(html);
				} catch (err) {
					res.writeHead(500);
					res.end(String(err));
				}
			});
		});

		const port = 3458;
		server.listen(port, () => {
			console.log(`\nFingerprint report at http://localhost:${port}`);
			console.log(`  Pins: http://localhost:${port}/pins`);
		});

		await new Promise<void>((resolve) => {
			function shutdown() {
				console.log("\nShutting down...");
				server.closeAllConnections();
				server.close();
				vite.close().then(resolve, resolve);
			}
			process.on("SIGINT", shutdown);
			process.on("SIGTERM", shutdown);
		});
	},
);
