/**
 * Tests for EntityRef, load codecs, unknown codecs, and unionLoadCodec.
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
	UnknownEntity,
	unknownLoadCodec,
} from "../../src/scope/entityRef.js";
import { type FxtrScope, GLOBAL_SCOPE, withFxtrScope } from "../../src/scope/scope.js";
import { unionLoadCodec } from "../../src/scope/unionLoadCodec.js";

// ---------------------------------------------------------------------------
// Test helpers: a simple "counter" entity with a hand-rolled codec
// ---------------------------------------------------------------------------

interface CounterEntity extends Entity {
	readonly count: number;
}

/** A fake type ID for counter entities. */
const COUNTER_TYPE_ID = FxtrID.builtin("test-counter-type");

const counterLoadCodec: EntityLoadCodec<CounterEntity> = {
	async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
		return typeId.key() === COUNTER_TYPE_ID.key();
	},
	async decode(cbor: FxtrCbor, _scope: FxtrScope): Promise<CounterEntity> {
		const count = (cbor as Record<string, unknown>).count as number;
		return makeCounter(count, true);
	},
};

const counterStoreCodec: EntityStoreCodec<CounterEntity> = {
	async store(value: CounterEntity, scope: FxtrScope): Promise<EntityRef<CounterEntity>> {
		const typeRef = new RawWeakFxtrID(COUNTER_TYPE_ID);
		const ctxMarker = new RawFxtrEntityMarkerID(scope.getContextMarkerId());
		scope.trackBuiltin(COUNTER_TYPE_ID);
		const id = await scope.storeCbor({
			"$fxtr.type": typeRef,
			"$fxtr.ctx": ctxMarker,
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

// ---------------------------------------------------------------------------
// A second entity type for union testing
// ---------------------------------------------------------------------------

interface LabelEntity extends Entity {
	readonly label: string;
}

const LABEL_TYPE_ID = FxtrID.builtin("test-label-type");

const labelLoadCodec: EntityLoadCodec<LabelEntity> = {
	async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
		return typeId.key() === LABEL_TYPE_ID.key();
	},
	async decode(cbor: FxtrCbor, _scope: FxtrScope): Promise<LabelEntity> {
		const label = (cbor as Record<string, unknown>).label as string;
		return makeLabel(label, true);
	},
};

const labelStoreCodec: EntityStoreCodec<LabelEntity> = {
	async store(value: LabelEntity, scope: FxtrScope): Promise<EntityRef<LabelEntity>> {
		const typeRef = new RawWeakFxtrID(LABEL_TYPE_ID);
		const ctxMarker = new RawFxtrEntityMarkerID(scope.getContextMarkerId());
		scope.trackBuiltin(LABEL_TYPE_ID);
		const id = await scope.storeCbor({
			"$fxtr.type": typeRef,
			"$fxtr.ctx": ctxMarker,
			label: value.label,
		});
		return new EntityRef(id, labelLoadCodec);
	},
};

function makeLabel(label: string, cacheable = false): LabelEntity {
	const obj = { label } as Record<string, unknown>;
	Object.defineProperty(obj, FXTR_CODEC, { value: labelStoreCodec, enumerable: false });
	if (cacheable) {
		Object.defineProperty(obj, FXTR_CACHEABLE, { value: true, enumerable: false });
	}
	return obj as unknown as LabelEntity;
}

// ---------------------------------------------------------------------------
// EntityRef tests
// ---------------------------------------------------------------------------

describe("EntityRef", () => {
	it("equals compares by entity ID and codec identity", () => {
		const id = FxtrID.builtin("test");
		const ref1 = new EntityRef(id, counterLoadCodec);
		const ref2 = new EntityRef(id, counterLoadCodec);
		const ref3 = new EntityRef(id, labelLoadCodec);

		expect(ref1.equals(ref2)).toBe(true);
		expect(ref1.equals(ref3)).toBe(false);
	});

	it("withLoadCodec changes the codec but keeps the ID", () => {
		const id = FxtrID.builtin("test");
		const ref1 = new EntityRef(id, counterLoadCodec);
		const ref2 = ref1.withLoadCodec(labelLoadCodec);

		expect(ref2.entityId.key()).toBe(ref1.entityId.key());
		expect(ref2.loadCodec).toBe(labelLoadCodec);
		expect(ref1.equals(ref2)).toBe(false);
	});

	it("toString returns a readable string", () => {
		const id = FxtrID.builtin("test");
		const ref = new EntityRef(id, counterLoadCodec);
		expect(ref.toString()).toContain("EntityRef");
	});
});

// ---------------------------------------------------------------------------
// unknownLoadCodec tests
// ---------------------------------------------------------------------------

describe("unknownLoadCodec", () => {
	it("supportsEntityType returns true for any type", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const result = await unknownLoadCodec.supportsEntityType(FxtrID.builtin("anything"), scope);
			expect(result).toBe(true);
		});
	});

	it("decode returns UnknownEntity with frozen CBOR and FXTR_CACHEABLE=true", async () => {
		const cbor = {
			"$fxtr.type": new RawWeakFxtrID(COUNTER_TYPE_ID),
			"$fxtr.ctx": new RawFxtrEntityMarkerID(FxtrID.fromTagAndPayload(".", new Uint8Array(0))),
			count: 42,
		};
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const result = await unknownLoadCodec.decode(cbor, scope);
			expect(result).toBeInstanceOf(UnknownEntity);
			expect(result[FXTR_CACHEABLE]).toBe(true);
			expect(Object.isFrozen(result.raw_cbor)).toBe(true);
		});
	});
});

// ---------------------------------------------------------------------------
// Store/load round-trip tests
// ---------------------------------------------------------------------------

describe("store/load round-trip", () => {
	it("store and load a counter entity", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const entity = makeCounter(42);
			const ref = await scope.store(entity);

			expect(ref.entityId).toBeDefined();
			expect(ref.loadCodec).toBe(counterLoadCodec);

			const loaded = await scope.load(ref);
			expect(loaded.count).toBe(42);
		});
	});

	it("store and load a label entity", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const entity = makeLabel("hello");
			const ref = await scope.store(entity);
			const loaded = await scope.load(ref);
			expect(loaded.label).toBe("hello");
		});
	});

	it("loading the same ref twice returns cached value", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(7));
			const v1 = await scope.load(ref);
			const v2 = await scope.load(ref);
			// Same object identity (cached).
			expect(v1).toBe(v2);
		});
	});

	it("loadCbor returns CBOR (cached, deep-frozen)", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(99));
			const cbor = await scope.loadCbor(ref.entityId);
			expect(typeof cbor).toBe("object");
			expect((cbor as Record<string, unknown>).count).toBe(99);
			// loadCbor now uses unknownLoadCodec which deep-freezes
			expect(Object.isFrozen(cbor)).toBe(true);
		});
	});

	it("storing the same entity twice produces the same ID", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref1 = await scope.store(makeCounter(42));
			const ref2 = await scope.store(makeCounter(42));
			expect(ref1.entityId.key()).toBe(ref2.entityId.key());
		});
	});

	it("ref.load(scope) convenience works", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(10));
			const loaded = await ref.load(scope);
			expect(loaded.count).toBe(10);
		});
	});
});

// ---------------------------------------------------------------------------
// unionLoadCodec tests
// ---------------------------------------------------------------------------

describe("unionLoadCodec", () => {
	const unionCodec = unionLoadCodec(counterLoadCodec, labelLoadCodec);

	it("supportsEntityType returns true for counter type", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			expect(await unionCodec.supportsEntityType(COUNTER_TYPE_ID, scope)).toBe(true);
		});
	});

	it("supportsEntityType returns true for label type", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			expect(await unionCodec.supportsEntityType(LABEL_TYPE_ID, scope)).toBe(true);
		});
	});

	it("supportsEntityType returns false for unknown type", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			expect(await unionCodec.supportsEntityType(FxtrID.builtin("unknown"), scope)).toBe(false);
		});
	});

	it("decodes counter entity through union codec", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeCounter(5));
			const unionRef = ref.withLoadCodec(unionCodec);
			const loaded = await scope.load(unionRef);
			expect((loaded as CounterEntity).count).toBe(5);
		});
	});

	it("decodes label entity through union codec", async () => {
		await withFxtrScope({ include: [GLOBAL_SCOPE] }, async (scope) => {
			const ref = await scope.store(makeLabel("world"));
			const unionRef = ref.withLoadCodec(unionCodec);
			const loaded = await scope.load(unionRef);
			expect((loaded as LabelEntity).label).toBe("world");
		});
	});
});
