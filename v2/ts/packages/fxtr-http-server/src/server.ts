/**
 * Hono-based HTTP server for serving fxtr entities.
 *
 * Port of Python's asgi_app.py. Provides routes for:
 * - GET /preview/:id_string — Human-readable entity view
 * - GET /data/:id_string — Entity bytes + provenance precomp map (CBOR)
 * - GET /pin/* — Look up a pin by name (CBOR with id + precomp)
 * - GET /pins?prefix=... — List pins matching a prefix (JSON)
 */

import { encode } from "cbor2";
import { Hono } from "hono";
import { cors } from "hono/cors";

import { type FxtrCbor, FxtrID, type FxtrScope, fxtrCborLoads } from "@fxtr/core";
import { FXTR_SCOPE_INTERNALS, type ReadableArena } from "@fxtr/core/internal";

// ---------------------------------------------------------------------------
// Precomp closure
// ---------------------------------------------------------------------------

/**
 * Compute transitively-closed precomp map for a set of seed IDs.
 *
 * For each seed ID (and any IDs discovered as values in precomp relationships),
 * compute unwraps_to, ctx_node, and backstep. The result maps
 * id_string -> {relationship_name: target_id_string}, with empty entries omitted.
 */
function computePrecompClosure(
	arena: ReadableArena,
	seedIds: FxtrID[],
): Record<string, Record<string, string>> {
	const precomp: Record<string, Record<string, string>> = {};
	const frontier: FxtrID[] = [...seedIds];
	const visited = new Set<string>();

	while (frontier.length > 0) {
		// biome-ignore lint/style/noNonNullAssertion: length check above guarantees non-empty
		const nodeId = frontier.shift()!;
		const key = nodeId.key();
		if (visited.has(key)) continue;
		visited.add(key);

		const entry: Record<string, string> = {};
		const tag = nodeId.getTag();

		// Only provenance nodes (c/p/u/n) can be unwrapped
		if (tag === "c" || tag === "p" || tag === "u" || tag === "n") {
			const unwrapsTo = arena.unwrapProvenance(nodeId);
			if (unwrapsTo !== null) {
				entry.unwraps_to = unwrapsTo.idString();
				frontier.push(unwrapsTo);
			}
		}

		// c nodes have ctx_node, p nodes have pathroot (both stored as ctx_node)
		if (tag === "c") {
			const ctx = arena.cNodeToContextData(nodeId);
			entry.ctx_node = ctx.idString();
			frontier.push(ctx);
		} else if (tag === "p") {
			const ctx = arena.pNodeToPathroot(nodeId);
			entry.ctx_node = ctx.idString();
			frontier.push(ctx);

			const backstep = arena.stepBackPCtx(nodeId);
			if (backstep !== null) {
				entry.backstep = backstep.idString();
				frontier.push(backstep);
			}
		}

		if (Object.keys(entry).length > 0) {
			precomp[nodeId.idString()] = entry;
		}
	}

	return precomp;
}

// ---------------------------------------------------------------------------
// Stringify for preview
// ---------------------------------------------------------------------------

/** Simple pretty-print of decoded CBOR for the /preview/ endpoint. */
function prettyPrintCbor(value: FxtrCbor): string {
	return JSON.stringify(
		value,
		(_key, v) => {
			if (v instanceof Uint8Array) {
				return `<bytes:${v.length}>`;
			}
			// FxtrID and tagged ID classes have toString
			if (v != null && typeof v === "object" && typeof v.idString === "function") {
				return v.idString();
			}
			return v;
		},
		2,
	);
}

// ---------------------------------------------------------------------------
// App factory
// ---------------------------------------------------------------------------

/**
 * Create a Hono app for serving fxtr entities over HTTP.
 *
 * @param scope - The FxtrScope to serve entities from. Must be deeply fetchable.
 * @param pins - Pin names to FxtrIDs. Read on each request (can be mutated externally).
 *               Accepts a Map or a plain object.
 */
export function createFxtrApp(
	scope: FxtrScope,
	pins: ReadonlyMap<string, FxtrID> | Readonly<Record<string, FxtrID>>,
): Hono {
	const app = new Hono();
	app.use("*", cors());

	// Helper to read from pins regardless of Map vs Record
	function getPin(name: string): FxtrID | undefined {
		if (pins instanceof Map) {
			return pins.get(name);
		}
		return (pins as Record<string, FxtrID>)[name];
	}

	function allPins(): Iterable<[string, FxtrID]> {
		if (pins instanceof Map) {
			return pins.entries();
		}
		return Object.entries(pins as Record<string, FxtrID>);
	}

	// -- Middleware: scope alive check --
	app.use("*", async (c, next) => {
		if (!scope.isAlive()) {
			return c.text("Scope is closed", 503);
		}
		await next();
	});

	// -- GET /preview/:id_string --
	app.get("/preview/:id_string{.+}", async (c) => {
		const idString = c.req.param("id_string");
		let fxtrId: FxtrID;
		try {
			fxtrId = FxtrID.fromIdString(idString);
		} catch {
			return c.text("Invalid ID", 400);
		}

		const arena = await scope[FXTR_SCOPE_INTERNALS].deepFetch(fxtrId);
		if (arena === null) {
			return c.text("Not found", 404);
		}

		try {
			const flat = await arena.loadFlat(fxtrId);

			if (flat.entityBytes === null) {
				const tag = fxtrId.getTag();
				const depsStr = flat.deps.map(([id, kind]) => `(${id.idString()}, ${kind})`).join(", ");
				return c.text(`Derived node (tag=${tag}, no content)\nDeps: [${depsStr}]`);
			}

			const decoded = fxtrCborLoads(flat.entityBytes);
			return c.text(prettyPrintCbor(decoded));
		} finally {
			arena.close();
		}
	});

	// -- GET /data/:id_string --
	app.get("/data/:id_string{.+}", async (c) => {
		const idString = c.req.param("id_string");
		let fxtrId: FxtrID;
		try {
			fxtrId = FxtrID.fromIdString(idString);
		} catch {
			return c.text("Invalid ID", 400);
		}

		const arena = await scope[FXTR_SCOPE_INTERNALS].deepFetch(fxtrId);
		if (arena === null) {
			return c.text("Not found", 404);
		}

		try {
			const flat = await arena.loadFlat(fxtrId);

			// Compute precomp closure for all deps + the node itself
			const depIds = flat.deps.map(([depId]) => depId);
			depIds.push(fxtrId);
			const precomp = computePrecompClosure(arena, depIds);

			const result: Record<string, unknown> = {};
			if (flat.entityBytes !== null) {
				result.bytes = flat.entityBytes;
			}
			if (Object.keys(precomp).length > 0) {
				result.precomp = precomp;
			}

			const encoded = encode(result).buffer as ArrayBuffer;
			return new Response(encoded, {
				status: 200,
				headers: { "Content-Type": "application/cbor" },
			});
		} finally {
			arena.close();
		}
	});

	// -- GET /pin/* (pin names can contain slashes) --
	app.get("/pin/*", async (c) => {
		const name = c.req.path.slice("/pin/".length);
		const fxtrId = getPin(name);
		if (fxtrId === undefined) {
			return c.text("Pin not found", 404);
		}

		// Compute precomp for the pinned ID itself
		const arena = await scope[FXTR_SCOPE_INTERNALS].deepFetch(fxtrId);
		let precomp: Record<string, Record<string, string>> = {};
		if (arena !== null) {
			try {
				precomp = computePrecompClosure(arena, [fxtrId]);
			} finally {
				arena.close();
			}
		}

		const result: Record<string, unknown> = { id: fxtrId.idString() };
		if (Object.keys(precomp).length > 0) {
			result.precomp = precomp;
		}

		const encoded = encode(result).buffer as ArrayBuffer;
		return new Response(encoded, {
			status: 200,
			headers: { "Content-Type": "application/cbor" },
		});
	});

	// -- GET /pins?prefix=... --
	app.get("/pins", (c) => {
		const prefix = c.req.query("prefix") ?? "";
		const filtered: Record<string, string> = {};
		for (const [name, fid] of allPins()) {
			if (name.startsWith(prefix)) {
				filtered[name] = fid.idString();
			}
		}
		return c.json(filtered);
	});

	return app;
}
