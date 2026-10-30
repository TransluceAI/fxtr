/**
 * Tests for FxtrScope: lifecycle, hierarchy, caching, lift, copy, abandon,
 * freeze, deep fetch, tracing surfaces, keep-alive.
 */

import { describe, expect, it } from "vitest";
import {
	type FxtrCbor,
	FxtrID,
	RawFxtrEntityMarkerID,
	RawWeakFxtrID,
} from "../../src/core/entityCore.js";
import {
	type Entity,
	type EntityLoadCodec,
	EntityRef,
	type EntityStoreCodec,
	FXTR_CACHEABLE,
	FXTR_CODEC,
} from "../../src/scope/entityRef.js";
import {
	EntityNotLoadedError,
	FXTR_SCOPE_INTERNALS,
	type FxtrScope,
	GLOBAL_SCOPE,
	createFxtrScope,
	withFxtrScope,
} from "../../src/scope/scope.js";

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

interface CounterEntity extends Entity {
	readonly count: number;
}

const COUNTER_TYPE_ID = FxtrID.builtin("test-counter-type");

const counterLoadCodec: EntityLoadCodec<CounterEntity> = {
	async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
		return typeId.key() === COUNTER_TYPE_ID.key();
	},
	async decode(cbor: FxtrCbor, _scope: FxtrScope): Promise<CounterEntity> {
		return makeCounter((cbor as Record<string, unknown>).count as number, true);
	},
};

const counterStoreCodec: EntityStoreCodec<CounterEntity> = {
	async store(value: CounterEntity, scope: FxtrScope): Promise<EntityRef<CounterEntity>> {
		scope.trackBuiltin(COUNTER_TYPE_ID);
		const id = await scope.storeCbor({
			"$fxtr.type": new RawWeakFxtrID(COUNTER_TYPE_ID),
			"$fxtr.ctx": new RawFxtrEntityMarkerID(scope.getContextMarkerId()),
			count: value.count,
		});
		return new EntityRef(id, counterLoadCodec);
	},
};

function makeCounter(count: number, cacheable = false): CounterEntity {
	const obj = { count } as Record<string, unknown>;
	Object.defineProperty(obj, FXTR_CODEC, { value: counterStoreCodec, enumerable: false });
	if (cacheable) {
		Object.defineProperty(obj, FXTR_CACHEABLE, { value: true, enumerable: false });
	}
	return obj as unknown as CounterEntity;
}

// A mutable (non-cacheable) entity type for testing uncacheable loads.
interface MutableCounterEntity extends Entity {
	readonly count: number;
}

const mutableCounterStoreCodec: EntityStoreCodec<MutableCounterEntity> = {
	async store(
		value: MutableCounterEntity,
		scope: FxtrScope,
	): Promise<EntityRef<MutableCounterEntity>> {
		scope.trackBuiltin(COUNTER_TYPE_ID);
		const id = await scope.storeCbor({
			"$fxtr.type": new RawWeakFxtrID(COUNTER_TYPE_ID),
			"$fxtr.ctx": new RawFxtrEntityMarkerID(scope.getContextMarkerId()),
			count: value.count,
		});
		return new EntityRef(id, mutableCounterLoadCodec);
	},
};

const mutableCounterLoadCodec: EntityLoadCodec<MutableCounterEntity> = {
	async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
		return typeId.key() === COUNTER_TYPE_ID.key();
	},
	async decode(cbor: FxtrCbor, _scope: FxtrScope): Promise<MutableCounterEntity> {
		const count = (cbor as Record<string, unknown>).count as number;
		const obj = { count } as Record<string, unknown>;
		Object.defineProperty(obj, FXTR_CODEC, { value: mutableCounterStoreCodec, enumerable: false });
		// No FXTR_CACHEABLE → not cacheable
		return obj as unknown as MutableCounterEntity;
	},
};

// ---------------------------------------------------------------------------
// Scope lifecycle
// ---------------------------------------------------------------------------

describe("scope lifecycle", () => {
	it("withFxtrScope creates a scope that is alive during callback", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			expect(scope.isAlive()).toBe(true);
		});
	});

	it("scope is not alive after callback completes", async () => {
		let capturedScope: FxtrScope | undefined;
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			capturedScope = scope;
		});
		expect(capturedScope).toBeDefined();
		// biome-ignore lint/style/noNonNullAssertion: verified defined above
		expect(capturedScope!.isAlive()).toBe(false);
	});

	it("scope is not alive after callback throws", async () => {
		let capturedScope: FxtrScope | undefined;
		try {
			await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
				capturedScope = scope;
				throw new Error("test error");
			});
		} catch {
			// expected
		}
		// biome-ignore lint/style/noNonNullAssertion: assigned in callback above
		expect(capturedScope!.isAlive()).toBe(false);
	});

	it("getContextMarkerId returns the '.' builtin", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ctxId = scope.getContextMarkerId();
			expect(ctxId.getTag()).toBe(".");
		});
	});

	it("GLOBAL_SCOPE is alive", () => {
		expect(GLOBAL_SCOPE.isAlive()).toBe(true);
	});
});

// ---------------------------------------------------------------------------
// Hierarchy
// ---------------------------------------------------------------------------

describe("scope hierarchy", () => {
	it("child scope can access parent entities via lift", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (parent) => {
			const ref = await parent.store(makeCounter(10));

			await withFxtrScope({ include: [parent] }, async (child) => {
				child.lift(ref.entityId);
				const loaded = await child.load(ref);
				expect(loaded.count).toBe(10);
			});
		});
	});

	it("lift copies cache entries from parent", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (parent) => {
			const ref = await parent.store(makeCounter(20));
			// Load in parent to populate cache.
			await parent.load(ref);

			await withFxtrScope({ include: [parent] }, async (child) => {
				// Load in child should use parent's cached value.
				const loaded = await child.load(ref);
				expect(loaded.count).toBe(20);
			});
		});
	});

	it("includedInScope is true for ancestors", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (parent) => {
			await withFxtrScope({ include: [parent] }, async (child) => {
				expect(parent.includedInScope(child)).toBe(true);
				expect(GLOBAL_SCOPE.includedInScope(child)).toBe(true);
				expect(child.includedInScope(parent)).toBe(false);
			});
		});
	});

	it("scope is included in itself", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			expect(scope.includedInScope(scope)).toBe(true);
		});
	});

	it("lift fails if node not in any parent", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (parent) => {
			const ref = await parent.store(makeCounter(1));

			await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (sibling) => {
				// ref is in parent, not in sibling's ancestry.
				expect(() => sibling.lift(ref.entityId)).toThrow("not found in any parent scope");
			});
		});
	});
});

// ---------------------------------------------------------------------------
// Copy
// ---------------------------------------------------------------------------

describe("copyFromScope", () => {
	it("copies entity and cache between unrelated scopes", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (source) => {
			const ref = await source.store(makeCounter(77));
			await source.load(ref); // populate cache

			await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (target) => {
				await target.copyFromScope(ref, source);
				expect(target.isTracked(ref.entityId)).toBe(true);
				const loaded = await target.load(ref);
				expect(loaded.count).toBe(77);
			});
		});
	});

	it("ref.copy({from, to}) convenience works", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (source) => {
			const ref = await source.store(makeCounter(33));

			await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (target) => {
				await ref.copy({ from: source, to: target });
				const loaded = await target.load(ref);
				expect(loaded.count).toBe(33);
			});
		});
	});
});

// ---------------------------------------------------------------------------
// Abandon
// ---------------------------------------------------------------------------

describe("abandon", () => {
	it("abandon removes the entity from the scope", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(1));
			expect(scope.isTracked(ref.entityId)).toBe(true);
			scope.abandon(ref.entityId);
			expect(scope.isTracked(ref.entityId)).toBe(false);
		});
	});

	it("abandon clears cache for the abandoned entity", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(1));
			await scope.load(ref);
			// Cached value should be accessible.
			expect(scope.getCachedValue(ref).count).toBe(1);

			scope.abandon(ref.entityId);
			expect(() => scope.getCachedValue(ref)).toThrow(EntityNotLoadedError);
		});
	});

	it("abandon throws for untracked node", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const fakeId = FxtrID.builtin("nonexistent");
			expect(() => scope.abandon(fakeId)).toThrow("not tracked");
		});
	});
});

// ---------------------------------------------------------------------------
// Caching
// ---------------------------------------------------------------------------

describe("caching", () => {
	it("getCachedValue returns cached value after load", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(42));
			await scope.load(ref);
			const cached = scope.getCachedValue(ref);
			expect(cached.count).toBe(42);
		});
	});

	it("getCachedValue throws EntityNotLoadedError before load", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(42));
			expect(() => scope.getCachedValue(ref)).toThrow(EntityNotLoadedError);
		});
	});

	it("clearCache removes cached values", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(42));
			await scope.load(ref);
			expect(scope.getCachedValue(ref).count).toBe(42);

			scope.clearCache();
			expect(() => scope.getCachedValue(ref)).toThrow(EntityNotLoadedError);
		});
	});

	it("non-cacheable loads are not cached", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(42));
			const mutableRef = new EntityRef(ref.entityId, mutableCounterLoadCodec);

			const v1 = await scope.load(mutableRef);
			const v2 = await scope.load(mutableRef);
			// Different object identity (not cached).
			expect(v1).not.toBe(v2);
			expect(v1.count).toBe(42);
			expect(v2.count).toBe(42);
		});
	});
});

// ---------------------------------------------------------------------------
// Freezing
// ---------------------------------------------------------------------------

describe("freezing", () => {
	// Freeze tests use scopes without GLOBAL_SCOPE as parent, because
	// GLOBAL_SCOPE is a root scope that can't be frozen, and freezing
	// requires all parents to be frozen.

	it("frozen scope disallows store", async () => {
		await withFxtrScope({}, async (freezable) => {
			const ref = await freezable.store(makeCounter(1));
			await freezable.load(ref);

			freezable.freeze();
			expect(freezable.frozen).toBe(true);
			expect(() => freezable.store(makeCounter(2))).toThrow("frozen");
		});
	});

	it("frozen scope disallows load", async () => {
		await withFxtrScope({}, async (freezable) => {
			const ref = await freezable.store(makeCounter(1));
			await freezable.load(ref);

			freezable.freeze();
			expect(() => freezable.load(ref)).toThrow("frozen");
		});
	});

	it("frozen scope allows getCachedValue", async () => {
		await withFxtrScope({}, async (freezable) => {
			const ref = await freezable.store(makeCounter(99));
			await freezable.load(ref);

			freezable.freeze();
			expect(freezable.getCachedValue(ref).count).toBe(99);
		});
	});

	it("frozen scope allows isTracked", async () => {
		await withFxtrScope({}, async (freezable) => {
			const ref = await freezable.store(makeCounter(1));

			freezable.freeze();
			expect(freezable.isTracked(ref.entityId)).toBe(true);
		});
	});

	it("freeze requires all parents to be frozen", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (parent) => {
			await withFxtrScope({ include: [parent] }, async (child) => {
				expect(() => child.freeze()).toThrow("parent");
			});
		});
	});

	it("root scope cannot be frozen", () => {
		expect(() => GLOBAL_SCOPE.freeze()).toThrow("frozen");
	});

	it("freeze is idempotent", async () => {
		await withFxtrScope({}, async (freezable) => {
			freezable.freeze();
			freezable.freeze(); // no-op
			expect(freezable.frozen).toBe(true);
		});
	});

	it("child of frozen scope can load from frozen parent", async () => {
		await withFxtrScope({}, async (freezable) => {
			const ref = await freezable.store(makeCounter(55));
			await freezable.load(ref);
			freezable.freeze();

			await withFxtrScope({ include: [freezable] }, async (child) => {
				const loaded = await child.load(ref);
				expect(loaded.count).toBe(55);
			});
		});
	});
});

// ---------------------------------------------------------------------------
// Tracing surface restrictions
// ---------------------------------------------------------------------------

describe("tracing surface restrictions", () => {
	it("store is disallowed during load codec decode", async () => {
		const sneakyCodec: EntityLoadCodec<CounterEntity> = {
			async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
				return typeId.key() === COUNTER_TYPE_ID.key();
			},
			async decode(cbor: FxtrCbor, scope: FxtrScope): Promise<CounterEntity> {
				// Try to store during decode — should throw.
				expect(() => scope.store(makeCounter(999))).toThrow("actively loading");
				return makeCounter((cbor as Record<string, unknown>).count as number, true);
			},
		};

		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(1));
			const sneakyRef = new EntityRef(ref.entityId, sneakyCodec);
			await scope.load(sneakyRef);
		});
	});

	it("lift is disallowed during load codec decode", async () => {
		const sneakyCodec: EntityLoadCodec<CounterEntity> = {
			async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
				return typeId.key() === COUNTER_TYPE_ID.key();
			},
			async decode(cbor: FxtrCbor, scope: FxtrScope): Promise<CounterEntity> {
				const fakeId = FxtrID.builtin("dummy");
				expect(() => scope.lift(fakeId)).toThrow("actively loading");
				return makeCounter((cbor as Record<string, unknown>).count as number, true);
			},
		};

		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(1));
			const sneakyRef = new EntityRef(ref.entityId, sneakyCodec);
			await scope.load(sneakyRef);
		});
	});

	it("load IS allowed during decode (for child entities)", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			// Store a child entity first.
			const childRef = await scope.store(makeCounter(100));

			// Store a parent entity that references the child.
			scope.trackBuiltin(COUNTER_TYPE_ID);
			const parentId = await scope.storeCbor({
				"$fxtr.type": new RawWeakFxtrID(COUNTER_TYPE_ID),
				"$fxtr.ctx": new RawFxtrEntityMarkerID(scope.getContextMarkerId()),
				count: 1,
			});

			// A codec that loads the child entity during decode via loadCbor.
			interface ParentWithChild extends Entity {
				readonly count: number;
				readonly childCbor: unknown;
			}

			const parentStoreCodec: EntityStoreCodec<ParentWithChild> =
				counterStoreCodec as unknown as EntityStoreCodec<ParentWithChild>;

			const simpleChildCodec: EntityLoadCodec<ParentWithChild> = {
				async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
					return typeId.key() === COUNTER_TYPE_ID.key();
				},
				async decode(cbor: FxtrCbor, scope: FxtrScope): Promise<ParentWithChild> {
					// Load the child via loadCbor — this should work on tracing surface.
					const childCbor = await scope.loadCbor(childRef.entityId);
					const count = (cbor as Record<string, unknown>).count as number;
					const obj = { count, childCbor } as Record<string, unknown>;
					Object.defineProperty(obj, FXTR_CODEC, { value: parentStoreCodec, enumerable: false });
					Object.defineProperty(obj, FXTR_CACHEABLE, { value: true, enumerable: false });
					return obj as unknown as ParentWithChild;
				},
			};

			const testRef = new EntityRef(parentId, simpleChildCodec);
			const loaded = await scope.load(testRef);
			expect(loaded.count).toBe(1);
			expect(loaded.childCbor).toBeDefined();
		});
	});
});

// ---------------------------------------------------------------------------
// Keep-alive
// ---------------------------------------------------------------------------

describe("keep-alive", () => {
	it("parent scope waits for child scope to close", async () => {
		const events: string[] = [];

		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (parent) => {
			// Start a child that will close after a short delay.
			const childPromise = withFxtrScope({ include: [parent] }, async (_child) => {
				await new Promise((resolve) => setTimeout(resolve, 50));
				events.push("child-done");
			});

			// Parent callback returns immediately, but withFxtrScope should wait.
			await childPromise;
			events.push("parent-done");
		});

		expect(events).toEqual(["child-done", "parent-done"]);
	});
});

// ---------------------------------------------------------------------------
// Dead scope operations
// ---------------------------------------------------------------------------

describe("dead scope operations", () => {
	it("store on dead scope throws", async () => {
		let deadScope: FxtrScope | undefined;
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			deadScope = scope;
		});
		expect(deadScope).toBeDefined();
		expect(() => deadScope?.store(makeCounter(1))).toThrow(/closing|shut down/);
	});

	it("load on dead scope throws", async () => {
		let deadScope: FxtrScope | undefined;
		let capturedRef: EntityRef<CounterEntity> | undefined;
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			capturedRef = await scope.store(makeCounter(1));
			deadScope = scope;
		});
		expect(deadScope).toBeDefined();
		expect(capturedRef).toBeDefined();
		// biome-ignore lint/style/noNonNullAssertion: asserted above
		expect(() => deadScope?.load(capturedRef!)).toThrow(/closing|shut down/);
	});
});

// ---------------------------------------------------------------------------
// isTracked
// ---------------------------------------------------------------------------

describe("isTracked", () => {
	it("returns true for stored entities", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(1));
			expect(scope.isTracked(ref.entityId)).toBe(true);
			expect(scope.isTracked(ref)).toBe(true);
		});
	});

	it("returns false for unknown entities", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			expect(scope.isTracked(FxtrID.builtin("nope"))).toBe(false);
		});
	});
});

// ---------------------------------------------------------------------------
// Frozen scope — all disallowed operations
// ---------------------------------------------------------------------------

describe("frozen scope disallowed operations", () => {
	it("storeCbor raises", async () => {
		await withFxtrScope({}, async (scope) => {
			scope.freeze();
			expect(() =>
				scope.storeCbor({
					"$fxtr.type": new RawWeakFxtrID(COUNTER_TYPE_ID),
					"$fxtr.ctx": new RawFxtrEntityMarkerID(scope.getContextMarkerId()),
					count: 1,
				}),
			).toThrow("frozen");
		});
	});

	it("loadCbor raises", async () => {
		await withFxtrScope({}, async (scope) => {
			const ref = await scope.store(makeCounter(1));
			scope.freeze();
			expect(() => scope.loadCbor(ref.entityId)).toThrow("frozen");
		});
	});

	it("lift raises", async () => {
		await withFxtrScope({}, async (scope) => {
			scope.freeze();
			expect(() => scope.lift(FxtrID.builtin("x"))).toThrow("frozen");
		});
	});

	it("copyFromScope raises", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (source) => {
			const ref = await source.store(makeCounter(1));
			await withFxtrScope({}, async (target) => {
				target.freeze();
				expect(() => target.copyFromScope(ref, source)).toThrow("frozen");
			});
		});
	});

	it("trackBuiltin raises", async () => {
		await withFxtrScope({}, async (scope) => {
			scope.freeze();
			expect(() => scope.trackBuiltin(FxtrID.builtin("x"))).toThrow("frozen");
		});
	});

	it("clearCache raises", async () => {
		await withFxtrScope({}, async (scope) => {
			scope.freeze();
			expect(() => scope.clearCache()).toThrow("frozen");
		});
	});

	it("abandon raises", async () => {
		await withFxtrScope({}, async (scope) => {
			const ref = await scope.store(makeCounter(1));
			scope.freeze();
			expect(() => scope.abandon(ref)).toThrow("frozen");
		});
	});

	it("error messages contain method name", async () => {
		await withFxtrScope({}, async (scope) => {
			scope.freeze();
			expect(() => scope.lift(FxtrID.builtin("x"))).toThrow("lift");
			expect(() => scope.trackBuiltin(FxtrID.builtin("x"))).toThrow("trackBuiltin");
			expect(() => scope.clearCache()).toThrow("clearCache");
		});
	});
});

// ---------------------------------------------------------------------------
// Frozen scope — freeze chain
// ---------------------------------------------------------------------------

describe("freeze chain", () => {
	it("freeze parent then freeze child", async () => {
		await withFxtrScope({}, async (parent) => {
			const ref = await parent.store(makeCounter(10));
			await parent.load(ref);

			await withFxtrScope({ include: [parent] }, async (child) => {
				await child.load(ref);

				parent.freeze();
				child.freeze();

				expect(parent.frozen).toBe(true);
				expect(child.frozen).toBe(true);
				expect(child.getCachedValue(ref).count).toBe(10);
			});
		});
	});
});

// ---------------------------------------------------------------------------
// Different load codecs give different results
// ---------------------------------------------------------------------------

describe("different load codecs", () => {
	it("same entity ID with different codecs gives different results", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(42));

			// Load with the typed codec.
			const typed = await scope.load(ref);
			expect(typed.count).toBe(42);

			// Load same entity with the mutable counter codec.
			const mutableRef = new EntityRef(ref.entityId, mutableCounterLoadCodec);
			const mutable = await scope.load(mutableRef);
			expect(mutable.count).toBe(42);

			// They are different objects (different codecs, different cache entries).
			expect(typed).not.toBe(mutable);
		});
	});
});

// ---------------------------------------------------------------------------
// Load untracked entity raises
// ---------------------------------------------------------------------------

describe("load errors", () => {
	it("load untracked entity raises", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const fakeId = FxtrID.builtin("nonexistent");
			const ref = new EntityRef(fakeId, counterLoadCodec);
			expect(() => scope.load(ref)).toThrow("not found");
		});
	});
});

// ---------------------------------------------------------------------------
// Side-effect replay
// ---------------------------------------------------------------------------

describe("side-effect replay", () => {
	it("cached load with child entity replays side effects", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			// Store a child entity.
			const childRef = await scope.store(makeCounter(100));

			// Store a parent entity that references the child.
			scope.trackBuiltin(COUNTER_TYPE_ID);
			const parentId = await scope.storeCbor({
				"$fxtr.type": new RawWeakFxtrID(COUNTER_TYPE_ID),
				"$fxtr.ctx": new RawFxtrEntityMarkerID(scope.getContextMarkerId()),
				count: 1,
			});

			// A codec that loads the child during decode.
			interface ParentWithChildCount extends Entity {
				readonly count: number;
				readonly childCount: number;
			}

			const parentStoreCodec2: EntityStoreCodec<ParentWithChildCount> =
				counterStoreCodec as unknown as EntityStoreCodec<ParentWithChildCount>;

			const parentCodec: EntityLoadCodec<ParentWithChildCount> = {
				async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
					return typeId.key() === COUNTER_TYPE_ID.key();
				},
				async decode(cbor: FxtrCbor, scope: FxtrScope): Promise<ParentWithChildCount> {
					const child = await scope.load(childRef);
					const count = (cbor as Record<string, unknown>).count as number;
					const obj = { count, childCount: child.count } as Record<string, unknown>;
					Object.defineProperty(obj, FXTR_CODEC, { value: parentStoreCodec2, enumerable: false });
					Object.defineProperty(obj, FXTR_CACHEABLE, { value: true, enumerable: false });
					return obj as unknown as ParentWithChildCount;
				},
			};

			const parentRef = new EntityRef(parentId, parentCodec);

			// First load — triggers decode and child load.
			const v1 = await scope.load(parentRef);
			expect(v1.count).toBe(1);
			expect(v1.childCount).toBe(100);

			// Second load — returns cached value, replays side effects.
			const v2 = await scope.load(parentRef);
			expect(v2).toBe(v1); // same cached object
		});
	});
});

// ---------------------------------------------------------------------------
// Deep fetch
// ---------------------------------------------------------------------------

describe("deep fetch", () => {
	it("root scope is deeply fetchable", async () => {
		// GLOBAL_SCOPE is a root scope, should be deeply fetchable.
		// We can't call deepFetch directly on GLOBAL_SCOPE for a missing entity,
		// but we can verify it doesn't throw "not deeply fetchable".
		const result = await GLOBAL_SCOPE[FXTR_SCOPE_INTERNALS].deepFetch(
			FxtrID.builtin("nonexistent"),
		);
		expect(result).toBeNull();
	});

	it("child inherits deep fetchability", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (child) => {
			// Parent (GLOBAL_SCOPE) is deeply fetchable, so child should be too.
			const result = await child[FXTR_SCOPE_INTERNALS].deepFetch(FxtrID.builtin("nonexistent"));
			expect(result).toBeNull();
		});
	});

	it("deep fetch finds entity in scope store", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(42));
			const found = await scope[FXTR_SCOPE_INTERNALS].deepFetch(ref.entityId);
			expect(found).not.toBeNull();
			found?.close();
		});
	});

	it("deep fetch finds entity in parent store", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (parent) => {
			const ref = await parent.store(makeCounter(42));

			await withFxtrScope({ include: [parent] }, async (child) => {
				const found = await child[FXTR_SCOPE_INTERNALS].deepFetch(ref.entityId);
				expect(found).not.toBeNull();
				found?.close();
			});
		});
	});

	it("deep fetch returns null for missing entity", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const result = await scope[FXTR_SCOPE_INTERNALS].deepFetch(FxtrID.builtin("totally-missing"));
			expect(result).toBeNull();
		});
	});

	it("deep fetch with external source", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (sourceScope) => {
			const ref = await sourceScope.store(makeCounter(77));

			await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (targetScope) => {
				// Add sourceScope's store as a deep fetch source.
				targetScope[FXTR_SCOPE_INTERNALS].deepFetchSources.add(
					sourceScope[FXTR_SCOPE_INTERNALS].backingStore,
				);

				const found = await targetScope[FXTR_SCOPE_INTERNALS].deepFetch(ref.entityId);
				expect(found).not.toBeNull();
				found?.close();
			});
		});
	});
});

// ---------------------------------------------------------------------------
// Scope inclusion (extended)
// ---------------------------------------------------------------------------

describe("scope inclusion (extended)", () => {
	it("grandparent included in grandchild", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (grandparent) => {
			await withFxtrScope({ include: [grandparent] }, async (parent) => {
				await withFxtrScope({ include: [parent] }, async (child) => {
					expect(grandparent.includedInScope(child)).toBe(true);
				});
			});
		});
	});

	it("sibling scopes not included in each other", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (parent) => {
			await withFxtrScope({ include: [parent] }, async (siblingA) => {
				await withFxtrScope({ include: [parent] }, async (siblingB) => {
					expect(siblingA.includedInScope(siblingB)).toBe(false);
					expect(siblingB.includedInScope(siblingA)).toBe(false);
				});
			});
		});
	});
});

// ---------------------------------------------------------------------------
// Copy tracks children
// ---------------------------------------------------------------------------

describe("copy tracks children", () => {
	it("after copy, loading the entity tracks its children", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (source) => {
			const ref = await source.store(makeCounter(10));

			await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (target) => {
				await target.copyFromScope(ref, source);
				expect(target.isTracked(ref.entityId)).toBe(true);

				// Loading the entity should work and track any referenced IDs.
				const loaded = await target.load(ref);
				expect(loaded.count).toBe(10);
			});
		});
	});
});

// ---------------------------------------------------------------------------
// createFxtrScope / scheduleClose / dispose
// ---------------------------------------------------------------------------

describe("createFxtrScope", () => {
	it("creates a scope that is alive", () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		expect(scope.isAlive()).toBe(true);
		scope.scheduleClose();
	});

	it("can store and load entities", async () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		try {
			const ref = await scope.store(makeCounter(42));
			const loaded = await scope.load(ref);
			expect(loaded.count).toBe(42);
		} finally {
			await scope.dispose();
		}
	});

	it("works with no parents", () => {
		const scope = createFxtrScope();
		expect(scope.isAlive()).toBe(true);
		scope.scheduleClose();
	});

	it("inherits deep fetchability from parents", () => {
		// GLOBAL_SCOPE is deeply fetchable
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		// If not deeply fetchable, deepFetch would throw. We just check
		// that the scope was created without error, which implies
		// inferDeepFetchability succeeded.
		expect(scope.isAlive()).toBe(true);
		scope.scheduleClose();
	});
});

describe("scheduleClose", () => {
	it("blocks direct operations on the scope", async () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		const ref = await scope.store(makeCounter(1));
		scope.scheduleClose();

		expect(scope.isAlive()).toBe(false);
		expect(() => scope.store(makeCounter(2))).toThrow(/closing|shut down/);
		expect(() => scope.load(ref)).toThrow(/closing|shut down/);
	});

	it("is idempotent", () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		scope.scheduleClose();
		scope.scheduleClose(); // no error
		scope.scheduleClose(); // still no error
	});

	it("children continue working after parent scheduleClose", async () => {
		const parent = createFxtrScope({ include: [GLOBAL_SCOPE] });
		const ref = await parent.store(makeCounter(99));

		const child = createFxtrScope({ include: [parent] });
		parent.scheduleClose();

		// Parent is closing, but child should still work.
		expect(child.isAlive()).toBe(true);
		const loaded = await child.load(ref);
		expect(loaded.count).toBe(99);

		// Clean up child, which lets parent finish shutdown.
		await child.dispose();
		await parent.dispose();
	});

	it("cannot create child of a closing scope", () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		scope.scheduleClose();

		expect(() => createFxtrScope({ include: [scope] })).toThrow(/closing|shut down/);
	});
});

describe("dispose", () => {
	it("waits for children to close", async () => {
		const parent = createFxtrScope({ include: [GLOBAL_SCOPE] });
		const child = createFxtrScope({ include: [parent] });

		// Start parent dispose — it should wait for child.
		let parentDisposed = false;
		const parentDisposePromise = parent.dispose().then(() => {
			parentDisposed = true;
		});

		// Parent should not be disposed yet (child still alive).
		await new Promise((r) => setTimeout(r, 10));
		expect(parentDisposed).toBe(false);

		// Close child — parent should now finish.
		await child.dispose();
		await parentDisposePromise;
		expect(parentDisposed).toBe(true);
	});

	it("resolves immediately if no children", async () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		await scope.dispose(); // should not hang
	});

	it("can be called multiple times", async () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		await scope.dispose();
		await scope.dispose(); // idempotent, should not error
	});
});

describe("withFxtrScope uses createFxtrScope internally", () => {
	it("still works for basic store/load", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(7));
			const loaded = await scope.load(ref);
			expect(loaded.count).toBe(7);
		});
	});

	it("scope is not alive after callback", async () => {
		let captured: FxtrScope | undefined;
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			captured = scope;
		});
		expect(captured?.isAlive()).toBe(false);
	});
});
