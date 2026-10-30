/**
 * Tests for the HTTP client: HttpReadableArena, HttpDeepFetchable,
 * HttpFxtrReadNamespace, withHttpReadNamespace.
 *
 * All tests use Hono's app.request() via a FetchFn adapter — no network.
 */

import {
	type EntityRef,
	FxtrID,
	type FxtrScope,
	GLOBAL_SCOPE,
	RawFxtrEntityMarkerID,
	RawStrongFxtrID,
	RawWeakFxtrID,
	UUID,
	defineType,
	withFxtrScope,
} from "@fxtr/core";
import * as bicodec from "@fxtr/core/bicodec";
import { FXTR_SCOPE_INTERNALS } from "@fxtr/core/internal";
import { createFxtrApp } from "@fxtr/http-server";
import type { Hono } from "hono";
import { describe, expect, it } from "vitest";
import {
	type FetchFn,
	HttpDeepFetchable,
	HttpReadableArena,
	withBaseUrl,
	withHttpReadNamespace,
} from "../../src/client.js";

// ---------------------------------------------------------------------------
// Test entity definitions
// ---------------------------------------------------------------------------

const NS = UUID("48eed5d3-8fdc-444e-8100-ae3caa392997");

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
// Test helper: create a FetchFn backed by a Hono app
// ---------------------------------------------------------------------------

function appFetch(app: Hono): FetchFn {
	return async (url: string, init?: RequestInit) => {
		// Hono's app.request expects a full URL or path
		const fullUrl = url.startsWith("http")
			? url
			: `http://test${url.startsWith("/") ? "" : "/"}${url}`;
		return app.request(fullUrl, init);
	};
}

// ---------------------------------------------------------------------------
// Fixture helper
// ---------------------------------------------------------------------------

interface ServerFixture {
	app: Hono;
	fetchFn: FetchFn;
	scope: FxtrScope;
	childRef: EntityRef<SimpleEntityT>;
	parentRef: EntityRef<EntityWithRefT>;
}

async function withServer(fn: (ctx: ServerFixture) => Promise<void>): Promise<void> {
	await withFxtrScope({ name: "test_server", include: [GLOBAL_SCOPE] }, async (scope) => {
		const childRef = await scope.store(SimpleEntity.create({ name: "child", value: 42 }));
		const parentRef = await scope.store(EntityWithRef.create({ name: "parent", child: childRef }));

		const pins: Record<string, FxtrID> = {
			"my/pin": childRef.entityId,
			parent: parentRef.entityId,
		};
		const app = createFxtrApp(scope, pins);
		const fetchFn = appFetch(app);

		await fn({ app, fetchFn, scope, childRef, parentRef });
	});
}

// ---------------------------------------------------------------------------
// HttpReadableArena
// ---------------------------------------------------------------------------

describe("HttpReadableArena", () => {
	it("load returns decoded CBOR", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			const arena = new HttpReadableArena(fetchFn);
			arena._tracked.set(childRef.entityId.key(), childRef.entityId);

			const cbor = await arena.load(childRef.entityId);
			expect(cbor).toBeTypeOf("object");
			expect((cbor as Record<string, unknown>).name).toBe("child");
			expect((cbor as Record<string, unknown>).value).toBe(42);
			arena.close();
		});
	});

	it("load tracks child IDs discovered in CBOR", async () => {
		await withServer(async ({ fetchFn, childRef, parentRef }) => {
			const arena = new HttpReadableArena(fetchFn);
			arena._tracked.set(parentRef.entityId.key(), parentRef.entityId);

			await arena.load(parentRef.entityId);
			// The child's ID should now be tracked
			expect(arena.isTracked(childRef.entityId)).toBe(true);
			arena.close();
		});
	});

	it("loadFlat returns entity bytes and deps", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			const arena = new HttpReadableArena(fetchFn);
			arena._tracked.set(childRef.entityId.key(), childRef.entityId);

			const flat = await arena.loadFlat(childRef.entityId);
			expect(flat.entityId.key()).toBe(childRef.entityId.key());
			expect(flat.entityBytes).toBeInstanceOf(Uint8Array);
			expect(flat.deps.length).toBeGreaterThanOrEqual(0);
			arena.close();
		});
	});

	it("load populates precomp from server response", async () => {
		await withServer(async ({ fetchFn, childRef, parentRef }) => {
			const arena = new HttpReadableArena(fetchFn);
			arena._tracked.set(parentRef.entityId.key(), parentRef.entityId);

			await arena.load(parentRef.entityId);
			// Child discovered from CBOR walk
			expect(arena.isTracked(childRef.entityId)).toBe(true);
			arena.close();
		});
	});

	it("abandon removes from tracking", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			const arena = new HttpReadableArena(fetchFn);
			arena._tracked.set(childRef.entityId.key(), childRef.entityId);

			expect(arena.isTracked(childRef.entityId)).toBe(true);
			arena.abandon(childRef.entityId);
			expect(arena.isTracked(childRef.entityId)).toBe(false);
			arena.close();
		});
	});

	it("copyToNewArena copies tracked + precomp data", async () => {
		await withServer(async ({ fetchFn, childRef, parentRef }) => {
			const arena = new HttpReadableArena(fetchFn);
			arena._tracked.set(childRef.entityId.key(), childRef.entityId);

			const newArena = arena.copyToNewArena([childRef.entityId]);
			expect(newArena.isTracked(childRef.entityId)).toBe(true);
			expect(newArena.isTracked(parentRef.entityId)).toBe(false);
			arena.close();
			newArena.close();
		});
	});

	it("close clears all state", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			const arena = new HttpReadableArena(fetchFn);
			arena._tracked.set(childRef.entityId.key(), childRef.entityId);

			arena.close();
			expect(arena.isTracked(childRef.entityId)).toBe(false);
		});
	});

	it("load throws for untracked node", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			const arena = new HttpReadableArena(fetchFn);
			await expect(arena.load(childRef.entityId)).rejects.toThrow("not tracked");
		});
	});
});

// ---------------------------------------------------------------------------
// HttpDeepFetchable
// ---------------------------------------------------------------------------

describe("HttpDeepFetchable", () => {
	it("tryLookup returns arena for found entity", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			const df = new HttpDeepFetchable(fetchFn);
			const arena = await df.tryLookup(childRef.entityId);
			expect(arena).not.toBeNull();
			expect(arena?.isTracked(childRef.entityId)).toBe(true);
			arena?.close();
		});
	});

	it("tryLookup returns null for unknown entity", async () => {
		await withServer(async ({ fetchFn }) => {
			const df = new HttpDeepFetchable(fetchFn);
			const fakeId = FxtrID.fromTagAndPayload("d", new Uint8Array(32));
			const arena = await df.tryLookup(fakeId);
			expect(arena).toBeNull();
		});
	});

	it("returned arena has precomp populated", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			const df = new HttpDeepFetchable(fetchFn);
			const arena = await df.tryLookup(childRef.entityId);
			expect(arena).not.toBeNull();
			// Arena should at least track the entity
			expect(arena?.isTracked(childRef.entityId)).toBe(true);
			arena?.close();
		});
	});
});

// ---------------------------------------------------------------------------
// withHttpReadNamespace + HttpFxtrReadNamespace
// ---------------------------------------------------------------------------

describe("withHttpReadNamespace", () => {
	it("retrieve returns FxtrID when no codec given", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const id = await ns.retrieve("my/pin");
				expect(id.key()).toBe(childRef.entityId.key());
			});
		});
	});

	it("retrieve returns EntityRef when codec given", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const ref = await ns.retrieve("my/pin", SimpleEntity);
				expect(ref.entityId.key()).toBe(childRef.entityId.key());

				// Load the entity and verify round-trip
				const loaded = await ref.load(ns.scope);
				expect(loaded.name).toBe("child");
				expect(loaded.value).toBe(42);
			});
		});
	});

	it("tryRetrieve returns null for unknown pin", async () => {
		await withServer(async ({ fetchFn }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const result = await ns.tryRetrieve("nonexistent");
				expect(result).toBeNull();
			});
		});
	});

	it("retrieve throws for unknown pin", async () => {
		await withServer(async ({ fetchFn }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				await expect(ns.retrieve("nonexistent")).rejects.toThrow("not found");
			});
		});
	});

	it("listPins returns all pins", async () => {
		await withServer(async ({ fetchFn, childRef, parentRef }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const pins = await ns.listPins();
				expect(pins.get("my/pin")?.key()).toBe(childRef.entityId.key());
				expect(pins.get("parent")?.key()).toBe(parentRef.entityId.key());
			});
		});
	});

	it("listPins filters by prefix", async () => {
		await withServer(async ({ fetchFn }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const pins = await ns.listPins("my/");
				expect(pins.has("my/pin")).toBe(true);
				expect(pins.has("parent")).toBe(false);
			});
		});
	});

	it("scope property returns the internal scope", async () => {
		await withServer(async ({ fetchFn }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				expect(ns.scope).toBeDefined();
				expect(ns.scope.isAlive()).toBe(true);
			});
		});
	});

	it("retrieve with intoScope + copyIntoScope copies entity", async () => {
		await withServer(async ({ fetchFn, childRef }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				// Create a child scope to copy into
				await withFxtrScope({ name: "target", include: [GLOBAL_SCOPE] }, async (targetScope) => {
					const id = await ns.retrieve("my/pin", {
						intoScope: targetScope,
						copyIntoScope: true,
					});
					expect(id.key()).toBe(childRef.entityId.key());
					expect(targetScope.isTracked(id)).toBe(true);
				});
			});
		});
	});
});

// ---------------------------------------------------------------------------
// Provenance via read namespace
// ---------------------------------------------------------------------------

describe("provenance via HttpFxtrReadNamespace", () => {
	// Provenance fixture: creates d, u, c, p nodes and a holder d-node referencing them.
	// Matches the Python provenance_server fixture.
	async function withProvenanceServer(
		fn: (ctx: {
			fetchFn: FetchFn;
			dId: FxtrID;
			uId: FxtrID;
			cId: FxtrID;
			pId: FxtrID;
			holderId: FxtrID;
		}) => Promise<void>,
	): Promise<void> {
		await withFxtrScope({ name: "prov_server", include: [GLOBAL_SCOPE] }, async (scope) => {
			const arena = scope[FXTR_SCOPE_INTERNALS].mainArena;
			const dRef = await scope.store(SimpleEntity.create({ name: "base", value: 1 }));
			const dId = dRef.entityId;
			const dotId = scope.getContextMarkerId();

			// u-node wrapping the d-node
			const uId = await arena.createUNode(dId);

			// c-node: context data with a "value" field
			const ctxData = {
				"$fxtr.ctx": new RawFxtrEntityMarkerID(dotId),
				value: new RawStrongFxtrID(dId),
			};
			const cId = await arena.createCNode(ctxData);

			// p-node: pathroot data with a "value" field
			const pathrootData = {
				"$fxtr.ctx": new RawFxtrEntityMarkerID(dotId),
				value: new RawStrongFxtrID(dId),
			};
			const pId = await arena.createPNode(pathrootData);

			// d-node that references u, c, p (for "via parent load" tests)
			const holderTypeId = scope.trackBuiltin(FxtrID.builtin("test_prov_holder"));
			const holderCbor = {
				"$fxtr.ctx": new RawFxtrEntityMarkerID(dotId),
				"$fxtr.type": new RawWeakFxtrID(holderTypeId),
				ref_to_u: new RawWeakFxtrID(uId),
				ref_to_c: new RawWeakFxtrID(cId),
				ref_to_p: new RawWeakFxtrID(pId),
			};
			const holderId = await arena.store(holderCbor);

			const pins: Record<string, FxtrID> = {
				d_node: dId,
				u_node: uId,
				c_node: cId,
				p_node: pId,
				holder: holderId,
			};
			const app = createFxtrApp(scope, pins);
			const fetchFn = appFetch(app);

			await fn({ fetchFn, dId, uId, cId, pId, holderId });
		});
	}

	// -- Case (a): provenance nodes directly pinned and retrieved --

	it("u-node unwrap via direct pin", async () => {
		await withProvenanceServer(async ({ fetchFn, dId, uId }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const retrievedU = await ns.retrieve("u_node");
				expect(retrievedU.key()).toBe(uId.key());

				const unwrapped = ns.scope[FXTR_SCOPE_INTERNALS].mainArena.unwrapProvenance(uId);
				expect(unwrapped?.key()).toBe(dId.key());
			});
		});
	});

	it("c-node unwrap and ctx_node via direct pin", async () => {
		await withProvenanceServer(async ({ fetchFn, dId, cId }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				await ns.retrieve("c_node");

				const unwrapped = ns.scope[FXTR_SCOPE_INTERNALS].mainArena.unwrapProvenance(cId);
				expect(unwrapped?.key()).toBe(dId.key());

				const ctxDataId = ns.scope[FXTR_SCOPE_INTERNALS].mainArena.cNodeToContextData(cId);
				expect(ctxDataId).toBeDefined();
			});
		});
	});

	it("p-node unwrap and pathroot via direct pin", async () => {
		await withProvenanceServer(async ({ fetchFn, dId, pId }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				await ns.retrieve("p_node");

				const unwrapped = ns.scope[FXTR_SCOPE_INTERNALS].mainArena.unwrapProvenance(pId);
				expect(unwrapped?.key()).toBe(dId.key());

				const pathrootId = ns.scope[FXTR_SCOPE_INTERNALS].mainArena.pNodeToPathroot(pId);
				expect(pathrootId).toBeDefined();
			});
		});
	});

	// -- Case (b): provenance nodes discovered by loading a d-node that references them --

	it("load holder then unwrap u-node", async () => {
		await withProvenanceServer(async ({ fetchFn, dId, uId, holderId }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const holderFxtrId = await ns.retrieve("holder");
				expect(holderFxtrId.key()).toBe(holderId.key());

				const cbor = await ns.scope.loadCbor(holderFxtrId);
				expect(cbor).toBeTypeOf("object");

				// u_id should now be tracked with precomp from the /data/ response
				expect(ns.scope.isTracked(uId)).toBe(true);
				const unwrapped = ns.scope[FXTR_SCOPE_INTERNALS].mainArena.unwrapProvenance(uId);
				expect(unwrapped?.key()).toBe(dId.key());
			});
		});
	});

	it("load holder then unwrap c-node", async () => {
		await withProvenanceServer(async ({ fetchFn, dId, cId }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				await ns.retrieve("holder");
				await ns.scope.loadCbor(await ns.retrieve("holder"));

				expect(ns.scope.isTracked(cId)).toBe(true);
				const unwrapped = ns.scope[FXTR_SCOPE_INTERNALS].mainArena.unwrapProvenance(cId);
				expect(unwrapped?.key()).toBe(dId.key());
			});
		});
	});

	it("load holder then unwrap p-node", async () => {
		await withProvenanceServer(async ({ fetchFn, dId, pId }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				await ns.retrieve("holder");
				await ns.scope.loadCbor(await ns.retrieve("holder"));

				expect(ns.scope.isTracked(pId)).toBe(true);
				const unwrapped = ns.scope[FXTR_SCOPE_INTERNALS].mainArena.unwrapProvenance(pId);
				expect(unwrapped?.key()).toBe(dId.key());
			});
		});
	});

	// -- Case (c): provenance nodes discovered via deep fetch --

	it("deep fetch u-node populates precomp", async () => {
		await withProvenanceServer(async ({ fetchFn, dId, uId }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const arena = await ns.scope[FXTR_SCOPE_INTERNALS].deepFetch(uId);
				expect(arena).not.toBeNull();

				const unwrapped = arena?.unwrapProvenance(uId);
				expect(unwrapped?.key()).toBe(dId.key());
				arena?.close();
			});
		});
	});

	it("deep fetch c-node populates precomp", async () => {
		await withProvenanceServer(async ({ fetchFn, dId, cId }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const arena = await ns.scope[FXTR_SCOPE_INTERNALS].deepFetch(cId);
				expect(arena).not.toBeNull();

				const unwrapped = arena?.unwrapProvenance(cId);
				expect(unwrapped?.key()).toBe(dId.key());

				const ctxDataId = arena?.cNodeToContextData(cId);
				expect(ctxDataId).toBeDefined();
				arena?.close();
			});
		});
	});

	it("deep fetch p-node populates precomp", async () => {
		await withProvenanceServer(async ({ fetchFn, dId, pId }) => {
			await withHttpReadNamespace(fetchFn, async (ns) => {
				const arena = await ns.scope[FXTR_SCOPE_INTERNALS].deepFetch(pId);
				expect(arena).not.toBeNull();

				const unwrapped = arena?.unwrapProvenance(pId);
				expect(unwrapped?.key()).toBe(dId.key());

				const pathrootId = arena?.pNodeToPathroot(pId);
				expect(pathrootId).toBeDefined();
				arena?.close();
			});
		});
	});
});

// ---------------------------------------------------------------------------
// withBaseUrl helper
// ---------------------------------------------------------------------------

describe("withBaseUrl", () => {
	it("prepends base URL to relative paths", async () => {
		let capturedUrl = "";
		const mockFetch: FetchFn = async (url) => {
			capturedUrl = url;
			return new Response("ok");
		};

		const fn = withBaseUrl("http://localhost:8080", mockFetch);
		await fn("/data/abc");
		expect(capturedUrl).toBe("http://localhost:8080/data/abc");
	});

	it("does not prepend to absolute URLs", async () => {
		let capturedUrl = "";
		const mockFetch: FetchFn = async (url) => {
			capturedUrl = url;
			return new Response("ok");
		};

		const fn = withBaseUrl("http://localhost:8080", mockFetch);
		await fn("http://other.host/data/abc");
		expect(capturedUrl).toBe("http://other.host/data/abc");
	});

	it("handles base URL with trailing slash", async () => {
		let capturedUrl = "";
		const mockFetch: FetchFn = async (url) => {
			capturedUrl = url;
			return new Response("ok");
		};

		const fn = withBaseUrl("http://localhost:8080/", mockFetch);
		await fn("/data/abc");
		expect(capturedUrl).toBe("http://localhost:8080/data/abc");
	});
});
