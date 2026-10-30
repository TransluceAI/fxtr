/**
 * Demo report server: constructs report entities and serves them via fxtr HTTP.
 *
 * Creates:
 * - Report 1: full-featured report with a SubReport figure, LinkedSVGFigure,
 *   and reference links. The SVG links to three small sub-reports.
 * - Report 2: simple report that links back to Report 1.
 * - 3 SimpleComments: one on Report 1, one on Report 2, one on the SubReport.
 */

import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { type FxtrID, GLOBAL_SCOPE, RawWeakFxtrID, withFxtrScope } from "@fxtr/core";
import { createFxtrApp } from "@fxtr/http-server";
import { type Component, evaluateMdx, passthroughCustomComponent } from "@fxtr/mdx-transform";
import { LinkedSVGFigure, RootReport, SimpleComment, SubReport } from "@fxtr/reports-entities";
import { createServer as createViteServer } from "vite";

// ---- CLI flags ----
const singleEntity = process.argv.includes("--single-entity");

await withFxtrScope({ name: "demo_report_server", include: [GLOBAL_SCOPE] }, async (scope) => {
	// ---- Vite dev server (middleware mode) ----
	const vite = await createViteServer({
		server: { middlewareMode: true },
		appType: "custom",
	});

	// ---- Helper: compile MDX string into FxtrCbor[] ----
	async function mdx(
		source: string,
		opts?: { components?: Record<string, Component>; props?: Record<string, unknown> },
	) {
		return evaluateMdx(source, {
			...opts,
			props: { fxtr_scope: scope, ...opts?.props },
		});
	}

	// ---- Create sub-reports for the SVG figure ----
	const svgSubA = await SubReport.create({
		label: "Category A Details",
		overview: await mdx("**Category A** represents the largest segment at 45% of total revenue."),
		detail: await mdx(
			"Category A has shown consistent growth over the past 3 quarters, driven primarily by enterprise adoption. Key metrics include a 23% increase in average deal size and a 15% improvement in retention rate.",
		),
	}).store(scope);

	const svgSubB = await SubReport.create({
		label: "Category B Details",
		overview: await mdx("**Category B** accounts for 30% of revenue with strong growth potential."),
		detail: await mdx(
			"Category B is our fastest-growing segment year-over-year at 40% growth. Recent product improvements have expanded our addressable market significantly. We project this will become the leading category within 2 quarters.",
		),
	}).store(scope);

	const svgSubC = await SubReport.create({
		label: "Category C Details",
		overview: await mdx("**Category C** makes up the remaining 25%, serving as a stable base."),
		detail: await mdx(
			"Category C provides predictable recurring revenue with low churn. While growth is modest at 8% year-over-year, the high margins make it a valuable part of the portfolio. Expansion opportunities exist in adjacent verticals.",
		),
	}).store(scope);

	// ---- Create LinkedSVGFigure ----
	const svgFigure = await LinkedSVGFigure.create({
		label: "Revenue by Category",
		svg: [
			"svg",
			{
				width: "460",
				height: "220",
				viewBox: "0 0 460 220",
				xmlns: "http://www.w3.org/2000/svg",
			},
			[
				// Background
				[
					"rect",
					{
						x: "0",
						y: "0",
						width: "460",
						height: "220",
						fill: "#f8f9fa",
						rx: "8",
					},
					[],
				],
				// Title
				[
					"text",
					{
						x: "230",
						y: "30",
						textAnchor: "middle",
						fontWeight: "bold",
						fontSize: "14",
						fill: "#333",
					},
					["Revenue Distribution"],
				],
				// Bar A (45%)
				[
					"rect",
					{
						x: "40",
						y: "50",
						width: "120",
						height: "130",
						fill: "#4a90d9",
						rx: "4",
						"data-preview-target": "catA",
					},
					[],
				],
				[
					"text",
					{
						x: "100",
						y: "200",
						textAnchor: "middle",
						fontSize: "12",
						fill: "#666",
					},
					["Cat A (45%)"],
				],
				// Bar B (30%)
				[
					"rect",
					{
						x: "180",
						y: "80",
						width: "120",
						height: "100",
						fill: "#50b87a",
						rx: "4",
						"data-preview-target": "catB",
					},
					[],
				],
				[
					"text",
					{
						x: "240",
						y: "200",
						textAnchor: "middle",
						fontSize: "12",
						fill: "#666",
					},
					["Cat B (30%)"],
				],
				// Bar C (25%)
				[
					"rect",
					{
						x: "320",
						y: "95",
						width: "120",
						height: "85",
						fill: "#e8a838",
						rx: "4",
						"data-preview-target": "catC",
					},
					[],
				],
				[
					"text",
					{
						x: "380",
						y: "200",
						textAnchor: "middle",
						fontSize: "12",
						fill: "#666",
					},
					["Cat C (25%)"],
				],
			],
		],
		links: {
			catA: svgSubA.entityId,
			catB: svgSubB.entityId,
			catC: svgSubC.entityId,
		},
	}).store(scope);

	// ---- Create the main SubReport (for the first report) ----
	const mainSubReport = await SubReport.create({
		label: "Methodology",
		overview: await mdx(
			"We collected data from 500+ organizations across 12 sectors over a 6-month period. Surveys were supplemented with anonymized transaction data.",
		),
		detail: await mdx(
			[
				"## Data Collection",
				"Primary data was gathered through structured interviews with senior leaders at 523 organizations ranging from mid-market to enterprise scale.",
				"",
				"## Analysis Framework",
				"We applied a multi-factor regression model controlling for organization size, sector, and geographic region. Statistical significance was established at p < 0.05 for all reported findings.",
				"",
				"## Limitations",
				"Self-reported survey data may contain response bias. The sample skews toward technology and financial services sectors.",
			].join("\n"),
		),
	}).store(scope);

	// ---- Create Reports ----
	// Create report2 first (no entity links), then report1 (links to report2)

	const report2 = await RootReport.create({
		title: "Q3 2025 Operational Metrics",
		summary: "Operational efficiency metrics and team performance analysis for Q3 2025.",
		contents: await mdx(
			[
				"# Q3 2025 Operational Metrics",
				"",
				"This companion report covers operational efficiency and team performance.",
				"",
				"## Team Performance",
				"",
				"Average team velocity increased 12% quarter-over-quarter, with the platform team showing the largest improvement at 22%.",
				"",
				"## Operational Efficiency",
				"",
				"- Deployment frequency: 4.2x per week (up from 3.1x)",
				"- Mean time to recovery: 45 minutes (down from 72 minutes)",
				"- Change failure rate: 8% (down from 12%)",
				"",
				"## Infrastructure",
				"",
				"Cloud costs were reduced 15% through right-sizing and reserved instance optimization, while supporting 30% more traffic.",
			].join("\n"),
		),
	}).store(scope);

	// Build report1 contents using MDX with entity refs passed as props.
	// RenderFigure/RenderLink are custom components that emit dispatch nodes.
	const report1Contents = await mdx(
		[
			"This report provides a comprehensive analysis of market trends and revenue distribution across our three main categories.",
			"",
			"## Revenue Breakdown",
			"",
			"The following chart shows how revenue is distributed across categories. **Click on a bar** to see detailed analysis for that category.",
			"",
			"<RenderFigure entity={props.svgFigure} />",
			"",
			"## Detailed Methodology",
			"",
			"Our analysis follows a rigorous methodology designed to minimize bias and maximize accuracy.",
			"",
			"<RenderFigure entity={props.subReport} />",
			"",
			"## Key Findings",
			"",
			"1. Overall revenue grew 18% year-over-year",
			"2. Category B showed the strongest growth trajectory",
			"3. Customer retention improved across all segments",
			"",
			'For comparison, see also our <RenderLink entity={props.report2} linkText="companion report" /> which covers operational metrics.',
		].join("\n"),
		{
			components: {
				RenderFigure: passthroughCustomComponent("RenderFigure"),
				RenderLink: passthroughCustomComponent("RenderLink"),
			},
			props: {
				svgFigure: new RawWeakFxtrID(svgFigure.entityId),
				subReport: new RawWeakFxtrID(mainSubReport.entityId),
				report2: new RawWeakFxtrID(report2.entityId),
			},
		},
	);

	const report1 = await RootReport.create({
		title: "Q3 2025 Market Analysis",
		summary:
			"Comprehensive market analysis covering revenue distribution, growth trends, and category deep-dives.",
		contents: report1Contents,
	}).store(scope);

	// ---- Create comments ----
	const comment1 = await SimpleComment.create({
		author: "Sarah Chen",
		comment:
			"The methodology section is very thorough. Can we add confidence intervals to the category breakdowns?",
		target: report1.entityId,
	}).store(scope);

	const comment2 = await SimpleComment.create({
		author: "Marcus Johnson",
		comment: "Great improvements on deployment frequency! The platform team deserves recognition.",
		target: report2.entityId,
	}).store(scope);

	const comment3 = await SimpleComment.create({
		author: "Priya Patel",
		comment:
			"The sample size methodology looks solid. We should consider expanding to include APAC data in the next iteration.",
		target: mainSubReport.entityId,
	}).store(scope);

	// ---- Set up pins ----
	const pins: Record<string, FxtrID> = {
		"reports/market-analysis": report1.entityId,
		"reports/operational-metrics": report2.entityId,
		"comments/1": comment1.entityId,
		"comments/2": comment2.entityId,
		"comments/3": comment3.entityId,
	};

	console.log("Created entities:");
	for (const [name, id] of Object.entries(pins)) {
		console.log(`  ${name} → ${id.idString()}`);
	}

	// ---- Create fxtr HTTP app ----
	const fxtrApp = createFxtrApp(scope, pins);

	// ---- Combined server: fxtr routes + vite frontend ----
	const indexHtmlPath = path.resolve(import.meta.dirname, "../index.html");

	const server = http.createServer((req, res) => {
		const url = req.url ?? "/";

		// Fxtr routes
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

		// Everything else → vite
		vite.middlewares(req, res, async () => {
			try {
				let rawHtml = await fs.readFile(indexHtmlPath, "utf-8");
				if (singleEntity) {
					rawHtml = rawHtml.replace(
						"</head>",
						"<script>window.__DEMO_CONFIG__ = { singleEntity: true };</script></head>",
					);
				}
				const html = await vite.transformIndexHtml(url, rawHtml);
				res.writeHead(200, { "Content-Type": "text/html" });
				res.end(html);
			} catch (err) {
				res.writeHead(500);
				res.end(String(err));
			}
		});
	});

	const port = 3457;
	server.listen(port, () => {
		console.log(`\nDemo report at http://localhost:${port}`);
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
});
