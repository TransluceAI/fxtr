/**
 * Visitor functions for traversing and transforming entity CBOR data.
 *
 * Ported from Python's arena_util.py. Provides:
 * - visitIds: Walk a CBOR tree, calling a callback for each tagged ID
 * - visitAndProjectIds: Walk and transform all tagged IDs
 * - visitAndProjectIdsInCanonicalCborOrder: Same but in canonical CBOR byte order
 * - asyncVisitAndProjectIdsInCanonicalCborOrder: Async version of the above
 */

import { Eager, type PromiseOrEager } from "../util/eager.js";
import {
	type FxtrCbor,
	FxtrID,
	RawFxtrEntityMarkerID,
	type RawFxtrRef,
	RawStrongFxtrID,
	RawWeakFxtrID,
	fxtrCborDumps,
} from "./entityCore.js";

import { decode } from "cbor2";

function isTaggedRef(value: FxtrCbor): value is RawFxtrRef {
	return (
		value instanceof RawWeakFxtrID ||
		value instanceof RawStrongFxtrID ||
		value instanceof RawFxtrEntityMarkerID
	);
}

function isPlainObject(value: FxtrCbor): value is Record<string, FxtrCbor> {
	return (
		typeof value === "object" &&
		value !== null &&
		!Array.isArray(value) &&
		!(value instanceof Uint8Array) &&
		!isTaggedRef(value)
	);
}

/** Visit all tagged IDs in a CBOR tree. */
export function visitIds(root: FxtrCbor, callback: (ref: RawFxtrRef) => void): void {
	if (isTaggedRef(root)) {
		callback(root);
	} else if (Array.isArray(root)) {
		for (const item of root) {
			visitIds(item, callback);
		}
	} else if (isPlainObject(root)) {
		for (const value of Object.values(root)) {
			visitIds(value, callback);
		}
	}
	// primitives (null, number, boolean, string, Uint8Array): no-op
}

/** Visit all tagged IDs and transform them to new FxtrIDs. */
export function visitAndProjectIds(
	root: FxtrCbor,
	callback: (ref: RawFxtrRef) => FxtrID,
): FxtrCbor {
	if (root instanceof RawWeakFxtrID) return new RawWeakFxtrID(callback(root));
	if (root instanceof RawStrongFxtrID) return new RawStrongFxtrID(callback(root));
	if (root instanceof RawFxtrEntityMarkerID) return new RawFxtrEntityMarkerID(callback(root));
	if (Array.isArray(root)) return root.map((item) => visitAndProjectIds(item, callback));
	if (isPlainObject(root)) {
		const result: Record<string, FxtrCbor> = {};
		for (const [k, v] of Object.entries(root)) {
			result[k] = visitAndProjectIds(v, callback);
		}
		return result;
	}
	return root;
}

/**
 * Build tag decoders that apply a projection callback during decode.
 * Used by canonical-order visitors to process IDs in serialization order.
 */
function makeProjectionTagDecoders(callback: (ref: RawFxtrRef) => FxtrID) {
	return new Map([
		[
			33000,
			(tag: { contents: unknown }) => {
				const ref = new RawFxtrEntityMarkerID(new FxtrID(tag.contents as Uint8Array));
				return new RawFxtrEntityMarkerID(callback(ref));
			},
		],
		[
			33001,
			(tag: { contents: unknown }) => {
				const ref = new RawWeakFxtrID(new FxtrID(tag.contents as Uint8Array));
				return new RawWeakFxtrID(callback(ref));
			},
		],
		[
			33002,
			(tag: { contents: unknown }) => {
				const ref = new RawStrongFxtrID(new FxtrID(tag.contents as Uint8Array));
				return new RawStrongFxtrID(callback(ref));
			},
		],
	]);
}

/**
 * Visit all IDs in canonical CBOR serialization order and project them.
 *
 * This is needed for p-nodes where path indices refer to the position of
 * child IDs in the canonical byte ordering, which may differ from the
 * in-memory object key order.
 */
export function visitAndProjectIdsInCanonicalCborOrder(
	root: FxtrCbor,
	callback: (ref: RawFxtrRef) => FxtrID,
): FxtrCbor {
	const asBytes = fxtrCborDumps(root);
	return decode(asBytes, { tags: makeProjectionTagDecoders(callback) }) as FxtrCbor;
}

/** Build tag decoders that collect IDs into an array during decode. */
function makeCollectingTagDecoders(idsInOrder: RawFxtrRef[]) {
	return new Map([
		[
			33000,
			(tag: { contents: unknown }) => {
				const ref = new RawFxtrEntityMarkerID(new FxtrID(tag.contents as Uint8Array));
				idsInOrder.push(ref);
				return ref;
			},
		],
		[
			33001,
			(tag: { contents: unknown }) => {
				const ref = new RawWeakFxtrID(new FxtrID(tag.contents as Uint8Array));
				idsInOrder.push(ref);
				return ref;
			},
		],
		[
			33002,
			(tag: { contents: unknown }) => {
				const ref = new RawStrongFxtrID(new FxtrID(tag.contents as Uint8Array));
				idsInOrder.push(ref);
				return ref;
			},
		],
	]);
}

/**
 * Async/Eager version: visit all IDs in canonical CBOR order and project them.
 *
 * Three-pass approach:
 * 1. Encode to canonical bytes, decode to collect IDs in order
 * 2. Call callback for each ID — uses Eager.all so sync when all callbacks return Eager
 * 3. Decode again, substituting projected IDs
 */
export function asyncVisitAndProjectIdsInCanonicalCborOrder(
	root: FxtrCbor,
	callback: (ref: RawFxtrRef) => PromiseOrEager<FxtrID>,
): PromiseOrEager<FxtrCbor> {
	const asBytes = fxtrCborDumps(root);

	// Pass 1: collect IDs in canonical order
	const idsInOrder: RawFxtrRef[] = [];
	decode(asBytes, { tags: makeCollectingTagDecoders(idsInOrder) });

	// Pass 2: call callback for each ID, collect results
	const projectedResults = idsInOrder.map((typedId) => callback(typedId));

	// Pass 3: reconstruct with projected IDs (after all callbacks resolve)
	return Eager.all(projectedResults).then((projectedIds) => {
		let idx = 0;
		const reconTagDecoders = new Map([
			[
				33000,
				(_tag: { contents: unknown }) => {
					// biome-ignore lint/style/noNonNullAssertion: idx tracks projectedIds which has same length as idsInOrder
					return new RawFxtrEntityMarkerID(projectedIds[idx++]!);
				},
			],
			[
				33001,
				(_tag: { contents: unknown }) => {
					// biome-ignore lint/style/noNonNullAssertion: idx tracks projectedIds which has same length as idsInOrder
					return new RawWeakFxtrID(projectedIds[idx++]!);
				},
			],
			[
				33002,
				(_tag: { contents: unknown }) => {
					// biome-ignore lint/style/noNonNullAssertion: idx tracks projectedIds which has same length as idsInOrder
					return new RawStrongFxtrID(projectedIds[idx++]!);
				},
			],
		]);
		return decode(asBytes, { tags: reconTagDecoders }) as FxtrCbor;
	});
}
