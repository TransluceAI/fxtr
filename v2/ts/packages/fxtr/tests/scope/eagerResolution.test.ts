/**
 * Tests that verify operations resolve synchronously (return Eager)
 * when the needed data is already available in memory.
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
	type FxtrScope,
	GLOBAL_SCOPE,
	createFxtrScope,
	withFxtrScope,
} from "../../src/scope/scope.js";
import { Eager, type PromiseOrEager } from "../../src/util/eager.js";

// ---------------------------------------------------------------------------
// Test helpers — Eager-native codecs (return Eager, not Promise)
// ---------------------------------------------------------------------------

interface SimpleEntity extends Entity {
	readonly value: number;
}

const SIMPLE_TYPE_ID = FxtrID.builtin("test-simple-type");

const simpleLoadCodec: EntityLoadCodec<SimpleEntity> = {
	supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Eager<boolean> {
		return Eager.resolve(typeId.key() === SIMPLE_TYPE_ID.key());
	},
	decode(cbor: FxtrCbor, _scope: FxtrScope): Eager<SimpleEntity> {
		const map = cbor as Record<string, unknown>;
		const value = map.value as number;
		const obj = { value } as Record<string, unknown>;
		Object.defineProperty(obj, FXTR_CODEC, { value: simpleStoreCodec, enumerable: false });
		Object.defineProperty(obj, FXTR_CACHEABLE, { value: true, enumerable: false });
		Object.freeze(obj);
		return Eager.resolve(obj as unknown as SimpleEntity);
	},
};

const simpleStoreCodec: EntityStoreCodec<SimpleEntity> = {
	store(entity: SimpleEntity, scope: FxtrScope): PromiseOrEager<EntityRef<SimpleEntity>> {
		scope.trackBuiltin(SIMPLE_TYPE_ID);
		return scope
			.storeCbor({
				"$fxtr.type": new RawWeakFxtrID(SIMPLE_TYPE_ID),
				"$fxtr.ctx": new RawFxtrEntityMarkerID(scope.getContextMarkerId()),
				value: entity.value,
			})
			.then((id) => new EntityRef(id, simpleLoadCodec));
	},
};

function makeSimple(value: number): SimpleEntity {
	const obj = { value } as Record<string, unknown>;
	Object.defineProperty(obj, FXTR_CODEC, { value: simpleStoreCodec, enumerable: false });
	return obj as unknown as SimpleEntity;
}

// ---------------------------------------------------------------------------
// Arena layer: MemoryArena.load returns Eager for non-inherited nodes
// ---------------------------------------------------------------------------

describe("arena eager resolution", () => {
	it("MemoryArena.load returns Eager for a stored non-inherited node", async () => {
		const { MemoryStore } = await import("../../src/arena/memoryArena.js");
		const store = new MemoryStore();
		const arena = store.newEmptyArena();

		// Store an entity (async due to SHA-256)
		const ctxId = FxtrID.here();
		arena.trackBuiltin(ctxId);
		const typeId = FxtrID.builtin("test-type");
		arena.trackBuiltin(typeId);
		const entityId = await arena.store({
			"$fxtr.type": new RawWeakFxtrID(typeId),
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ctxId),
			value: 42,
		});

		// Now load from the arena — should be Eager since the node is stored and non-inherited
		const loadResult = arena.load(entityId);
		expect(loadResult).toBeInstanceOf(Eager);

		arena.close();
	});
});

// ---------------------------------------------------------------------------
// Scope.load returns Eager on cache hit
// ---------------------------------------------------------------------------

describe("scope.load eager resolution", () => {
	it("returns Eager on cache hit (second load of same ref)", async () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });

		// Store and load once (first load is async — codec.decode needs to be called, type check, etc.)
		const entity = makeSimple(99);
		const ref = await entity[FXTR_CODEC].store(entity, scope);
		await scope.load(ref);

		// Second load should hit the cache and return Eager
		const secondLoad = scope.load(ref);
		expect(secondLoad).toBeInstanceOf(Eager);
		expect((secondLoad as Eager<SimpleEntity>).unwrap().value).toBe(99);

		scope.scheduleClose();
	});

	it("returns Eager on cache miss for in-memory entity with Eager-native codec", async () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });

		// Store entity
		const entity = makeSimple(77);
		const ref = await entity[FXTR_CODEC].store(entity, scope);

		// First load — codec returns Eager, arena returns Eager → whole load should be Eager
		const loadResult = scope.load(ref);
		expect(loadResult).toBeInstanceOf(Eager);
		expect((loadResult as Eager<SimpleEntity>).unwrap().value).toBe(77);

		scope.scheduleClose();
	});
});

// ---------------------------------------------------------------------------
// Bicodec field codecs return Eager
// ---------------------------------------------------------------------------

describe("bicodec eager resolution", () => {
	it("scalar field codecs return Eager", async () => {
		// Import bicodec
		const bicodec = await import("../../src/common/bicodec.js");
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });

		const stringCodec = bicodec.string();
		const numberCodec = bicodec.number();
		const booleanCodec = bicodec.boolean();

		expect(stringCodec.toCbor("hello", scope)).toBeInstanceOf(Eager);
		expect(stringCodec.fromCbor("hello", scope)).toBeInstanceOf(Eager);
		expect(numberCodec.toCbor(42, scope)).toBeInstanceOf(Eager);
		expect(numberCodec.fromCbor(42, scope)).toBeInstanceOf(Eager);
		expect(booleanCodec.toCbor(true, scope)).toBeInstanceOf(Eager);
		expect(booleanCodec.fromCbor(true, scope)).toBeInstanceOf(Eager);

		scope.scheduleClose();
	});

	it("object codec with all-scalar fields returns Eager", async () => {
		const bicodec = await import("../../src/common/bicodec.js");
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });

		const objCodec = bicodec.object({
			name: bicodec.string(),
			count: bicodec.number(),
		});

		const toCborResult = objCodec.toCbor({ name: "test", count: 5 }, scope);
		expect(toCborResult).toBeInstanceOf(Eager);

		const fromCborResult = objCodec.fromCbor({ name: "test", count: 5 }, scope);
		expect(fromCborResult).toBeInstanceOf(Eager);

		scope.scheduleClose();
	});
});

// ---------------------------------------------------------------------------
// Dispose returns Eager when no child holds
// ---------------------------------------------------------------------------

describe("dispose eager resolution", () => {
	it("dispose returns Eager for a childless scope", () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });

		// No child scopes → dispose should be sync
		const disposeResult = scope.dispose();
		expect(disposeResult).toBeInstanceOf(Eager);
	});

	it("dispose returns Promise when children are still alive", () => {
		const parent = createFxtrScope({ include: [GLOBAL_SCOPE] });
		const _child = createFxtrScope({ include: [parent] });

		// Child holds parent alive → dispose returns Promise
		const disposeResult = parent.dispose();
		expect(disposeResult).toBeInstanceOf(Promise);

		// Clean up
		_child.scheduleClose();
	});
});

// ---------------------------------------------------------------------------
// withFxtrScope returns Eager when callback and dispose are both sync
// ---------------------------------------------------------------------------

describe("withFxtrScope eager resolution", () => {
	it("returns Eager when callback returns Eager and scope has no children", () => {
		const result = withFxtrScope({ include: [GLOBAL_SCOPE] }, (_scope) => {
			return Eager.resolve(42);
		});
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<number>).unwrap()).toBe(42);
	});
});
