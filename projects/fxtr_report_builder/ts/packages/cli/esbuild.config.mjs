/**
 * Bundle src/main.ts → dist/main.cjs, with @mdx-js/mdx and cbor-x
 * inlined. Output is CommonJS so Python can `node dist/main.cjs`
 * without arranging an ESM-aware setup.
 */

import { build } from "esbuild";

await build({
	entryPoints: ["src/main.ts"],
	outfile: "dist/main.cjs",
	bundle: true,
	platform: "node",
	target: "node20",
	format: "cjs",
	logLevel: "info",
});
