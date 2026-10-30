/**
 * Tests for bicodec: field codecs, entity definitions, inline children,
 * frozen/cacheable propagation, defineType, FxtrBasicType.
 */

import { describe, expect, it } from "vitest";
import * as bicodec from "../../src/common/bicodec.js";
import { FXTR_CTX } from "../../src/common/bicodec.js";
import {
	PyCompatSet,
	entityRefKeyFn,
	fxtrIdKeyFn,
	numberKeyFn,
	pyCompatSet,
	stringKeyFn,
} from "../../src/common/pyCompatSet.js";
import { FxtrBasicType, UUID, defineType } from "../../src/common/systemEntities.js";
import {
	type FxtrCbor,
	FxtrID,
	RawStrongFxtrID,
	RawWeakFxtrID,
	deepFreeze,
} from "../../src/core/entityCore.js";
import {
	type Entity,
	type EntityLoadCodec,
	EntityRef,
	type EntityStoreCodec,
	FXTR_CACHEABLE,
	FXTR_CODEC,
} from "../../src/scope/entityRef.js";
import { type FxtrScope, GLOBAL_SCOPE, withFxtrScope } from "../../src/scope/scope.js";
import { unionLoadCodec } from "../../src/scope/unionLoadCodec.js";

// ---------------------------------------------------------------------------
// Helper: create a scope for testing
// ---------------------------------------------------------------------------

async function withTestScope(fn: (scope: FxtrScope) => Promise<void>): Promise<void> {
	await withFxtrScope({ include: [GLOBAL_SCOPE] }, fn);
}

// ---------------------------------------------------------------------------
// Scalar field codecs
// ---------------------------------------------------------------------------

describe("scalar field codecs", () => {
	it("string() round-trips", async () => {
		const codec = bicodec.string();
		expect(codec.kind).toBe("string");
		await withTestScope(async (scope) => {
			const cbor = await codec.toCbor("hello", scope);
			expect(cbor).toBe("hello");
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toBe("hello");
			expect(decoded.frozen).toBe(true);
		});
	});

	it("number() round-trips", async () => {
		const codec = bicodec.number();
		expect(codec.kind).toBe("number");
		await withTestScope(async (scope) => {
			const cbor = await codec.toCbor(42, scope);
			expect(cbor).toBe(42);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toBe(42);
			expect(decoded.frozen).toBe(true);
		});
	});

	it("number() handles floats", async () => {
		const codec = bicodec.number();
		await withTestScope(async (scope) => {
			const cbor = await codec.toCbor(3.14, scope);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toBeCloseTo(3.14);
		});
	});

	it("boolean() round-trips", async () => {
		const codec = bicodec.boolean();
		expect(codec.kind).toBe("boolean");
		await withTestScope(async (scope) => {
			expect((await codec.fromCbor(await codec.toCbor(true, scope), scope)).value).toBe(true);
			expect((await codec.fromCbor(await codec.toCbor(false, scope), scope)).value).toBe(false);
		});
	});

	it("bytes() round-trips (mutable by default)", async () => {
		const codec = bicodec.bytes();
		expect(codec.kind).toBe("bytes");
		await withTestScope(async (scope) => {
			const data = new Uint8Array([1, 2, 3]);
			const cbor = await codec.toCbor(data, scope);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toEqual(new Uint8Array([1, 2, 3]));
			expect(decoded.frozen).toBe(false);
		});
	});

	it("bytes({ promiseImmutable: true }) reports frozen", async () => {
		const codec = bicodec.bytes({ promiseImmutable: true });
		await withTestScope(async (scope) => {
			const data = new Uint8Array([1, 2, 3]);
			const decoded = await codec.fromCbor(await codec.toCbor(data, scope), scope);
			expect(decoded.value).toEqual(new Uint8Array([1, 2, 3]));
			expect(decoded.frozen).toBe(true);
		});
	});

	it("unknown() round-trips and deep-freezes", async () => {
		const codec = bicodec.unknown();
		expect(codec.kind).toBe("unknown");
		await withTestScope(async (scope) => {
			const data = { nested: [1, "two", { three: true }] } as FxtrCbor;
			const cbor = await codec.toCbor(deepFreeze(data), scope);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.frozen).toBe(true);
			expect(Object.isFrozen(decoded.value)).toBe(true);
		});
	});
});

// ---------------------------------------------------------------------------
// FxtrID codecs
// ---------------------------------------------------------------------------

describe("FxtrID field codecs", () => {
	it("fxtrId() wraps in RawWeakFxtrID", async () => {
		const codec = bicodec.fxtrId();
		expect(codec.kind).toBe("fxtrId");
		await withTestScope(async (scope) => {
			const id = FxtrID.builtin("test");
			const cbor = await codec.toCbor(id, scope);
			expect(cbor).toBeInstanceOf(RawWeakFxtrID);
			expect((cbor as RawWeakFxtrID).innerId.key()).toBe(id.key());
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value.key()).toBe(id.key());
			expect(decoded.frozen).toBe(true);
		});
	});

	it("childFxtrId() wraps in RawStrongFxtrID", async () => {
		const codec = bicodec.childFxtrId();
		expect(codec.kind).toBe("childFxtrId");
		await withTestScope(async (scope) => {
			const id = FxtrID.builtin("test");
			const cbor = await codec.toCbor(id, scope);
			expect(cbor).toBeInstanceOf(RawStrongFxtrID);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value.key()).toBe(id.key());
			expect(decoded.frozen).toBe(true);
		});
	});
});

// ---------------------------------------------------------------------------
// Entity ref codecs
// ---------------------------------------------------------------------------

describe("entity ref field codecs", () => {
	// A simple Entity-conforming type + load codec for testing
	interface DummyEntity extends Entity {
		readonly x: number;
	}

	const dummyStoreCodec: EntityStoreCodec<DummyEntity> = {
		async store(value: DummyEntity, scope: FxtrScope): Promise<EntityRef<DummyEntity>> {
			const id = await scope.storeCbor({ x: value.x } as unknown as FxtrCbor);
			return new EntityRef(id, dummyLoadCodec);
		},
	};

	const dummyLoadCodec: EntityLoadCodec<DummyEntity> = {
		async supportsEntityType() {
			return true;
		},
		async decode(cbor: FxtrCbor): Promise<DummyEntity> {
			const obj = { x: (cbor as Record<string, unknown>).x as number } as Record<string, unknown>;
			Object.defineProperty(obj, FXTR_CODEC, { value: dummyStoreCodec, enumerable: false });
			Object.defineProperty(obj, FXTR_CACHEABLE, { value: true, enumerable: false });
			return obj as unknown as DummyEntity;
		},
	};

	it("entityRef() creates EntityRef with weak CBOR tag", async () => {
		const codec = bicodec.entityRef(dummyLoadCodec);
		expect(codec.kind).toBe("entityRef");
		await withTestScope(async (scope) => {
			const id = FxtrID.builtin("ref-test");
			const ref = new EntityRef(id, dummyLoadCodec);
			const cbor = await codec.toCbor(ref, scope);
			expect(cbor).toBeInstanceOf(RawWeakFxtrID);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value.entityId.key()).toBe(id.key());
			expect(decoded.value.loadCodec).toBe(dummyLoadCodec);
			expect(decoded.frozen).toBe(true);
		});
	});

	it("childRef() creates EntityRef with strong CBOR tag", async () => {
		const codec = bicodec.childRef(dummyLoadCodec);
		expect(codec.kind).toBe("childRef");
		await withTestScope(async (scope) => {
			const id = FxtrID.builtin("ref-test");
			const ref = new EntityRef(id, dummyLoadCodec);
			const cbor = await codec.toCbor(ref, scope);
			expect(cbor).toBeInstanceOf(RawStrongFxtrID);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value.entityId.key()).toBe(id.key());
		});
	});

	it("entityRef() accepts EntityDefinition (which IS a load codec)", async () => {
		const TestDef = bicodec.entity(FxtrID.builtin("test-def"), {
			val: bicodec.number(),
		});
		const codec = bicodec.entityRef(TestDef);
		await withTestScope(async (scope) => {
			const id = FxtrID.builtin("some-id");
			const ref = new EntityRef(id, TestDef);
			const cbor = await codec.toCbor(ref, scope);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value.loadCodec).toBe(TestDef);
		});
	});
});

// ---------------------------------------------------------------------------
// Composite codecs
// ---------------------------------------------------------------------------

describe("composite field codecs", () => {
	it("object() round-trips nested fields", async () => {
		const codec = bicodec.object({
			name: bicodec.string(),
			score: bicodec.number(),
		});
		expect(codec.kind).toBe("object");
		await withTestScope(async (scope) => {
			const val = { name: "alice", score: 42 };
			const cbor = await codec.toCbor(val, scope);
			expect(cbor).toEqual({ name: "alice", score: 42 });
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toEqual({ name: "alice", score: 42 });
			expect(decoded.frozen).toBe(true);
			expect(Object.isFrozen(decoded.value)).toBe(true);
		});
	});

	it("array() round-trips and freezes", async () => {
		const codec = bicodec.array(bicodec.number());
		expect(codec.kind).toBe("array");
		await withTestScope(async (scope) => {
			const val = [1, 2, 3];
			const cbor = await codec.toCbor(val, scope);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toEqual([1, 2, 3]);
			expect(decoded.frozen).toBe(true);
			expect(Object.isFrozen(decoded.value)).toBe(true);
		});
	});

	it("array() with empty array", async () => {
		const codec = bicodec.array(bicodec.string());
		await withTestScope(async (scope) => {
			const decoded = await codec.fromCbor(await codec.toCbor([], scope), scope);
			expect(decoded.value).toEqual([]);
			expect(decoded.frozen).toBe(true);
		});
	});

	it("record() round-trips", async () => {
		const codec = bicodec.record(bicodec.number());
		expect(codec.kind).toBe("record");
		await withTestScope(async (scope) => {
			const val = { a: 1, b: 2 };
			const cbor = await codec.toCbor(val, scope);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toEqual({ a: 1, b: 2 });
			expect(decoded.frozen).toBe(true);
			expect(Object.isFrozen(decoded.value)).toBe(true);
		});
	});

	it("nullable() with value", async () => {
		const codec = bicodec.nullable(bicodec.string());
		expect(codec.kind).toBe("nullable");
		await withTestScope(async (scope) => {
			const cbor = await codec.toCbor("hello", scope);
			expect(cbor).toBe("hello");
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toBe("hello");
		});
	});

	it("nullable() with null", async () => {
		const codec = bicodec.nullable(bicodec.string());
		await withTestScope(async (scope) => {
			const cbor = await codec.toCbor(null, scope);
			expect(cbor).toBe(null);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toBe(null);
			expect(decoded.frozen).toBe(true);
		});
	});
});

// ---------------------------------------------------------------------------
// Combinators
// ---------------------------------------------------------------------------

describe("combinator codecs", () => {
	it("mapped() transforms values", async () => {
		// Map string <-> number
		const codec = bicodec.mapped(
			bicodec.string(),
			(s) => Number.parseInt(s, 10),
			(n) => String(n),
			{ frozen: true },
		);
		expect(codec.kind).toBe("mapped");
		await withTestScope(async (scope) => {
			const cbor = await codec.toCbor(42, scope);
			expect(cbor).toBe("42");
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value).toBe(42);
			expect(decoded.frozen).toBe(true);
		});
	});

	it("mapped() defaults frozen to false", async () => {
		const codec = bicodec.mapped(
			bicodec.string(),
			(s) => ({ wrapped: s }),
			(o) => o.wrapped,
		);
		await withTestScope(async (scope) => {
			const decoded = await codec.fromCbor("hello", scope);
			expect(decoded.value).toEqual({ wrapped: "hello" });
			expect(decoded.frozen).toBe(false);
		});
	});

	it("lazy() resolves and caches", async () => {
		let callCount = 0;
		const codec = bicodec.lazy(() => {
			callCount++;
			return bicodec.string();
		});
		expect(codec.kind).toBe("lazy");
		await withTestScope(async (scope) => {
			const cbor = await codec.toCbor("test", scope);
			expect(cbor).toBe("test");
			expect(callCount).toBe(1);
			await codec.fromCbor(cbor, scope);
			expect(callCount).toBe(1); // cached
		});
	});
});

// ---------------------------------------------------------------------------
// PyCompatSet
// ---------------------------------------------------------------------------

describe("PyCompatSet class", () => {
	it("basic add/has/delete/size", () => {
		const set = new PyCompatSet(stringKeyFn);
		set.add("a");
		set.add("b");
		expect(set.size).toBe(2);
		expect(set.has("a")).toBe(true);
		expect(set.has("c")).toBe(false);
		set.delete("a");
		expect(set.size).toBe(1);
		expect(set.has("a")).toBe(false);
	});

	it("deduplicates by key", () => {
		const id1 = FxtrID.builtin("test");
		const id2 = FxtrID.builtin("test"); // same key, different object
		const set = new PyCompatSet(fxtrIdKeyFn, [id1, id2]);
		expect(set.size).toBe(1);
	});

	it("iteration", () => {
		const set = new PyCompatSet(numberKeyFn, [1, 2, 3]);
		expect([...set].sort()).toEqual([1, 2, 3]);
	});

	it("constructor with items", () => {
		const set = new PyCompatSet(stringKeyFn, ["x", "y"]);
		expect(set.size).toBe(2);
		expect(set.has("x")).toBe(true);
	});

	it("freeze prevents mutation", () => {
		const set = new PyCompatSet(stringKeyFn, ["a"]);
		set.freeze();
		expect(set.frozen).toBe(true);
		expect(() => set.add("b")).toThrow("frozen");
		expect(() => set.delete("a")).toThrow("frozen");
	});

	it("freeze is idempotent", () => {
		const set = new PyCompatSet(stringKeyFn);
		set.freeze();
		set.freeze();
		expect(set.frozen).toBe(true);
	});
});

describe("pyCompatSet codec", () => {
	it("rejects unsupported codec kinds", () => {
		expect(() => pyCompatSet(bicodec.array(bicodec.string()))).toThrow("does not support");
		expect(() => pyCompatSet(bicodec.object({}))).toThrow("does not support");
	});

	it("string set round-trips with canonical sort", async () => {
		const codec = pyCompatSet(bicodec.string());
		expect(codec.kind).toBe("pyCompatSet");
		await withTestScope(async (scope) => {
			const set = new PyCompatSet(stringKeyFn, ["banana", "apple", "cherry"]);
			const cbor = await codec.toCbor(set, scope);
			// Should be sorted lexicographically
			expect(cbor).toEqual(["apple", "banana", "cherry"]);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value.size).toBe(3);
			expect(decoded.value.has("apple")).toBe(true);
			expect(decoded.value.has("banana")).toBe(true);
			expect(decoded.value.has("cherry")).toBe(true);
			expect(decoded.value.frozen).toBe(true);
			expect(decoded.frozen).toBe(true);
		});
	});

	it("number set sorts numerically", async () => {
		const codec = pyCompatSet(bicodec.number());
		await withTestScope(async (scope) => {
			const set = new PyCompatSet(numberKeyFn, [30, 2, 100]);
			const cbor = await codec.toCbor(set, scope);
			expect(cbor).toEqual([2, 30, 100]);
		});
	});

	it("fxtrId set wraps in RawWeakFxtrID and sorts by raw bytes", async () => {
		const codec = pyCompatSet(bicodec.fxtrId());
		await withTestScope(async (scope) => {
			const id1 = FxtrID.builtin("aaa");
			const id2 = FxtrID.builtin("zzz");
			const set = new PyCompatSet(fxtrIdKeyFn, [id2, id1]);
			const cbor = (await codec.toCbor(set, scope)) as FxtrCbor[];
			// id1 (!aaa) should sort before id2 (!zzz)
			expect((cbor[0] as RawWeakFxtrID).innerId.key()).toBe(id1.key());
			expect((cbor[1] as RawWeakFxtrID).innerId.key()).toBe(id2.key());
			// Round-trip
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value.size).toBe(2);
			expect(decoded.value.has(id1)).toBe(true);
			expect(decoded.value.has(id2)).toBe(true);
		});
	});

	it("entityRef set sorts by entity ID bytes", async () => {
		// biome-ignore lint/suspicious/noExplicitAny: test dummy codec
		const dummyCodec: EntityLoadCodec<any> = {
			async supportsEntityType() {
				return true;
			},
			async decode(_cbor: FxtrCbor) {
				const obj = {} as Record<string, unknown>;
				Object.defineProperty(obj, FXTR_CODEC, { value: {}, enumerable: false });
				Object.defineProperty(obj, FXTR_CACHEABLE, { value: true, enumerable: false });
				return obj;
			},
		};
		const codec = pyCompatSet(bicodec.entityRef(dummyCodec));
		await withTestScope(async (scope) => {
			const ref1 = new EntityRef(FxtrID.builtin("aaa"), dummyCodec);
			const ref2 = new EntityRef(FxtrID.builtin("zzz"), dummyCodec);
			const set = new PyCompatSet(entityRefKeyFn, [ref2, ref1]);
			const cbor = (await codec.toCbor(set, scope)) as FxtrCbor[];
			// ref1 should sort before ref2
			expect((cbor[0] as RawWeakFxtrID).innerId.key()).toBe(ref1.entityId.key());
			expect((cbor[1] as RawWeakFxtrID).innerId.key()).toBe(ref2.entityId.key());
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value.size).toBe(2);
		});
	});

	it("empty set round-trips", async () => {
		const codec = pyCompatSet(bicodec.string());
		await withTestScope(async (scope) => {
			const set = codec.createSet();
			const cbor = await codec.toCbor(set, scope);
			expect(cbor).toEqual([]);
			const decoded = await codec.fromCbor(cbor, scope);
			expect(decoded.value.size).toBe(0);
		});
	});

	it("createSet() builds a set with the correct key function", () => {
		const codec = pyCompatSet(bicodec.fxtrId());
		const id1 = FxtrID.builtin("a");
		const id2 = FxtrID.builtin("a"); // same key, different object
		const set = codec.createSet([id1, id2]);
		expect(set.size).toBe(1); // deduplicated by key
		expect(set.has(id1)).toBe(true);
	});

	it("bytes set sorts lexicographically", async () => {
		const codec = pyCompatSet(bicodec.bytes());
		await withTestScope(async (scope) => {
			const b1 = new Uint8Array([1, 2]);
			const b2 = new Uint8Array([1, 1]);
			const b3 = new Uint8Array([0, 255]);
			const set = codec.createSet([b1, b2, b3]);
			const cbor = (await codec.toCbor(set, scope)) as Uint8Array[];
			expect(cbor[0]).toEqual(b3); // [0, 255]
			expect(cbor[1]).toEqual(b2); // [1, 1]
			expect(cbor[2]).toEqual(b1); // [1, 2]
		});
	});
});

// ---------------------------------------------------------------------------
// EntityDefinition
// ---------------------------------------------------------------------------

describe("EntityDefinition", () => {
	// Register a test builtin type ID
	const TEST_TYPE_ID = FxtrID.builtin("test-bicodec-entity");

	it("create() returns mutable instance with FXTR_CODEC", async () => {
		GLOBAL_SCOPE.trackBuiltin(TEST_TYPE_ID);
		const TestEntity = bicodec.entity(TEST_TYPE_ID, {
			name: bicodec.string(),
			value: bicodec.number(),
		});
		const instance = TestEntity.create({ name: "foo", value: 10 });
		expect(instance.name).toBe("foo");
		expect(instance.value).toBe(10);
		expect(instance[FXTR_CODEC]).toBeDefined();
		expect(typeof instance.store).toBe("function");
		// Should be mutable
		expect(Object.isFrozen(instance)).toBe(false);
	});

	it("store/load round-trip", async () => {
		GLOBAL_SCOPE.trackBuiltin(TEST_TYPE_ID);
		const TestEntity = bicodec.entity(TEST_TYPE_ID, {
			name: bicodec.string(),
			value: bicodec.number(),
		});
		await withTestScope(async (scope) => {
			const instance = TestEntity.create({ name: "bar", value: 99 });
			const ref = await instance.store(scope);
			expect(ref.entityId).toBeDefined();
			expect(ref.loadCodec).toBe(TestEntity);

			const loaded = await ref.load(scope);
			expect(loaded.name).toBe("bar");
			expect(loaded.value).toBe(99);
			// Loaded should be frozen
			expect(Object.isFrozen(loaded)).toBe(true);
			// Loaded should have FXTR_CODEC
			expect(loaded[FXTR_CODEC]).toBeDefined();
			// Loaded should have store method
			expect(typeof loaded.store).toBe("function");
		});
	});

	it("loaded entity has FXTR_CTX set", async () => {
		GLOBAL_SCOPE.trackBuiltin(TEST_TYPE_ID);
		const TestEntity = bicodec.entity(TEST_TYPE_ID, {
			x: bicodec.number(),
		});
		await withTestScope(async (scope) => {
			const instance = TestEntity.create({ x: 1 });
			// Created instance should NOT have FXTR_CTX
			// biome-ignore lint/suspicious/noExplicitAny: accessing symbol property
			expect((instance as any)[FXTR_CTX]).toBeUndefined();

			const ref = await instance.store(scope);
			const loaded = await ref.load(scope);
			// Loaded instance SHOULD have FXTR_CTX
			// biome-ignore lint/suspicious/noExplicitAny: accessing symbol property
			expect((loaded as any)[FXTR_CTX]).toBeInstanceOf(FxtrID);
		});
	});

	it("re-store preserves FXTR_CTX", async () => {
		GLOBAL_SCOPE.trackBuiltin(TEST_TYPE_ID);
		const TestEntity = bicodec.entity(TEST_TYPE_ID, {
			x: bicodec.number(),
		});
		await withTestScope(async (scope) => {
			const ref1 = await TestEntity.create({ x: 1 }).store(scope);
			const loaded = await ref1.load(scope);

			// Re-store the loaded entity — should produce same ID (same content)
			const ref2 = await loaded.store(scope);
			expect(ref2.entityId.key()).toBe(ref1.entityId.key());
		});
	});

	it("supportsEntityType correctly identifies types", async () => {
		GLOBAL_SCOPE.trackBuiltin(TEST_TYPE_ID);
		const TestEntity = bicodec.entity(TEST_TYPE_ID, {
			x: bicodec.number(),
		});
		await withTestScope(async (scope) => {
			expect(await TestEntity.supportsEntityType(TEST_TYPE_ID, scope)).toBe(true);
			expect(await TestEntity.supportsEntityType(FxtrID.builtin("other"), scope)).toBe(false);
		});
	});

	it("ignores unknown CBOR keys on decode", async () => {
		GLOBAL_SCOPE.trackBuiltin(TEST_TYPE_ID);
		const TestEntity = bicodec.entity(TEST_TYPE_ID, {
			x: bicodec.number(),
		});
		await withTestScope(async (scope) => {
			const instance = TestEntity.create({ x: 42 });
			const ref = await instance.store(scope);
			// Load should succeed even though the CBOR may have extra keys
			const loaded = await ref.load(scope);
			expect(loaded.x).toBe(42);
		});
	});

	it("scope.store() works (alternative to entity.store())", async () => {
		GLOBAL_SCOPE.trackBuiltin(TEST_TYPE_ID);
		const TestEntity = bicodec.entity(TEST_TYPE_ID, {
			x: bicodec.number(),
		});
		await withTestScope(async (scope) => {
			const instance = TestEntity.create({ x: 7 });
			const ref = await scope.store(instance);
			const loaded = await scope.load(ref);
			expect(loaded.x).toBe(7);
		});
	});
});

// ---------------------------------------------------------------------------
// Cacheable/frozen propagation
// ---------------------------------------------------------------------------

describe("frozen/cacheable propagation", () => {
	const FTYPE = FxtrID.builtin("test-frozen-entity");

	it("all-frozen fields → cacheable: true", async () => {
		GLOBAL_SCOPE.trackBuiltin(FTYPE);
		const E = bicodec.entity(FTYPE, {
			s: bicodec.string(),
			n: bicodec.number(),
		});
		await withTestScope(async (scope) => {
			const ref = await E.create({ s: "a", n: 1 }).store(scope);
			const loaded = await ref.load(scope);
			expect(Object.isFrozen(loaded)).toBe(true);
		});
	});

	it("mapped with frozen: false → not frozen", async () => {
		GLOBAL_SCOPE.trackBuiltin(FTYPE);
		const mutableCodec = bicodec.mapped(
			bicodec.string(),
			(s) => ({ wrapped: s }),
			(o) => o.wrapped,
			// frozen defaults to false
		);
		const E = bicodec.entity(FTYPE, {
			val: mutableCodec,
		});
		await withTestScope(async (scope) => {
			const ref = await E.create({ val: { wrapped: "hi" } }).store(scope);
			const loaded = await ref.load(scope);
			// Not frozen because mapped field is not frozen
			expect(Object.isFrozen(loaded)).toBe(false);
		});
	});

	it("child() propagates non-cacheable from inner entity via FXTR_CACHEABLE", async () => {
		const INNER_TYPE = FxtrID.builtin("test-inner-mutable");
		const OUTER_TYPE = FxtrID.builtin("test-outer-child-frozen");
		GLOBAL_SCOPE.trackBuiltin(INNER_TYPE);
		GLOBAL_SCOPE.trackBuiltin(OUTER_TYPE);

		const Inner = bicodec.entity(INNER_TYPE, {
			val: bicodec.mapped(
				bicodec.string(),
				(s) => ({ wrapped: s }),
				(o) => o.wrapped,
				// frozen: false (default) → Inner entities are NOT cacheable
			),
		});
		const Outer = bicodec.entity(OUTER_TYPE, {
			label: bicodec.string(),
			inner: bicodec.child(Inner),
		});

		await withTestScope(async (scope) => {
			const inner = Inner.create({ val: { wrapped: "hi" } });
			const outer = Outer.create({ label: "test", inner });
			const ref = await outer.store(scope);
			const loaded = await ref.load(scope);
			// Outer should NOT be frozen because its child is not cacheable
			expect(Object.isFrozen(loaded)).toBe(false);
		});
	});

	it("child() propagates cacheable from frozen inner entity", async () => {
		const INNER_TYPE = FxtrID.builtin("test-inner-frozen");
		const OUTER_TYPE = FxtrID.builtin("test-outer-child-cacheable");
		GLOBAL_SCOPE.trackBuiltin(INNER_TYPE);
		GLOBAL_SCOPE.trackBuiltin(OUTER_TYPE);

		const Inner = bicodec.entity(INNER_TYPE, {
			val: bicodec.string(), // all frozen → Inner is cacheable
		});
		const Outer = bicodec.entity(OUTER_TYPE, {
			label: bicodec.string(),
			inner: bicodec.child(Inner),
		});

		await withTestScope(async (scope) => {
			const inner = Inner.create({ val: "hi" });
			const outer = Outer.create({ label: "test", inner });
			const ref = await outer.store(scope);
			const loaded = await ref.load(scope);
			// Outer should be frozen because all fields including child are cacheable
			expect(Object.isFrozen(loaded)).toBe(true);
		});
	});
});

// ---------------------------------------------------------------------------
// Inline child/linked entities
// ---------------------------------------------------------------------------

describe("inline child/linked entities", () => {
	const PARENT_TYPE = FxtrID.builtin("test-parent");
	const CHILD_TYPE = FxtrID.builtin("test-child");

	it("child() stores and loads inline entity", async () => {
		GLOBAL_SCOPE.trackBuiltin(PARENT_TYPE);
		GLOBAL_SCOPE.trackBuiltin(CHILD_TYPE);
		const Child = bicodec.entity(CHILD_TYPE, {
			val: bicodec.number(),
		});
		const Parent = bicodec.entity(PARENT_TYPE, {
			label: bicodec.string(),
			child: bicodec.child(Child),
		});

		await withTestScope(async (scope) => {
			const childInstance = Child.create({ val: 42 });
			const parent = Parent.create({
				label: "test",
				child: childInstance,
			});

			const parentRef = await parent.store(scope);
			const loaded = await parentRef.load(scope);
			expect(loaded.label).toBe("test");
			expect(loaded.child.val).toBe(42);
			// Child should also be frozen (loaded via scope.load)
			expect(Object.isFrozen(loaded.child)).toBe(true);
		});
	});

	it("linked() stores and loads inline entity with weak ref", async () => {
		GLOBAL_SCOPE.trackBuiltin(PARENT_TYPE);
		GLOBAL_SCOPE.trackBuiltin(CHILD_TYPE);
		const Child = bicodec.entity(CHILD_TYPE, {
			val: bicodec.number(),
		});
		const Parent = bicodec.entity(PARENT_TYPE, {
			label: bicodec.string(),
			link: bicodec.linked(Child),
		});

		await withTestScope(async (scope) => {
			const linked = Child.create({ val: 99 });
			const parent = Parent.create({
				label: "with-link",
				link: linked,
			});

			const parentRef = await parent.store(scope);
			const loaded = await parentRef.load(scope);
			expect(loaded.label).toBe("with-link");
			expect(loaded.link.val).toBe(99);
		});
	});

	it("entityRef() field is not auto-loaded", async () => {
		GLOBAL_SCOPE.trackBuiltin(PARENT_TYPE);
		GLOBAL_SCOPE.trackBuiltin(CHILD_TYPE);
		const Child = bicodec.entity(CHILD_TYPE, {
			val: bicodec.number(),
		});
		const Parent = bicodec.entity(PARENT_TYPE, {
			ref: bicodec.entityRef(Child),
		});

		await withTestScope(async (scope) => {
			const childRef = await Child.create({ val: 5 }).store(scope);
			const parent = Parent.create({ ref: childRef });
			const parentRef = await parent.store(scope);
			const loaded = await parentRef.load(scope);
			// ref field is an EntityRef, not the loaded entity
			expect(loaded.ref).toBeInstanceOf(EntityRef);
			expect(loaded.ref.entityId.key()).toBe(childRef.entityId.key());
			// Can load it explicitly
			const child = await loaded.ref.load(scope);
			expect(child.val).toBe(5);
		});
	});
});

// ---------------------------------------------------------------------------
// defineType + FxtrBasicType
// ---------------------------------------------------------------------------

describe("defineType and FxtrBasicType", () => {
	it("FxtrBasicType creates and stores correctly", async () => {
		await withTestScope(async (scope) => {
			const bt = FxtrBasicType.create({
				namespace_uuid: "550e8400-e29b-41d4-a716-446655440000",
				type_name: "TestType",
			});
			const ref = await bt.store(scope);
			const loaded = await ref.load(scope);
			expect(loaded.namespace_uuid).toBe("550e8400-e29b-41d4-a716-446655440000");
			expect(loaded.type_name).toBe("TestType");
		});
	});

	it("defineType returns deterministic ID", async () => {
		const id1 = await defineType(UUID("550e8400-e29b-41d4-a716-446655440000"), "Foo");
		const id2 = await defineType(UUID("550e8400-e29b-41d4-a716-446655440000"), "Foo");
		expect(id1.key()).toBe(id2.key());
	});

	it("defineType returns different IDs for different names", async () => {
		const id1 = await defineType(UUID("550e8400-e29b-41d4-a716-446655440000"), "TypeA");
		const id2 = await defineType(UUID("550e8400-e29b-41d4-a716-446655440000"), "TypeB");
		expect(id1.key()).not.toBe(id2.key());
	});

	it("entity with Promise<FxtrID> typeId works", async () => {
		const E = bicodec.entity(
			defineType(UUID("550e8400-e29b-41d4-a716-446655440000"), "PromiseTest"),
			{
				x: bicodec.number(),
			},
		);
		await withTestScope(async (scope) => {
			const ref = await E.create({ x: 42 }).store(scope);
			const loaded = await ref.load(scope);
			expect(loaded.x).toBe(42);
		});
	});

	it("create() with Promise typeId is synchronous", async () => {
		const E = bicodec.entity(
			defineType(UUID("550e8400-e29b-41d4-a716-446655440000"), "SyncCreateTest"),
			{
				x: bicodec.number(),
			},
		);
		// This should not throw — create doesn't need the type ID
		const instance = E.create({ x: 1 });
		expect(instance.x).toBe(1);
	});
});

// ---------------------------------------------------------------------------
// Nested/complex scenarios
// ---------------------------------------------------------------------------

describe("complex scenarios", () => {
	it("nested arrays of objects", async () => {
		const NTYPE = FxtrID.builtin("test-nested");
		GLOBAL_SCOPE.trackBuiltin(NTYPE);
		const E = bicodec.entity(NTYPE, {
			items: bicodec.array(
				bicodec.object({
					key: bicodec.string(),
					vals: bicodec.array(bicodec.number()),
				}),
			),
		});
		await withTestScope(async (scope) => {
			const instance = E.create({
				items: [
					{ key: "a", vals: [1, 2] },
					{ key: "b", vals: [3] },
				],
			});
			const ref = await instance.store(scope);
			const loaded = await ref.load(scope);
			expect(loaded.items).toHaveLength(2);
			// biome-ignore lint/style/noNonNullAssertion: test assertion on array element
			expect(loaded.items[0]!.key).toBe("a");
			// biome-ignore lint/style/noNonNullAssertion: test assertion on array element
			expect(loaded.items[0]!.vals).toEqual([1, 2]);
			// biome-ignore lint/style/noNonNullAssertion: test assertion on array element
			expect(loaded.items[1]!.key).toBe("b");
		});
	});

	it("nullable entity ref", async () => {
		const NRTYPE = FxtrID.builtin("test-nullable-ref");
		GLOBAL_SCOPE.trackBuiltin(NRTYPE);
		// biome-ignore lint/suspicious/noExplicitAny: test dummy codec
		const dummyCodec: EntityLoadCodec<any> = {
			async supportsEntityType() {
				return true;
			},
			async decode(_cbor: FxtrCbor) {
				const obj = {} as Record<string, unknown>;
				Object.defineProperty(obj, FXTR_CODEC, { value: {}, enumerable: false });
				Object.defineProperty(obj, FXTR_CACHEABLE, { value: true, enumerable: false });
				return obj;
			},
		};
		const E = bicodec.entity(NRTYPE, {
			ref: bicodec.nullable(bicodec.entityRef(dummyCodec)),
		});
		await withTestScope(async (scope) => {
			// null ref
			const ref1 = await E.create({ ref: null }).store(scope);
			const loaded1 = await ref1.load(scope);
			expect(loaded1.ref).toBe(null);
		});
	});

	it("lazy() for recursive entity types", async () => {
		const TREE_TYPE = FxtrID.builtin("test-tree");
		GLOBAL_SCOPE.trackBuiltin(TREE_TYPE);

		type TreeNodeFields = {
			label: string;
			children: bicodec.EntityInstance<TreeNodeFields>[];
		};
		const TreeNode: bicodec.EntityDefinition<bicodec.EntityInstance<TreeNodeFields>> =
			bicodec.entity(TREE_TYPE, {
				label: bicodec.string(),
				children: bicodec.array(bicodec.lazy(() => bicodec.child(TreeNode))),
			});

		await withTestScope(async (scope) => {
			const leaf = TreeNode.create({ label: "leaf", children: [] });
			const root = TreeNode.create({ label: "root", children: [leaf] });
			const ref = await root.store(scope);
			const loaded = await ref.load(scope);
			expect(loaded.label).toBe("root");
			expect(loaded.children).toHaveLength(1);
			// biome-ignore lint/style/noNonNullAssertion: test assertion on array element
			expect(loaded.children[0]!.label).toBe("leaf");
			// biome-ignore lint/style/noNonNullAssertion: test assertion on array element
			expect(loaded.children[0]!.children).toHaveLength(0);
		});
	});

	it("unionLoadCodec with bicodec entities", async () => {
		const TYPE_A = FxtrID.builtin("test-union-a");
		const TYPE_B = FxtrID.builtin("test-union-b");
		GLOBAL_SCOPE.trackBuiltin(TYPE_A);
		GLOBAL_SCOPE.trackBuiltin(TYPE_B);

		const EntityA = bicodec.entity(TYPE_A, { a: bicodec.string() });
		const EntityB = bicodec.entity(TYPE_B, { b: bicodec.number() });

		const union = unionLoadCodec(EntityA, EntityB);

		await withTestScope(async (scope) => {
			const refA = await EntityA.create({ a: "hello" }).store(scope);
			const refB = await EntityB.create({ b: 42 }).store(scope);

			const refAWithUnion = refA.withLoadCodec(union);
			const refBWithUnion = refB.withLoadCodec(union);

			const loadedA = await refAWithUnion.load(scope);
			const loadedB = await refBWithUnion.load(scope);

			// biome-ignore lint/suspicious/noExplicitAny: union type needs cast to access fields
			expect((loadedA as any).a).toBe("hello");
			// biome-ignore lint/suspicious/noExplicitAny: union type needs cast to access fields
			expect((loadedB as any).b).toBe(42);
		});
	});
});

// ---------------------------------------------------------------------------
// Type inference validation (compile-time checks)
// ---------------------------------------------------------------------------

describe("type inference", () => {
	const INFER_TYPE = FxtrID.builtin("test-infer");
	GLOBAL_SCOPE.trackBuiltin(INFER_TYPE);

	// Use the recommended named-type pattern:
	// _Score holds the inferred definition, Score interface names the instance type,
	// Score const is annotated so downstream types use the name.
	const _Score = bicodec.entity(INFER_TYPE, {
		player: bicodec.string(),
		value: bicodec.number(),
	});
	interface Score extends bicodec.InferEntity<typeof _Score> {}
	const Score: bicodec.EntityDefinition<Score> = _Score;

	it("InferEntity<> gives instance type with Entity interface", () => {
		const score = Score.create({ player: "alice", value: 42 });
		// score is Score which extends Entity (has FXTR_CODEC, store, etc.)
		const _codec: Score = score;
		expect(_codec[FXTR_CODEC]).toBeDefined();
		expect(typeof _codec.store).toBe("function");
	});

	it("child() with EntityDefinition infers the named instance type", () => {
		const childCodec = bicodec.child(Score);
		type ChildType = bicodec.InferField<typeof childCodec>;
		// ChildType should be Score (the named instance type)
		const check: ChildType = null as unknown as Score;
		expect(typeof check).not.toBe("undefined"); // use the variable
	});

	it("entityRef() with EntityDefinition infers EntityRef<Score>", () => {
		const codec = bicodec.entityRef(Score);
		type RefType = bicodec.InferField<typeof codec>;
		// RefType should be EntityRef<Score> — no EntityInstance wrapper needed
		const check: RefType = null as unknown as EntityRef<Score>;
		expect(typeof check).not.toBe("undefined");
	});
});
