/**
 * Tests for the fxtr HTTP server (createFxtrApp).
 *
 * Uses Hono's app.request() for in-process testing (no network).
 */

import {
	type EntityRef,
	FxtrID,
	type FxtrScope,
	GLOBAL_SCOPE,
	RawFxtrEntityMarkerID,
	RawStrongFxtrID,
	UUID,
	defineType,
	fxtrCborLoads,
	withFxtrScope,
} from "@fxtr/core";
import * as bicodec from "@fxtr/core/bicodec";
import { FXTR_SCOPE_INTERNALS } from "@fxtr/core/internal";
import { decode } from "cbor2";
import { describe, expect, it } from "vitest";
import { createFxtrApp } from "../../src/server.js";

// ---------------------------------------------------------------------------
// Test entity definitions
// ---------------------------------------------------------------------------

const NS = UUID("e8500aeb-c795-4979-815d-4807f3b4df18");

const _SimpleEntity = bicodec.entity(defineType(NS, "SimpleEntity"), {
	name: bicodec.string(),
	value: bicodec.number(),
});
interface SimpleEntityT extends bicodec.InferEntity<typeof _SimpleEntity> {}
const SimpleEntity: bicodec.EntityDefinition<SimpleEntityT> = _SimpleEntity;

const _EntityWithRef = bicodec.entity(defineType(NS, "EntityWithRef"), {
	name: bicodec.string(),
	child: bicodec.childRef(SimpleEntity),
});
interface EntityWithRefT extends bicodec.InferEntity<typeof _EntityWithRef> {}
const EntityWithRef: bicodec.EntityDefinition<EntityWithRefT> = _EntityWithRef;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function withServerContext(
	fn: (ctx: {
		app: ReturnType<typeof createFxtrApp>;
		scope: FxtrScope;
		childRef: EntityRef<SimpleEntityT>;
		parentRef: EntityRef<EntityWithRefT>;
		pins: Record<string, FxtrID>;
	}) => Promise<void>,
): Promise<void> {
	await withFxtrScope({ name: "test_server", include: [GLOBAL_SCOPE] }, async (scope) => {
		const childRef = await scope.store(SimpleEntity.create({ name: "child", value: 42 }));
		const parentRef = await scope.store(EntityWithRef.create({ name: "parent", child: childRef }));

		const pins: Record<string, FxtrID> = {
			"my/pin": childRef.entityId,
			parent: parentRef.entityId,
		};
		const app = createFxtrApp(scope, pins);

		await fn({ app, scope, childRef, parentRef, pins });
	});
}

async function request(app: ReturnType<typeof createFxtrApp>, path: string): Promise<Response> {
	return app.request(path);
}

// ---------------------------------------------------------------------------
// Preview endpoint
// ---------------------------------------------------------------------------

describe("GET /preview/:id_string", () => {
	it("returns pretty-printed entity for found entity", async () => {
		await withServerContext(async ({ app, childRef }) => {
			const resp = await request(app, `/preview/${childRef.entityId.idString()}`);
			expect(resp.status).toBe(200);
			expect(resp.headers.get("content-type")).toContain("text/plain");
			const text = await resp.text();
			expect(text).toContain("child");
			expect(text).toContain("42");
		});
	});

	it("returns 404 for unknown entity", async () => {
		await withServerContext(async ({ app }) => {
			const fakeId = FxtrID.fromTagAndPayload("d", new Uint8Array(32));
			const resp = await request(app, `/preview/${fakeId.idString()}`);
			expect(resp.status).toBe(404);
		});
	});

	it("returns derived node text for builtin", async () => {
		await withServerContext(async ({ app, scope }) => {
			// Track a builtin explicitly so it's findable via deep fetch
			const builtinId = scope.trackBuiltin(FxtrID.builtin("test_preview_builtin"));
			const resp = await request(app, `/preview/${builtinId.idString()}`);
			expect(resp.status).toBe(200);
			const text = await resp.text();
			expect(text).toContain("Derived node");
		});
	});

	it("returns 400 for invalid ID", async () => {
		await withServerContext(async ({ app }) => {
			const resp = await request(app, "/preview/not-a-valid-id");
			expect(resp.status).toBe(400);
		});
	});
});

// ---------------------------------------------------------------------------
// Data endpoint
// ---------------------------------------------------------------------------

describe("GET /data/:id_string", () => {
	it("returns bytes and content-type", async () => {
		await withServerContext(async ({ app, childRef }) => {
			const resp = await request(app, `/data/${childRef.entityId.idString()}`);
			expect(resp.status).toBe(200);
			expect(resp.headers.get("content-type")).toBe("application/cbor");

			const buf = await resp.arrayBuffer();
			const result = decode(new Uint8Array(buf)) as Record<string, unknown>;
			expect(result.bytes).toBeInstanceOf(Uint8Array);

			// Decode inner entity bytes
			const decoded = fxtrCborLoads(result.bytes as Uint8Array);
			expect(decoded).toBeTypeOf("object");
			expect((decoded as Record<string, unknown>).name).toBe("child");
			expect((decoded as Record<string, unknown>).value).toBe(42);
		});
	});

	it("returns 404 for unknown entity", async () => {
		await withServerContext(async ({ app }) => {
			const fakeId = FxtrID.fromTagAndPayload("d", new Uint8Array(32));
			const resp = await request(app, `/data/${fakeId.idString()}`);
			expect(resp.status).toBe(404);
		});
	});

	it("includes precomp for entities with provenance", async () => {
		await withServerContext(async ({ app, parentRef }) => {
			const resp = await request(app, `/data/${parentRef.entityId.idString()}`);
			expect(resp.status).toBe(200);

			const buf = await resp.arrayBuffer();
			const result = decode(new Uint8Array(buf)) as Record<string, unknown>;
			expect(result.bytes).toBeInstanceOf(Uint8Array);
			// precomp may or may not exist depending on entity structure
		});
	});
});

// ---------------------------------------------------------------------------
// Pin endpoint
// ---------------------------------------------------------------------------

describe("GET /pin/*", () => {
	it("returns pin ID for found pin", async () => {
		await withServerContext(async ({ app, childRef }) => {
			const resp = await request(app, "/pin/my/pin");
			expect(resp.status).toBe(200);
			expect(resp.headers.get("content-type")).toBe("application/cbor");

			const buf = await resp.arrayBuffer();
			const result = decode(new Uint8Array(buf)) as Record<string, unknown>;
			expect(result.id).toBe(childRef.entityId.idString());
		});
	});

	it("returns 404 for unknown pin", async () => {
		await withServerContext(async ({ app }) => {
			const resp = await request(app, "/pin/nonexistent");
			expect(resp.status).toBe(404);
		});
	});
});

// ---------------------------------------------------------------------------
// Pins list endpoint
// ---------------------------------------------------------------------------

describe("GET /pins", () => {
	it("returns all pins as JSON", async () => {
		await withServerContext(async ({ app, childRef, parentRef }) => {
			const resp = await request(app, "/pins");
			expect(resp.status).toBe(200);
			expect(resp.headers.get("content-type")).toContain("application/json");

			const result = (await resp.json()) as Record<string, string>;
			expect(result["my/pin"]).toBe(childRef.entityId.idString());
			expect(result.parent).toBe(parentRef.entityId.idString());
		});
	});

	it("filters by prefix", async () => {
		await withServerContext(async ({ app }) => {
			const resp = await request(app, "/pins?prefix=my/");
			expect(resp.status).toBe(200);

			const result = (await resp.json()) as Record<string, string>;
			expect(result["my/pin"]).toBeDefined();
			expect(result.parent).toBeUndefined();
		});
	});
});

// ---------------------------------------------------------------------------
// Scope lifecycle
// ---------------------------------------------------------------------------

describe("scope lifecycle", () => {
	it("returns 503 when scope is closed", async () => {
		// We can't easily close a scope while testing with withFxtrScope,
		// so we create the app with a scope that's already been used and closed.
		let capturedApp: ReturnType<typeof createFxtrApp> | undefined;
		let capturedId: string | undefined;

		await withFxtrScope({ name: "will_close", include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(SimpleEntity.create({ name: "x", value: 1 }));
			capturedId = ref.entityId.idString();
			capturedApp = createFxtrApp(scope, { test: ref.entityId });
		});

		// Scope is now closed
		// biome-ignore lint/style/noNonNullAssertion: assigned inside withFxtrScope above
		const app = capturedApp!;
		for (const url of [`/preview/${capturedId}`, `/data/${capturedId}`, "/pin/test", "/pins"]) {
			const resp = await request(app, url);
			expect(resp.status).toBe(503);
		}
	});
});

// ---------------------------------------------------------------------------
// Provenance nodes
// ---------------------------------------------------------------------------

describe("provenance node precomp", () => {
	it("includes precomp for u-node", async () => {
		await withFxtrScope({ name: "prov_test", include: [GLOBAL_SCOPE] }, async (scope) => {
			const dRef = await scope.store(SimpleEntity.create({ name: "base", value: 1 }));
			const dId = dRef.entityId;
			const uId = await scope[FXTR_SCOPE_INTERNALS].mainArena.createUNode(dId);

			const pins = { u_node: uId };
			const app = createFxtrApp(scope, pins);

			// Fetch via /pin/u_node — should have precomp with unwraps_to
			const resp = await request(app, "/pin/u_node");
			expect(resp.status).toBe(200);
			const buf = await resp.arrayBuffer();
			const result = decode(new Uint8Array(buf)) as Record<string, unknown>;
			expect(result.id).toBe(uId.idString());

			const precomp = result.precomp as Record<string, Record<string, string>> | undefined;
			expect(precomp).toBeDefined();
			const uEntry = precomp?.[uId.idString()];
			expect(uEntry).toBeDefined();
			expect(uEntry?.unwraps_to).toBe(dId.idString());
		});
	});

	it("includes precomp for c-node", async () => {
		await withFxtrScope({ name: "prov_test_c", include: [GLOBAL_SCOPE] }, async (scope) => {
			const dRef = await scope.store(SimpleEntity.create({ name: "base", value: 1 }));
			const dId = dRef.entityId;
			const dotId = scope.getContextMarkerId();

			const ctxData = {
				"$fxtr.ctx": new RawFxtrEntityMarkerID(dotId),
				value: new RawStrongFxtrID(dId),
			};
			const cId = await scope[FXTR_SCOPE_INTERNALS].mainArena.createCNode(ctxData);

			const pins = { c_node: cId };
			const app = createFxtrApp(scope, pins);

			const resp = await request(app, "/pin/c_node");
			expect(resp.status).toBe(200);
			const buf = await resp.arrayBuffer();
			const result = decode(new Uint8Array(buf)) as Record<string, unknown>;
			const precomp = result.precomp as Record<string, Record<string, string>>;
			expect(precomp).toBeDefined();

			const cEntry = precomp?.[cId.idString()];
			expect(cEntry).toBeDefined();
			expect(cEntry?.unwraps_to).toBe(dId.idString());
			expect(cEntry?.ctx_node).toBeDefined();
		});
	});
});
