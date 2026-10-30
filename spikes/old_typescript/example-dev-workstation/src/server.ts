/**
 * Dev demo server.
 *
 * - Bootstraps a small fxtr scope with two DemoNote entities and one
 *   DemoSection linking them.
 * - Pins:
 *     demo/section    → the DemoSection
 *     demo/first-note → the first DemoNote
 * - Serves fxtr HTTP routes (/pin, /pins, /data, /preview) plus the vite
 *   dev server middleware for the client at http://localhost:3460.
 *
 * Mirrors example-demo-fingerprinting-report/server.ts pattern (raw
 * node:http delegating to Hono for fxtr routes and vite middleware for
 * the HTML/client).
 */

import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { type EntityRef, type FxtrID, GLOBAL_SCOPE, withFxtrScope } from "@fxtr/core";
import { createFxtrApp } from "@fxtr/http-server";
import { createServer as createViteServer } from "vite";
import { DemoArchive, DemoListNode, DemoNote, DemoSection } from "./entities.js";

const PORT = 3460;

await withFxtrScope({ name: "workstation_dev_demo", include: [GLOBAL_SCOPE] }, async (scope) => {
	const vite = await createViteServer({
		server: { middlewareMode: true },
		appType: "custom",
	});

	// ---- Create entities ----
	const noteA = await DemoNote.create({
		title: "Welcome note",
		body: "This is the first demo note. DemoNote has a registered FullPanel handler on the client.",
	}).store(scope);

	const noteB = await DemoNote.create({
		title: "Second note",
		body: "DemoNote also has a registered BlockLink and HoverPreview. The InlineLink slot intentionally falls back to the workstation-core default — so inline references to notes render via the fallback.",
	}).store(scope);

	const section = await DemoSection.create({
		title: "Demo section",
		summary:
			"DemoSection has a registered FullPanel handler that lists its items as interactive EntityLinks — try hovering, clicking, dragging, and right-clicking.",
		items: [noteA, noteB],
	}).store(scope);

	const archive = await DemoArchive.create({
		name: "Demo archive",
		sections: [section],
	}).store(scope);

	// 20-node linked list: build from the tail (Node 20, next=null) so
	// each earlier node can reference the stored id of its successor.
	const CHAIN_LENGTH = 20;
	let prev: EntityRef<DemoListNode> | null = null;
	for (let i = CHAIN_LENGTH; i >= 1; i -= 1) {
		const node: EntityRef<DemoListNode> = await DemoListNode.create({
			label: `Node ${i}`,
			next: prev,
		}).store(scope);
		prev = node;
	}
	if (!prev) throw new Error("failed to seed chain head");
	const chainHead = prev;

	const pins: Record<string, FxtrID> = {
		"demo/section": section.entityId,
		"demo/first-note": noteA.entityId,
		"demo/archive": archive.entityId,
		"demo/chain-head": chainHead.entityId,
	};

	console.log("Created entities:");
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

	server.listen(PORT, () => {
		console.log(`\nDev demo at http://localhost:${PORT}`);
		console.log(`  Pins: http://localhost:${PORT}/pins`);
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
