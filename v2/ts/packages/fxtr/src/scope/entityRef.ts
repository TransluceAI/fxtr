/**
 * Entity codec system: EntityRef, store/load codecs, FXTR_CODEC symbol.
 *
 * This is the TS equivalent of Python's Entity base class, EntityLoadHandler,
 * EntityRef, and the global entity load registry — but without the registry.
 * Instead, load codecs are carried explicitly on EntityRef instances.
 *
 * Uses `import type` for FxtrScope (type annotations only — no runtime
 * dependency on scope.ts). unionLoadCodec lives in its own file to avoid
 * circular dependencies.
 */

import { type FxtrCbor, type FxtrID, deepFreeze } from "../core/entityCore.js";
import { Eager, type PromiseOrEager } from "../util/eager.js";
import type { FxtrScope } from "./scope.js";

// ---------------------------------------------------------------------------
// FXTR_CODEC / FXTR_CACHEABLE symbols
// ---------------------------------------------------------------------------

/** Symbol used to attach a store codec to entity objects. */
export const FXTR_CODEC: unique symbol = Symbol("fxtrCodec");

/**
 * Symbol indicating whether a loaded entity is immutable and safe to cache.
 *
 * If true, the scope will cache the value and reuse it for repeated loads
 * of the same entity ref without re-decoding. The cached value will be
 * returned by reference — it MUST NOT be mutated.
 *
 * If absent or false, each load() call will re-decode from CBOR.
 */
export const FXTR_CACHEABLE: unique symbol = Symbol("fxtrCacheable");

// ---------------------------------------------------------------------------
// Entity interface
// ---------------------------------------------------------------------------

/**
 * Base interface for all entity objects. Every entity carries a store codec
 * (for re-storing) and an optional cacheability flag (set by load codecs).
 *
 * Uses polymorphic `this` so that accessing entity[FXTR_CODEC] on a value
 * typed as T extends Entity yields EntityStoreCodec<T>.
 */
export interface Entity {
	[FXTR_CODEC]: EntityStoreCodec<this>;
	[FXTR_CACHEABLE]?: boolean;
}

// ---------------------------------------------------------------------------
// EntityStoreCodec
// ---------------------------------------------------------------------------

/**
 * Encode an entity value, store it into a scope, and return an EntityRef.
 *
 * The codec is responsible for:
 * 1. Encoding the value to FxtrCbor
 * 2. Calling scope.storeCbor(cbor) to store it
 * 3. Returning an EntityRef with an appropriate load codec attached
 *
 * Returns PromiseOrEager — usually a Promise in practice (SHA-256 is async),
 * but typed for future flexibility.
 * Mirrors Python's Entity._fxtr_store_impl() -> EntityRef[Self].
 */
export interface EntityStoreCodec<T extends Entity> {
	store(value: T, scope: FxtrScope): PromiseOrEager<EntityRef<T>>;
}

// ---------------------------------------------------------------------------
// EntityLoadCodec
// ---------------------------------------------------------------------------

export interface EntityLoadCodec<T extends Entity> {
	/**
	 * Check if this codec can handle a given $fxtr.type ID.
	 *
	 * The scope is a fresh child scope created by the caller specifically
	 * for this check — any loads performed here are isolated and won't
	 * pollute the main scope's tracking state.
	 *
	 * The result should be deterministic given its inputs and may be cached.
	 */
	supportsEntityType(typeId: FxtrID, scope: FxtrScope): PromiseOrEager<boolean>;

	/**
	 * Decode raw CBOR to a rich entity object.
	 *
	 * The scope is a tracing surface — loads through it are tracked as
	 * side effects. Stores are disallowed (runtime error).
	 *
	 * The returned object must have [FXTR_CODEC] attached (it's an Entity).
	 * Set [FXTR_CACHEABLE] = true if the value is immutable and safe to cache.
	 */
	decode(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<T>;
}

// ---------------------------------------------------------------------------
// EntityRef
// ---------------------------------------------------------------------------

export class EntityRef<T extends Entity> {
	readonly entityId: FxtrID;
	readonly loadCodec: EntityLoadCodec<T>;

	constructor(entityId: FxtrID, loadCodec: EntityLoadCodec<T>) {
		this.entityId = entityId;
		this.loadCodec = loadCodec;
	}

	/** Load the entity from the given scope. Convenience for scope.load(ref). */
	load(scope: FxtrScope): PromiseOrEager<T> {
		return scope.load(this);
	}

	/** Create a new ref with the same ID but a different load codec. */
	withLoadCodec<U extends Entity>(newCodec: EntityLoadCodec<U>): EntityRef<U> {
		return new EntityRef(this.entityId, newCodec);
	}

	/** Copy this entity between scopes. */
	copy(opts: { from: FxtrScope; to: FxtrScope }): PromiseOrEager<EntityRef<T>> {
		return opts.to.copyFromScope(this, opts.from).then(() => this);
	}

	/** Lift this entity into the given scope (must already be in a parent). */
	lift(scope: FxtrScope): EntityRef<T> {
		scope.lift(this);
		return this;
	}

	/** Value equality: same entity ID and same load codec reference. */
	equals(other: EntityRef<Entity>): boolean {
		return this.entityId.key() === other.entityId.key() && this.loadCodec === other.loadCodec;
	}

	toString(): string {
		return `<EntityRef ${this.entityId.idString()}>`;
	}
}

// ---------------------------------------------------------------------------
// UnknownEntity / UnknownMutableEntity
// ---------------------------------------------------------------------------

/**
 * Store codec for UnknownEntity — re-stores the raw CBOR.
 * Declared before the class so it can reference unknownLoadCodec (defined after).
 */
const unknownStoreCodec: EntityStoreCodec<UnknownEntity> = {
	store(value: UnknownEntity, scope: FxtrScope): PromiseOrEager<EntityRef<UnknownEntity>> {
		return scope.storeCbor(value.raw_cbor).then((storedId) => {
			return new EntityRef<UnknownEntity>(storedId, unknownLoadCodec);
		});
	},
};

/**
 * Fallback entity wrapping frozen CBOR data for an entity whose type is
 * not known. Matches Python's UnknownEntity. Cacheable.
 */
export class UnknownEntity implements Entity {
	readonly raw_cbor: FxtrCbor;
	declare readonly [FXTR_CODEC]: EntityStoreCodec<this>;
	readonly [FXTR_CACHEABLE] = true;

	constructor(raw_cbor: FxtrCbor) {
		this.raw_cbor = raw_cbor;
		Object.defineProperty(this, FXTR_CODEC, {
			value: unknownStoreCodec,
			enumerable: false,
			writable: false,
			configurable: false,
		});
		Object.freeze(this);
	}
}

// ---------------------------------------------------------------------------
// unknownLoadCodec
// ---------------------------------------------------------------------------

/** Load codec that returns an UnknownEntity wrapping deeply frozen CBOR. Cacheable. */
export const unknownLoadCodec: EntityLoadCodec<UnknownEntity> = {
	supportsEntityType(_typeId: FxtrID, _scope: FxtrScope): Eager<boolean> {
		return Eager.resolve(true);
	},

	decode(cbor: FxtrCbor, _scope: FxtrScope): Eager<UnknownEntity> {
		return Eager.resolve(new UnknownEntity(deepFreeze(cbor)));
	},
};
