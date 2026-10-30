/**
 * Example: fxtr HTTP client
 *
 * Connects to a running fxtr server and queries entities.
 *
 * Usage:
 *   pnpm --filter @fxtr/examples client -- http://localhost:3000
 *
 * Start the server first:
 *   pnpm --filter @fxtr/examples server
 */

import type { FxtrID } from "@fxtr/core";
import { withBaseUrl, withHttpReadNamespace } from "@fxtr/http-client";
import { Article } from "./entities.js";

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const args = process.argv.slice(2).filter((a) => a !== "--");
const baseUrl = args[0];
if (!baseUrl) {
	console.error("Usage: pnpm --filter @fxtr/examples client -- <server-url>");
	console.error("Example: pnpm --filter @fxtr/examples client -- http://localhost:3000");
	process.exit(1);
}

const fetchFn = withBaseUrl(baseUrl);

await withHttpReadNamespace(fetchFn, async (ns) => {
	// List all pins
	console.log("--- All pins ---");
	const allPins = await ns.listPins();
	for (const [name, id] of allPins) {
		console.log(`  ${name} → ${id.idString()}`);
	}

	// List just article pins
	console.log("\n--- Article pins ---");
	const articlePins = await ns.listPins("articles/");
	for (const [name] of articlePins) {
		console.log(`  ${name}`);
	}

	// Retrieve an article by pin and load it
	console.log("\n--- Loading 'articles/getting-started' ---");
	const articleRef = await ns.retrieve("articles/getting-started", Article);
	const article = await ns.scope.load(articleRef);
	console.log(`  Title: ${article.title}`);
	console.log(`  Body: ${article.body}`);

	// Load the linked author (automatically fetched via deep fetch)
	const author = await ns.scope.load(article.author);
	console.log(`  Author: ${author.name} <${author.email}>`);

	// Load the tags
	const tags: string[] = [];
	for (const tagRef of article.tags) {
		const tag = await ns.scope.load(tagRef);
		tags.push(tag.label);
	}
	console.log(`  Tags: ${tags.join(", ")}`);

	// Retrieve the second article
	console.log("\n--- Loading 'articles/cross-language' ---");
	const article2Ref = await ns.retrieve("articles/cross-language", Article);
	const article2 = await ns.scope.load(article2Ref);
	console.log(`  Title: ${article2.title}`);

	const author2 = await ns.scope.load(article2.author);
	console.log(`  Author: ${author2.name} <${author2.email}>`);

	const tags2: string[] = [];
	for (const tagRef of article2.tags) {
		const tag = await ns.scope.load(tagRef);
		tags2.push(tag.label);
	}
	console.log(`  Tags: ${tags2.join(", ")}`);

	// Retrieve an author directly
	console.log("\n--- Loading 'authors/alice' directly ---");
	const aliceId = (await ns.retrieve("authors/alice")) as FxtrID;
	console.log(`  ID: ${aliceId.idString()}`);
});
