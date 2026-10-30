/**
 * Client-only server: serves the Vite frontend, fetching entity data from
 * an external fxtr HTTP server.
 *
 * Two modes:
 *   Direct:  tsx src/client-server.ts <url>
 *            Client fetches directly from the given URL (sets __FXTR_API_BASE_URL__).
 *
 *   Proxy:   tsx src/client-server.ts --proxy <url>
 *            Server proxies /pin/, /data/, /preview/, /pins to the given URL.
 *            Client uses same-origin fetch (no CORS, no base URL injection).
 *
 * Example:
 *   tsx src/client-server.ts http://localhost:3458
 *   tsx src/client-server.ts --proxy http://localhost:3458
 */

import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { createServer as createViteServer } from "vite";

const proxyMode = process.argv[2] === "--proxy";
const apiBaseUrl = proxyMode ? process.argv[3] : process.argv[2];

if (!apiBaseUrl) {
	console.error("Usage: tsx src/client-server.ts [--proxy] <api-base-url>");
	console.error("Example: tsx src/client-server.ts --proxy http://localhost:3458");
	process.exit(1);
}

const normalizedBaseUrl = apiBaseUrl.replace(/\/+$/, "");

const vite = await createViteServer({
	server: { middlewareMode: true },
	appType: "custom",
});

const indexHtmlPath = path.resolve(import.meta.dirname, "../index.html");
const port = 3459;

function isFxtrRoute(url: string): boolean {
	return (
		url.startsWith("/pin/") ||
		url.startsWith("/data/") ||
		url.startsWith("/preview/") ||
		url === "/pins" ||
		url.startsWith("/pins?")
	);
}

async function proxyRequest(url: string, res: http.ServerResponse) {
	try {
		const upstream = await fetch(`${normalizedBaseUrl}${url}`);
		const headers: Record<string, string> = {};
		upstream.headers.forEach((v, k) => {
			headers[k] = v;
		});
		res.writeHead(upstream.status, headers);
		const body = await upstream.arrayBuffer();
		res.end(Buffer.from(body));
	} catch (err) {
		res.writeHead(502);
		res.end(`Proxy error: ${err}`);
	}
}

const server = http.createServer((req, res) => {
	const url = req.url ?? "/";

	// In proxy mode, intercept fxtr routes and forward upstream
	if (proxyMode && isFxtrRoute(url)) {
		proxyRequest(url, res);
		return;
	}

	vite.middlewares(req, res, async () => {
		try {
			const rawHtml = await fs.readFile(indexHtmlPath, "utf-8");
			const transformed = await vite.transformIndexHtml(url, rawHtml);
			// In direct mode, inject the base URL so the client fetches from it.
			// In proxy mode, no injection needed — client uses same-origin.
			const html = proxyMode
				? transformed
				: transformed.replace(
						"</head>",
						`<script>window.__FXTR_API_BASE_URL__ = ${JSON.stringify(normalizedBaseUrl)};</script>\n</head>`,
					);
			res.writeHead(200, { "Content-Type": "text/html" });
			res.end(html);
		} catch (err) {
			res.writeHead(500);
			res.end(String(err));
		}
	});
});

server.listen(port, () => {
	console.log(`Fingerprint report client at http://localhost:${port}`);
	console.log(`  Mode: ${proxyMode ? "proxy" : "direct"}`);
	console.log(`  API: ${normalizedBaseUrl}`);
});

process.on("SIGINT", () => {
	server.close();
	process.exit(0);
});
process.on("SIGTERM", () => {
	server.close();
	process.exit(0);
});
