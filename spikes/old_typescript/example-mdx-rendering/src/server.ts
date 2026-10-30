/**
 * Example server: MDX → FxtrCbor → fxtr entity serving.
 *
 * On startup, renders MDX pages into ComponentTree entities and serves them
 * via the fxtr HTTP server. The frontend loads entities via the fxtr HTTP client.
 *
 * Fxtr routes (via createFxtrApp):
 *   GET /pin/*       — load entity by pin name
 *   GET /data/:id    — load entity by ID
 *   GET /preview/:id — human-readable entity preview
 *   GET /pins        — list all pins
 *
 * Frontend (via vite):
 *   Everything else is served by vite (index.html, client assets, HMR).
 */

import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { type FxtrID, GLOBAL_SCOPE, withFxtrScope } from "@fxtr/core";
import { createFxtrApp } from "@fxtr/http-server";
import { type MdxContentFn, evaluateMdx, renderMdxToFxtr } from "@fxtr/mdx-transform";
import { createServer as createViteServer } from "vite";
import { getComponents } from "./components.js";
import { ComponentTree } from "./entities.js";

const components = getComponents();

await withFxtrScope({ name: "mdx_example_server", include: [GLOBAL_SCOPE] }, async (scope) => {
	// ---- Vite dev server (middleware mode) ----
	const vite = await createViteServer({
		server: { middlewareMode: true },
		appType: "custom",
	});

	// ---- Render MDX pages into ComponentTree entities ----

	async function renderPage(mdxPath: string): Promise<FxtrID> {
		const mod = await vite.ssrLoadModule(mdxPath);
		const Content = mod.default as MdxContentFn;
		const document = await renderMdxToFxtr(Content, {
			components,
			props: { fxtr_scope: scope },
		});
		const ref = await scope.store(ComponentTree.create({ document }));
		return ref.entityId;
	}

	async function renderDynamicPage(mdxPath: string): Promise<FxtrID> {
		const source = await fs.readFile(mdxPath, "utf-8");
		const document = await evaluateMdx(source, { components, props: { fxtr_scope: scope } });
		const ref = await scope.store(ComponentTree.create({ document }));
		return ref.entityId;
	}

	// Render all pages and create pins
	const pins: Record<string, FxtrID> = {};

	const pagesDir = path.resolve(import.meta.dirname, "../pages");
	const contentDir = path.resolve(import.meta.dirname, "../content");

	pins["pages/report"] = await renderPage(path.join(pagesDir, "report.mdx"));
	pins["pages/dynamic-example"] = await renderDynamicPage(
		path.join(contentDir, "dynamic-example.mdx"),
	);

	console.log("Rendered pages:");
	for (const [name, id] of Object.entries(pins)) {
		console.log(`  ${name} → ${id.idString()}`);
	}

	// ---- Create fxtr HTTP app ----
	const fxtrApp = createFxtrApp(scope, pins);

	// ---- Combined server: fxtr routes + vite frontend ----
	const indexHtmlPath = path.resolve(import.meta.dirname, "../index.html");

	const server = http.createServer((req, res) => {
		const url = req.url ?? "/";

		// Fxtr routes: /pin/, /data/, /preview/, /pins
		if (
			url.startsWith("/pin/") ||
			url.startsWith("/data/") ||
			url.startsWith("/preview/") ||
			url === "/pins" ||
			url.startsWith("/pins?")
		) {
			Promise.resolve(
				fxtrApp.fetch(new Request(`http://localhost${url}`, { method: req.method ?? "GET" })),
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

		// Everything else → vite (frontend assets, index.html, HMR)
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

	const port = 3456;
	server.listen(port, () => {
		console.log(`\nMDX rendering example at http://localhost:${port}`);
		console.log(`  Frontend:  http://localhost:${port}/`);
		console.log(`  Pins:      http://localhost:${port}/pins`);
		console.log(`  Preview:   http://localhost:${port}/preview/<id>`);
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
