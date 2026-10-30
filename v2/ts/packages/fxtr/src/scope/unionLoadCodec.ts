/**
 * Union load codec: dispatches to the first matching branch codec.
 *
 * In its own file to avoid a circular dependency between entityRef.ts and
 * scope.ts — this file imports from both without issue since it has no
 * exports consumed by either at module load time.
 */

import { type FxtrCbor, type FxtrID, RawStrongFxtrID, RawWeakFxtrID } from "../core/entityCore.js";
import { Eager, type PromiseOrEager } from "../util/eager.js";
import type { Entity, EntityLoadCodec } from "./entityRef.js";
import type { FxtrScope } from "./scope.js";
import { cachedSupportsEntityType } from "./scope.js";

/**
 * Extract the FxtrID from a $fxtr.type field value.
 * The type field is a RawWeakFxtrID or RawStrongFxtrID.
 */
function extractTypeId(typeField: FxtrCbor): FxtrID {
	if (typeField instanceof RawWeakFxtrID) return typeField.innerId;
	if (typeField instanceof RawStrongFxtrID) return typeField.innerId;
	throw new Error(
		`Expected RawWeakFxtrID or RawStrongFxtrID for $fxtr.type, got ${typeof typeField}`,
	);
}

/** Infer the union of all codec value types. */
type InferCodecType<C> = C extends EntityLoadCodec<infer T> ? T : never;
type InferUnion<Codecs extends EntityLoadCodec<Entity>[]> = InferCodecType<Codecs[number]>;

/**
 * Create a load codec that dispatches to the first matching branch codec.
 *
 * Uses cachedSupportsEntityType for cached type checks with scope isolation.
 */
export function unionLoadCodec<Codecs extends EntityLoadCodec<Entity>[]>(
	...codecs: Codecs
): EntityLoadCodec<InferUnion<Codecs>> {
	return {
		supportsEntityType(typeId: FxtrID, scope: FxtrScope): PromiseOrEager<boolean> {
			// Sequential scan: return true on first match.
			let result: PromiseOrEager<boolean> = Eager.resolve(false);
			for (const codec of codecs) {
				result = result.then((found) => {
					if (found) return true;
					return cachedSupportsEntityType(codec, typeId, scope);
				});
			}
			return result;
		},

		decode(cbor: FxtrCbor, scope: FxtrScope): PromiseOrEager<InferUnion<Codecs>> {
			const typeField = (cbor as Record<string, FxtrCbor>)["$fxtr.type"];
			if (typeField === undefined) {
				throw new Error("Entity CBOR is missing $fxtr.type field");
			}
			const typeId = extractTypeId(typeField);

			// Sequential scan: collect all matching codecs.
			let result: PromiseOrEager<EntityLoadCodec<Entity>[]> = Eager.resolve(
				[] as EntityLoadCodec<Entity>[],
			);
			for (const codec of codecs) {
				result = result.then((matching) => {
					return cachedSupportsEntityType(codec, typeId, scope).then((supported) => {
						if (supported) matching.push(codec);
						return matching;
					});
				});
			}

			return result.then((matching) => {
				if (matching.length === 0) {
					throw new Error(`No codec supports entity type ${typeId.idString()}`);
				}
				if (matching.length > 1) {
					throw new Error(`Multiple codecs match entity type ${typeId.idString()}`);
				}

				// biome-ignore lint/style/noNonNullAssertion: length checked above
				return matching[0]!.decode(cbor, scope) as PromiseOrEager<InferUnion<Codecs>>;
			});
		},
	};
}
