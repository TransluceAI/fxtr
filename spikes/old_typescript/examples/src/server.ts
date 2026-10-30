/**
 * Example: fxtr HTTP server
 *
 * Creates a few linked entities and serves them over HTTP.
 *
 * Usage:
 *   pnpm --filter @fxtr/examples server
 *
 * Then try:
 *   curl http://localhost:3000/pins
 *   curl http://localhost:3000/preview/<id>
 *
 * Or run the client example against it:
 *   pnpm --filter @fxtr/examples client -- http://localhost:3000
 */

import { GLOBAL_SCOPE, withFxtrScope } from "@fxtr/core";
import { createFxtrApp } from "@fxtr/http-server";
import { serve } from "@hono/node-server";
import { Article, Author, Tag } from "./entities.js";

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const PORT = 3000;

await withFxtrScope({ name: "example_server", include: [GLOBAL_SCOPE] }, async (scope) => {
	// Create some entities
	const alice = await scope.store(Author.create({ name: "Alice", email: "alice@example.com" }));
	const bob = await scope.store(Author.create({ name: "Bob", email: "bob@example.com" }));

	const rustTag = await scope.store(Tag.create({ label: "rust" }));
	const tsTag = await scope.store(Tag.create({ label: "typescript" }));
	const fxtrTag = await scope.store(Tag.create({ label: "fxtr" }));

	const article1 = await scope.store(
		Article.create({
			title: "Getting Started with Fxtr",
			body: "Fxtr is a content-addressed entity system...",
			author: alice,
			tags: [fxtrTag, tsTag],
		}),
	);

	const article2 = await scope.store(
		Article.create({
			title: "Cross-Language Interop",
			body: "Fxtr entities can be shared between Python and TypeScript...",
			author: bob,
			tags: [fxtrTag, rustTag],
		}),
	);

	// Set up pins
	const pins: Record<string, import("@fxtr/core").FxtrID> = {
		"authors/alice": alice.entityId,
		"authors/bob": bob.entityId,
		"articles/getting-started": article1.entityId,
		"articles/cross-language": article2.entityId,
	};

	// Create and serve the Hono app
	const app = createFxtrApp(scope, pins);

	console.log(`Fxtr server listening on http://localhost:${PORT}`);
	console.log("Pins:");
	for (const [name, id] of Object.entries(pins)) {
		console.log(`  ${name} → ${id.idString()}`);
	}

	// Start the HTTP server and keep the scope alive until the process is killed
	serve({ fetch: app.fetch, port: PORT });
	await new Promise<void>((resolve) => {
		process.on("SIGINT", () => {
			console.log("\nShutting down...");
			resolve();
		});
		process.on("SIGTERM", resolve);
	});
});
