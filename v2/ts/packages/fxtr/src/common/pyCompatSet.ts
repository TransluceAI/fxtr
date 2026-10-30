/**
 * PyCompatSet<T>: A set-like collection backed by Map<string, T>.
 *
 * Needed because JS Set uses reference equality, which doesn't work for
 * FxtrID or EntityRef objects. Also provides freezability for loaded entities.
 *
 * Also exports the pyCompatSet() field codec for bicodec integration.
 */

import { type FxtrCbor, type FxtrID, RawStrongFxtrID, RawWeakFxtrID } from "../core/entityCore.js";
import type { Entity, EntityRef } from "../scope/entityRef.js";
import type { FxtrScope } from "../scope/scope.js";
import { Eager, type PromiseOrEager } from "../util/eager.js";
import type { DecodedField, FieldCodec } from "./bicodec.js";

// ---------------------------------------------------------------------------
// Key functions
// ---------------------------------------------------------------------------

export type KeyFn<T> = (item: T) => string;

function hexEncode(bytes: Uint8Array): string {
	let s = "";
	for (let i = 0; i < bytes.length; i++) {
		// biome-ignore lint/style/noNonNullAssertion: i bounded by loop
		s += bytes[i]!.toString(16).padStart(2, "0");
	}
	return s;
}

export const stringKeyFn: KeyFn<string> = (v) => v;
export const numberKeyFn: KeyFn<number> = (v) => String(v);
export const bytesKeyFn: KeyFn<Uint8Array> = (v) => hexEncode(v);
export const fxtrIdKeyFn: KeyFn<FxtrID> = (v) => v.key();
export const entityRefKeyFn: KeyFn<EntityRef<Entity>> = (v) => v.entityId.key();

// ---------------------------------------------------------------------------
// Compare functions (for canonical sort)
// ---------------------------------------------------------------------------

type CompareFn<T> = (a: T, b: T) => number;

function compareBytes(a: Uint8Array, b: Uint8Array): number {
	const minLen = Math.min(a.length, b.length);
	for (let i = 0; i < minLen; i++) {
		// biome-ignore lint/style/noNonNullAssertion: i bounded by minLen
		const diff = a[i]! - b[i]!;
		if (diff !== 0) return diff;
	}
	return a.length - b.length;
}

const stringCompareFn: CompareFn<string> = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const numberCompareFn: CompareFn<number> = (a, b) => a - b;
const bytesCompareFn: CompareFn<Uint8Array> = (a, b) => compareBytes(a, b);
const fxtrIdCompareFn: CompareFn<FxtrID> = (a, b) => compareBytes(a.rawDataId, b.rawDataId);
const entityRefCompareFn: CompareFn<EntityRef<Entity>> = (a, b) =>
	compareBytes(a.entityId.rawDataId, b.entityId.rawDataId);

// ---------------------------------------------------------------------------
// PyCompatSet
// ---------------------------------------------------------------------------

export class PyCompatSet<T> implements Iterable<T> {
	private _map: Map<string, T>;
	private _keyFn: KeyFn<T>;
	private _frozen: boolean;

	constructor(keyFn: KeyFn<T>, items?: Iterable<T>) {
		this._keyFn = keyFn;
		this._map = new Map();
		this._frozen = false;
		if (items) {
			for (const item of items) {
				this._map.set(keyFn(item), item);
			}
		}
	}

	get size(): number {
		return this._map.size;
	}

	get frozen(): boolean {
		return this._frozen;
	}

	has(item: T): boolean {
		return this._map.has(this._keyFn(item));
	}

	add(item: T): this {
		if (this._frozen) {
			throw new Error("Cannot add to a frozen PyCompatSet");
		}
		this._map.set(this._keyFn(item), item);
		return this;
	}

	delete(item: T): boolean {
		if (this._frozen) {
			throw new Error("Cannot delete from a frozen PyCompatSet");
		}
		return this._map.delete(this._keyFn(item));
	}

	values(): IterableIterator<T> {
		return this._map.values();
	}

	[Symbol.iterator](): IterableIterator<T> {
		return this._map.values();
	}

	/** Freeze the set — add/delete throw after this. */
	freeze(): this {
		this._frozen = true;
		return this;
	}

	/** Convert to an array (unordered). */
	toArray(): T[] {
		return [...this._map.values()];
	}
}

// ---------------------------------------------------------------------------
// pyCompatSet() field codec
// ---------------------------------------------------------------------------

/** Allowed codec kinds for pyCompatSet items. */
const ALLOWED_KINDS = new Set([
	"string",
	"number",
	"bytes",
	"fxtrId",
	"childFxtrId",
	"entityRef",
	"childRef",
]);

interface SetCodecConfig<T> {
	keyFn: KeyFn<T>;
	compareFn: CompareFn<T>;
	/** Wrap item for CBOR encoding (e.g., FxtrID → RawWeakFxtrID). */
	toCborItem(value: T, scope: FxtrScope): PromiseOrEager<FxtrCbor>;
	/** Unwrap item from CBOR decoding. */
	fromCborItem(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<T>;
}

function getSetCodecConfig<T>(itemCodec: FieldCodec<T>): SetCodecConfig<T> {
	switch (itemCodec.kind) {
		case "string":
			return {
				keyFn: stringKeyFn as KeyFn<T>,
				compareFn: stringCompareFn as CompareFn<T>,
				toCborItem(value: T): Eager<FxtrCbor> {
					return Eager.resolve(value as FxtrCbor);
				},
				fromCborItem(cbor: FxtrCbor): Eager<T> {
					return Eager.resolve(cbor as T);
				},
			};
		case "number":
			return {
				keyFn: numberKeyFn as KeyFn<T>,
				compareFn: numberCompareFn as CompareFn<T>,
				toCborItem(value: T): Eager<FxtrCbor> {
					return Eager.resolve(value as FxtrCbor);
				},
				fromCborItem(cbor: FxtrCbor): Eager<T> {
					return Eager.resolve(cbor as T);
				},
			};
		case "bytes":
			return {
				keyFn: bytesKeyFn as KeyFn<T>,
				compareFn: bytesCompareFn as CompareFn<T>,
				toCborItem(value: T): Eager<FxtrCbor> {
					return Eager.resolve(value as FxtrCbor);
				},
				fromCborItem(cbor: FxtrCbor): Eager<T> {
					return Eager.resolve(cbor as T);
				},
			};
		case "fxtrId":
			return {
				keyFn: fxtrIdKeyFn as KeyFn<T>,
				compareFn: fxtrIdCompareFn as CompareFn<T>,
				toCborItem(value: T): Eager<FxtrCbor> {
					return Eager.resolve(new RawWeakFxtrID(value as unknown as FxtrID));
				},
				fromCborItem(cbor: FxtrCbor): Eager<T> {
					return Eager.resolve((cbor as RawWeakFxtrID).innerId as unknown as T);
				},
			};
		case "childFxtrId":
			return {
				keyFn: fxtrIdKeyFn as KeyFn<T>,
				compareFn: fxtrIdCompareFn as CompareFn<T>,
				toCborItem(value: T): Eager<FxtrCbor> {
					return Eager.resolve(new RawStrongFxtrID(value as unknown as FxtrID));
				},
				fromCborItem(cbor: FxtrCbor): Eager<T> {
					return Eager.resolve((cbor as RawStrongFxtrID).innerId as unknown as T);
				},
			};
		case "entityRef":
			return {
				keyFn: entityRefKeyFn as KeyFn<T>,
				compareFn: entityRefCompareFn as CompareFn<T>,
				toCborItem(value: T, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
					return itemCodec.toCbor(value, scope);
				},
				fromCborItem(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<T> {
					return itemCodec.fromCbor(cbor, scope).then((decoded) => decoded.value);
				},
			};
		case "childRef":
			return {
				keyFn: entityRefKeyFn as KeyFn<T>,
				compareFn: entityRefCompareFn as CompareFn<T>,
				toCborItem(value: T, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
					return itemCodec.toCbor(value, scope);
				},
				fromCborItem(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<T> {
					return itemCodec.fromCbor(cbor, scope).then((decoded) => decoded.value);
				},
			};
		default:
			throw new Error(
				`pyCompatSet does not support item codec kind "${itemCodec.kind}". ` +
					`Allowed kinds: ${[...ALLOWED_KINDS].join(", ")}`,
			);
	}
}

/**
 * Unordered homogeneous collection. Serialized as a sorted array matching
 * Python's _canonical_set_sort_key.
 */
export interface PyCompatSetCodec<T> extends FieldCodec<PyCompatSet<T>> {
	/** Create a new PyCompatSet with the correct key function for this codec. */
	createSet(items?: Iterable<T>): PyCompatSet<T>;
}

export function pyCompatSet<T>(itemCodec: FieldCodec<T>): PyCompatSetCodec<T> {
	if (!ALLOWED_KINDS.has(itemCodec.kind)) {
		throw new Error(
			`pyCompatSet does not support item codec kind "${itemCodec.kind}". ` +
				`Allowed kinds: ${[...ALLOWED_KINDS].join(", ")}`,
		);
	}
	const config = getSetCodecConfig(itemCodec);

	return {
		kind: "pyCompatSet",
		createSet(items?: Iterable<T>): PyCompatSet<T> {
			return new PyCompatSet<T>(config.keyFn, items);
		},
		toCbor(value: PyCompatSet<T>, scope: FxtrScope): PromiseOrEager<FxtrCbor> {
			// Sort items by canonical sort key
			const items = value.toArray();
			items.sort(config.compareFn);
			// Encode each item
			return Eager.all(items.map((item) => config.toCborItem(item, scope))).then((encoded) => {
				return encoded as FxtrCbor[];
			});
		},
		fromCbor(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<DecodedField<PyCompatSet<T>>> {
			const arr = cbor as FxtrCbor[];
			return Eager.all(arr.map((item) => config.fromCborItem(item, scope))).then((decodedItems) => {
				const set = new PyCompatSet<T>(config.keyFn);
				for (const item of decodedItems) {
					set.add(item);
				}
				set.freeze();
				return { value: set, frozen: true };
			});
		},
	};
}
