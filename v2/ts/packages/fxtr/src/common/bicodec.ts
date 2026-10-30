/**
 * Declarative codec builder for defining fxtr entity types in TypeScript.
 *
 * Provides two layers:
 * 1. Field codecs (FieldCodec<T>) — bidirectional converters between TS values
 *    and FxtrCbor fragments.
 * 2. Entity definitions (EntityDefinition<T>) — full store/load codec pairs
 *    with $fxtr.type/$fxtr.ctx metadata. EntityDefinition extends EntityLoadCodec
 *    directly, so definitions can be used wherever a load codec is expected.
 *
 * Inspired by ts-jsonbinding's bidirectional binding pattern.
 *
 * TODO(future): Consider a strict mode that validates field values at store
 * time (e.g., checking that a string field actually receives a string).
 * TypeScript catches this at compile time, but runtime validation would catch
 * issues from `as` casts or raw CBOR manipulation. Python's mashumaro also
 * doesn't validate on store, so this is consistent for now.
 */

import {
	type FxtrCbor,
	FxtrID,
	RawFxtrEntityMarkerID,
	RawStrongFxtrID,
	RawWeakFxtrID,
	deepFreeze,
} from "../core/entityCore.js";
import {
	type Entity,
	type EntityLoadCodec,
	EntityRef,
	type EntityStoreCodec,
	FXTR_CACHEABLE,
	FXTR_CODEC,
} from "../scope/entityRef.js";
import type { FxtrScope } from "../scope/scope.js";
import { Eager, type PromiseOrEager } from "../util/eager.js";

// ---------------------------------------------------------------------------
// FieldCodec interface
// ---------------------------------------------------------------------------

/** Result of decoding a single field. */
export interface DecodedField<T> {
	readonly value: T;
	/**
	 * Whether this value is deeply frozen. The entity codec ANDs all field
	 * flags to determine whether the loaded entity is cacheable.
	 */
	readonly frozen: boolean;
}

/**
 * Bidirectional converter between a TypeScript value and an FxtrCbor fragment.
 */
export interface FieldCodec<T> {
	/** Identifies what kind of codec this is. Used by pyCompatSet for sort
	 *  order and key extraction, and useful for debugging. */
	readonly kind: string;

	/** Encode a TS value to a CBOR-ready value. */
	toCbor(value: T, scope: FxtrScope): PromiseOrEager<FxtrCbor>;

	/** Decode a CBOR value to a TS value. */
	fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<T>>;
}

/** Extract the value type from a FieldCodec. */
export type InferField<C> = C extends FieldCodec<infer T> ? T : never;

// ---------------------------------------------------------------------------
// FXTR_CTX symbol
// ---------------------------------------------------------------------------

/** Symbol for preserving the context marker from load → re-store. */
export const FXTR_CTX: unique symbol = Symbol("fxtrCtx");

// ---------------------------------------------------------------------------
// EntityDefinition + EntityInstance
// ---------------------------------------------------------------------------

/**
 * Force TypeScript to structurally expand a type alias in tooltips.
 * Used on create()'s parameter so the IDE shows the actual fields needed,
 * while return types preserve the named alias.
 */
type Expand<T> = T extends infer U ? { [K in keyof U]: U[K] } : never;

/** Symbol for recovering the fields type from an EntityInstance. Type-level only. */
declare const __PHANTOM_ENTITY_FIELDS: unique symbol;

/**
 * An entity definition: combines EntityLoadCodec with a factory method and type ID.
 * Extends EntityLoadCodec directly, so definitions can be used wherever a load codec
 * is expected (no .loadCodec indirection needed).
 *
 * Parameterized on the full instance type `I` (not just fields).
 */
export interface EntityDefinition<I extends EntityInstance<unknown>> extends EntityLoadCodec<I> {
	/** Create a mutable entity instance. Synchronous. */
	create(fields: Expand<FieldsOf<I>>): I;

	/** The type ID. May be a Promise for defineType-based types. */
	readonly typeId: FxtrID | Promise<FxtrID>;
}

interface _EntityInstance<T> extends Entity {
	readonly [FXTR_CTX]?: FxtrID;
	store(scope: FxtrScope): PromiseOrEager<EntityRef<this>>;

	// Phantom type carrier. Just used to mark the type of the fields, not used at runtime.
	readonly [__PHANTOM_ENTITY_FIELDS]?: T;
}

/** An entity instance: user fields + Entity interface + store convenience + optional ctx. */
export type EntityInstance<T> = T & _EntityInstance<T>;

/** Extract the fields type `T` from an `EntityInstance<T>`. */
export type FieldsOf<I> = I extends { readonly [__PHANTOM_ENTITY_FIELDS]?: infer T } ? T : never;

/**
 * Extract the instance type from an EntityDefinition.
 *
 * Use with `interface extends` to create a named type alias that TypeScript
 * preserves in tooltips (unlike `type =` which gets expanded for conditional types):
 *
 * ```typescript
 * const _MyEntity = bicodec.entity(defineType("ns", "MyEntity"), {
 *     name: bicodec.string(),
 *     value: bicodec.number(),
 * });
 * export interface MyEntity extends bicodec.InferEntity<typeof _MyEntity> {}
 * export const MyEntity: bicodec.EntityDefinition<MyEntity> = _MyEntity;
 * ```
 *
 * This gives you:
 * - `MyEntity.create(...)` shows expanded fields in the parameter, `MyEntity` in the return
 * - `EntityRef<MyEntity>` everywhere downstream instead of `EntityRef<EntityInstance<...>>`
 */
export type InferEntity<D> = D extends EntityDefinition<infer I> ? I : never;

// ---------------------------------------------------------------------------
// Scalar codecs
// ---------------------------------------------------------------------------

const STRING_CODEC: FieldCodec<string> = {
	kind: "string",
	toCbor(value: string): Eager<FxtrCbor> {
		return Eager.resolve(value);
	},
	fromCbor(cbor: FxtrCbor): Eager<DecodedField<string>> {
		return Eager.resolve({ value: cbor as string, frozen: true });
	},
};

const NUMBER_CODEC: FieldCodec<number> = {
	kind: "number",
	toCbor(value: number): Eager<FxtrCbor> {
		return Eager.resolve(value);
	},
	fromCbor(cbor: FxtrCbor): Eager<DecodedField<number>> {
		return Eager.resolve({ value: cbor as number, frozen: true });
	},
};

const BOOLEAN_CODEC: FieldCodec<boolean> = {
	kind: "boolean",
	toCbor(value: boolean): Eager<FxtrCbor> {
		return Eager.resolve(value);
	},
	fromCbor(cbor: FxtrCbor): Eager<DecodedField<boolean>> {
		return Eager.resolve({ value: cbor as boolean, frozen: true });
	},
};

export function string(): FieldCodec<string> {
	return STRING_CODEC;
}

export function number(): FieldCodec<number> {
	return NUMBER_CODEC;
}

export function boolean(): FieldCodec<boolean> {
	return BOOLEAN_CODEC;
}

export function bytes(opts?: { promiseImmutable?: boolean }): FieldCodec<Uint8Array> {
	const frozen = opts?.promiseImmutable ?? false;
	return {
		kind: "bytes",
		toCbor(value: Uint8Array): Eager<FxtrCbor> {
			return Eager.resolve(value);
		},
		fromCbor(cbor: FxtrCbor): Eager<DecodedField<Uint8Array>> {
			// Uint8Array cannot be Object.freeze()'d (throws TypeError).
			// If promiseImmutable is set, the caller promises not to mutate the
			// buffer, so we report frozen: true. Otherwise frozen: false, which
			// means entities containing bytes fields won't be cacheable by default.
			return Eager.resolve({ value: cbor as Uint8Array, frozen });
		},
	};
}

/** Passthrough: any FxtrCbor value, deep-frozen on decode for runtime safety. */
export function unknown(): FieldCodec<FxtrCbor> {
	return {
		kind: "unknown",
		toCbor(value: FxtrCbor): Eager<FxtrCbor> {
			return Eager.resolve(value);
		},
		fromCbor(cbor: FxtrCbor): Eager<DecodedField<FxtrCbor>> {
			return Eager.resolve({ value: deepFreeze(cbor), frozen: true });
		},
	};
}

// ---------------------------------------------------------------------------
// FxtrID codecs
// ---------------------------------------------------------------------------

/** Weak ID reference (CBOR tag 33001). */
export function fxtrId(): FieldCodec<FxtrID> {
	return {
		kind: "fxtrId",
		toCbor(value: FxtrID): Eager<FxtrCbor> {
			return Eager.resolve(new RawWeakFxtrID(value));
		},
		fromCbor(cbor: FxtrCbor): Eager<DecodedField<FxtrID>> {
			const ref = cbor as RawWeakFxtrID;
			return Eager.resolve({ value: ref.innerId, frozen: true });
		},
	};
}

/** Strong/owning ID reference (CBOR tag 33002). */
export function childFxtrId(): FieldCodec<FxtrID> {
	return {
		kind: "childFxtrId",
		toCbor(value: FxtrID): Eager<FxtrCbor> {
			return Eager.resolve(new RawStrongFxtrID(value));
		},
		fromCbor(cbor: FxtrCbor): Eager<DecodedField<FxtrID>> {
			const ref = cbor as RawStrongFxtrID;
			return Eager.resolve({ value: ref.innerId, frozen: true });
		},
	};
}

// ---------------------------------------------------------------------------
// Entity ref codecs
// ---------------------------------------------------------------------------

/** Weak entity reference. Serialized as RawWeakFxtrID (tag 33001). */
export function entityRef<T extends Entity>(codec: EntityLoadCodec<T>): FieldCodec<EntityRef<T>> {
	return {
		kind: "entityRef",
		toCbor(value: EntityRef<T>): Eager<FxtrCbor> {
			return Eager.resolve(new RawWeakFxtrID(value.entityId));
		},
		fromCbor(cbor: FxtrCbor): Eager<DecodedField<EntityRef<T>>> {
			const ref = cbor as RawWeakFxtrID;
			return Eager.resolve({ value: new EntityRef(ref.innerId, codec), frozen: true });
		},
	};
}

/** Strong/owning entity reference. Serialized as RawStrongFxtrID (tag 33002). */
export function childRef<T extends Entity>(codec: EntityLoadCodec<T>): FieldCodec<EntityRef<T>> {
	return {
		kind: "childRef",
		toCbor(value: EntityRef<T>): Eager<FxtrCbor> {
			return Eager.resolve(new RawStrongFxtrID(value.entityId));
		},
		fromCbor(cbor: FxtrCbor): Eager<DecodedField<EntityRef<T>>> {
			const ref = cbor as RawStrongFxtrID;
			return Eager.resolve({ value: new EntityRef(ref.innerId, codec), frozen: true });
		},
	};
}

// ---------------------------------------------------------------------------
// Composite codecs
// ---------------------------------------------------------------------------

/** Fixed-schema nested object (not an entity — no $fxtr.type). */
export function object<S extends Record<string, FieldCodec<unknown>>>(
	schema: S,
): FieldCodec<{ [K in keyof S]: InferField<S[K]> }> {
	type T = { [K in keyof S]: InferField<S[K]> };
	const keys = Object.keys(schema);
	return {
		kind: "object",
		toCbor(value: T, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
			const val = value as Record<string, unknown>;
			return Eager.all(
				// biome-ignore lint/style/noNonNullAssertion: key from Object.keys(schema)
				keys.map((key) => schema[key]!.toCbor(val[key], scope)),
			).then((encoded) => {
				const result: Record<string, FxtrCbor> = {};
				for (let i = 0; i < keys.length; i++) {
					result[keys[i] as string] = encoded[i] as FxtrCbor;
				}
				return result;
			});
		},
		fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<T>> {
			const map = cbor as Record<string, FxtrCbor>;
			return Eager.all(
				// biome-ignore lint/style/noNonNullAssertion: key from Object.keys(schema/map)
				keys.map((key) => schema[key]!.fromCbor(map[key]!, scope)),
			).then((decodeds) => {
				const result: Record<string, unknown> = {};
				let allFrozen = true;
				for (let i = 0; i < keys.length; i++) {
					const decoded = decodeds[i] as DecodedField<unknown>;
					result[keys[i] as string] = decoded.value;
					if (!decoded.frozen) allFrozen = false;
				}
				Object.freeze(result);
				return { value: result as T, frozen: allFrozen };
			});
		},
	};
}

/** Ordered collection. */
export function array<T>(itemCodec: FieldCodec<T>): FieldCodec<T[]> {
	return {
		kind: "array",
		toCbor(value: T[], scope: FxtrScope): PromiseOrEager<FxtrCbor> {
			return Eager.all(value.map((item) => itemCodec.toCbor(item, scope))).then((encoded) => {
				return encoded as FxtrCbor[];
			});
		},
		fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<T[]>> {
			const arr = cbor as FxtrCbor[];
			return Eager.all(arr.map((item) => itemCodec.fromCbor(item, scope))).then((decodeds) => {
				const result: T[] = [];
				let allFrozen = true;
				for (const decoded of decodeds) {
					result.push(decoded.value);
					if (!decoded.frozen) allFrozen = false;
				}
				Object.freeze(result);
				return { value: result, frozen: allFrozen };
			});
		},
	};
}

/** String-keyed map. Serialized as a CBOR map. */
export function record<V>(valueCodec: FieldCodec<V>): FieldCodec<Record<string, V>> {
	return {
		kind: "record",
		toCbor(value: Record<string, V>, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
			const entries = Object.entries(value);
			return Eager.all(entries.map(([, v]) => valueCodec.toCbor(v, scope))).then((encoded) => {
				const result: Record<string, FxtrCbor> = {};
				for (let i = 0; i < entries.length; i++) {
					result[(entries[i] as [string, V])[0]] = encoded[i] as FxtrCbor;
				}
				return result;
			});
		},
		fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<Record<string, V>>> {
			const entries = Object.entries(cbor as Record<string, FxtrCbor>);
			return Eager.all(entries.map(([, v]) => valueCodec.fromCbor(v, scope))).then((decodeds) => {
				const result: Record<string, V> = {};
				let allFrozen = true;
				for (let i = 0; i < entries.length; i++) {
					const decoded = decodeds[i] as DecodedField<V>;
					result[(entries[i] as [string, FxtrCbor])[0]] = decoded.value;
					if (!decoded.frozen) allFrozen = false;
				}
				Object.freeze(result);
				return { value: result, frozen: allFrozen };
			});
		},
	};
}

/** Nullable field (CBOR null). */
export function nullable<T>(codec: FieldCodec<T>): FieldCodec<T | null> {
	return {
		kind: "nullable",
		toCbor(value: T | null, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
			if (value === null) return Eager.resolve(null);
			return codec.toCbor(value, scope);
		},
		fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<T | null>> {
			if (cbor === null) return Eager.resolve({ value: null, frozen: true });
			return codec.fromCbor(cbor, scope) as PromiseOrEager<DecodedField<T | null>>;
		},
	};
}

// ---------------------------------------------------------------------------
// Combinators
// ---------------------------------------------------------------------------

/** Transform between a CBOR representation type A and a TS-side type B. */
export function mapped<A, B>(
	inner: FieldCodec<A>,
	toTs: (a: A) => B,
	fromTs: (b: B) => A,
	opts?: { frozen?: boolean },
): FieldCodec<B> {
	const frozenFlag = opts?.frozen ?? false;
	return {
		kind: "mapped",
		toCbor(value: B, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
			return inner.toCbor(fromTs(value), scope);
		},
		fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<B>> {
			return inner.fromCbor(cbor, scope).then((decoded) => {
				return { value: toTs(decoded.value), frozen: frozenFlag };
			});
		},
	};
}

/** Deferred codec evaluation for recursive types. */
export function lazy<T>(fn: () => FieldCodec<T>): FieldCodec<T> {
	let cached: FieldCodec<T> | null = null;
	function resolve(): FieldCodec<T> {
		if (cached === null) {
			cached = fn();
		}
		return cached;
	}
	return {
		kind: "lazy",
		toCbor(value: T, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
			return resolve().toCbor(value, scope);
		},
		fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<T>> {
			return resolve().fromCbor(cbor, scope);
		},
	};
}

// ---------------------------------------------------------------------------
// Inline child codecs
// ---------------------------------------------------------------------------

/** Inline entity — strong link, auto-stored on store, auto-loaded on load. */
export function child<T extends Entity>(codec: EntityLoadCodec<T>): FieldCodec<T> {
	return {
		kind: "child",
		toCbor(value: T, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
			return value[FXTR_CODEC].store(value, scope).then((ref) => {
				return new RawStrongFxtrID(ref.entityId);
			});
		},
		fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<T>> {
			const raw = cbor as RawStrongFxtrID;
			const ref = new EntityRef<T>(raw.innerId, codec);
			return scope.load(ref).then((loaded) => {
				const frozen = loaded[FXTR_CACHEABLE] === true;
				return { value: loaded, frozen };
			});
		},
	};
}

/** Inline entity — weak link, auto-stored on store, auto-loaded on load. */
export function linked<T extends Entity>(codec: EntityLoadCodec<T>): FieldCodec<T> {
	return {
		kind: "linked",
		toCbor(value: T, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
			return value[FXTR_CODEC].store(value, scope).then((ref) => {
				return new RawWeakFxtrID(ref.entityId);
			});
		},
		fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<T>> {
			const raw = cbor as RawWeakFxtrID;
			const ref = new EntityRef<T>(raw.innerId, codec);
			return scope.load(ref).then((loaded) => {
				const frozen = loaded[FXTR_CACHEABLE] === true;
				return { value: loaded, frozen };
			});
		},
	};
}

// ---------------------------------------------------------------------------
// entity() — EntityDefinition factory
// ---------------------------------------------------------------------------

/**
 * Create an EntityDefinition from a type ID and a field schema.
 *
 * The returned definition implements EntityLoadCodec directly (supportsEntityType + decode),
 * so it can be passed anywhere a load codec is expected.
 *
 * **Recommended pattern** for named types with good IDE tooltip display:
 *
 * ```typescript
 * const _MyEntity = bicodec.entity(defineType("ns", "MyEntity"), {
 *     name: bicodec.string(),
 *     value: bicodec.number(),
 * });
 * export interface MyEntity extends bicodec.InferEntity<typeof _MyEntity> {}
 * export const MyEntity: bicodec.EntityDefinition<MyEntity> = _MyEntity;
 * ```
 *
 * The `_`-prefixed const holds the inferred definition. The `interface extends`
 * extracts the instance type and gives it a name that TypeScript always preserves
 * in tooltips. The exported const is annotated so downstream generics (return
 * types, `EntityRef<MyEntity>`, etc.) use the named type.
 */
export function entity<S extends Record<string, FieldCodec<unknown>>>(
	typeId: FxtrID | PromiseOrEager<FxtrID>,
	schema: S,
): EntityDefinition<EntityInstance<{ [K in keyof S]: InferField<S[K]> }>> {
	type Fields = { [K in keyof S]: InferField<S[K]> };
	type Instance = EntityInstance<Fields>;

	// Cache the resolved type ID
	let resolvedTypeId: FxtrID | null = null;
	const typeIdPromiseOrEager: PromiseOrEager<FxtrID> =
		typeId instanceof FxtrID ? Eager.resolve(typeId) : typeId;

	function getTypeId(): PromiseOrEager<FxtrID> {
		if (resolvedTypeId !== null) return Eager.resolve(resolvedTypeId);
		return typeIdPromiseOrEager.then((tid) => {
			resolvedTypeId = tid;
			return tid;
		});
	}
	// If typeId is already a FxtrID (not a Promise), resolve immediately
	if (typeId instanceof FxtrID) {
		resolvedTypeId = typeId;
	}

	const keys = Object.keys(schema);
	const storeCodec: EntityStoreCodec<Instance> = {
		store(value: Instance, scope: FxtrScope): PromiseOrEager<EntityRef<Instance>> {
			return getTypeId().then((tid) => {
				// Lift the type ID into this scope (must be in a parent, e.g. GLOBAL_SCOPE)
				scope.lift(tid);

				// Encode fields
				const val = value as Record<string, unknown>;
				return Eager.all(
					// biome-ignore lint/style/noNonNullAssertion: key from Object.keys(schema)
					keys.map((key) => schema[key]!.toCbor(val[key], scope)),
				).then((encodedFields) => {
					const cborMap: Record<string, FxtrCbor> = {};
					for (let i = 0; i < keys.length; i++) {
						cborMap[keys[i] as string] = encodedFields[i] as FxtrCbor;
					}

					// Add $fxtr.type
					cborMap["$fxtr.type"] = new RawWeakFxtrID(tid);

					// Add $fxtr.ctx: reuse from loaded entity, or get fresh from scope
					const existingCtx = (value as unknown as Record<symbol, unknown>)[FXTR_CTX] as
						| FxtrID
						| undefined;
					if (existingCtx !== undefined) {
						cborMap["$fxtr.ctx"] = new RawFxtrEntityMarkerID(existingCtx);
					} else {
						cborMap["$fxtr.ctx"] = new RawFxtrEntityMarkerID(scope.getContextMarkerId());
					}

					return scope.storeCbor(cborMap).then((storedId) => {
						return new EntityRef<Instance>(storedId, definition);
					});
				});
			});
		},
	};

	// FieldsOf<Instance> is structurally Fields, but TS can't resolve this
	// through the conditional type at the generic level — hence the assertion.
	const definition = {
		create(fields: Expand<Fields>): Instance {
			const instance = { ...fields } as Record<string, unknown>;
			Object.defineProperty(instance, FXTR_CODEC, {
				value: storeCodec,
				enumerable: false,
				writable: false,
				configurable: false,
			});
			Object.defineProperty(instance, "store", {
				value(this: Instance, s: FxtrScope) {
					return s.store(this);
				},
				enumerable: false,
				writable: false,
				configurable: false,
			});
			return instance as Instance;
		},

		typeId,

		// EntityLoadCodec implementation (directly on the definition)
		supportsEntityType(typeIdToCheck: FxtrID, _scope: FxtrScope): PromiseOrEager<boolean> {
			return getTypeId().then((tid) => typeIdToCheck.key() === tid.key());
		},

		decode(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<Instance> {
			const map = cbor as Record<string, FxtrCbor>;

			// Validate all fields exist before decoding
			for (const key of keys) {
				if (map[key] === undefined) {
					throw new Error(`Missing field "${key}" in entity CBOR`);
				}
			}

			// Decode user fields (skip $fxtr.type and $fxtr.ctx)
			return Eager.all(
				// biome-ignore lint/style/noNonNullAssertion: key from Object.keys(schema), validated above
				keys.map((key) => schema[key]!.fromCbor(map[key]!, scope)),
			).then((decodeds) => {
				const fields: Record<string, unknown> = {};
				let allFrozen = true;
				for (let i = 0; i < keys.length; i++) {
					const decoded = decodeds[i] as DecodedField<unknown>;
					fields[keys[i] as string] = decoded.value;
					if (!decoded.frozen) allFrozen = false;
				}
				// TODO(revisit): Unknown CBOR keys are silently dropped on re-store.
				// This means load-then-store is not a perfect round-trip if extra keys exist.

				// Extract context marker
				const ctxRaw = map["$fxtr.ctx"] as RawFxtrEntityMarkerID | undefined;
				const ctxId = ctxRaw?.innerId;

				// Build instance
				const instance = fields as Record<string, unknown>;
				Object.defineProperty(instance, FXTR_CODEC, {
					value: storeCodec,
					enumerable: false,
					writable: false,
					configurable: false,
				});
				if (ctxId !== undefined) {
					Object.defineProperty(instance, FXTR_CTX, {
						value: ctxId,
						enumerable: false,
						writable: false,
						configurable: false,
					});
				}
				Object.defineProperty(instance, "store", {
					value(this: Instance, s: FxtrScope) {
						return s.store(this);
					},
					enumerable: false,
					writable: false,
					configurable: false,
				});
				Object.defineProperty(instance, FXTR_CACHEABLE, {
					value: allFrozen,
					enumerable: false,
					writable: false,
					configurable: false,
				});

				if (allFrozen) {
					Object.freeze(instance);
				}

				return instance as Instance;
			});
		},
	};

	return definition as EntityDefinition<Instance>;
}
