// @fxtr/core — Public API
//
// Core entity system: IDs, CBOR encoding, scopes, entity refs, codecs,
// system entities, namespaces.
//
// Arena types are internal — use @fxtr/core/internal if you need them.
// Bicodec field codecs are at @fxtr/core/bicodec.

// --- core ---
export {
	type FxtrCbor,
	FxtrID,
	RawWeakFxtrID,
	RawStrongFxtrID,
	RawFxtrEntityMarkerID,
	type RawFxtrRef,
	fxtrCborDumps,
	fxtrCborLoads,
	fxtrCborDumpsWithMarkerCheck,
	deepFreeze,
	b32enc,
	b32dec,
	encodeVarint32,
	decodeVarint32,
	uint8ArrayEquals,
	InvalidEntityError,
	DuplicateEntityMarkerError,
} from "./core/entityCore.js";

export {
	visitIds,
	visitAndProjectIds,
	visitAndProjectIdsInCanonicalCborOrder,
	asyncVisitAndProjectIdsInCanonicalCborOrder,
} from "./core/visitor.js";

// --- scope ---
export {
	type Entity,
	type EntityStoreCodec,
	type EntityLoadCodec,
	EntityRef,
	FXTR_CODEC,
	FXTR_CACHEABLE,
	UnknownEntity,
	unknownLoadCodec,
} from "./scope/entityRef.js";

export {
	type FxtrScope,
	type FxtrScopeOptions,
	createFxtrScope,
	withFxtrScope,
	GLOBAL_SCOPE,
	cachedSupportsEntityType,
	EntityNotLoadedError,
	type RefLoadCacheEntry,
	type RefLoadUncacheableMarker,
} from "./scope/scope.js";

export { unionLoadCodec } from "./scope/unionLoadCodec.js";

// --- common ---
export {
	FXTR_CTX,
	type EntityDefinition,
	type EntityInstance,
	type InferEntity,
	type FieldsOf,
	type InferField,
	type FieldCodec,
	type DecodedField,
} from "./common/bicodec.js";

export {
	PyCompatSet,
	type KeyFn,
	pyCompatSet,
	type PyCompatSetCodec,
	stringKeyFn,
	numberKeyFn,
	bytesKeyFn,
	fxtrIdKeyFn,
	entityRefKeyFn,
} from "./common/pyCompatSet.js";

export { FxtrBasicType, defineType, UUID } from "./common/systemEntities.js";

// --- namespace ---
export type { FxtrReadNamespace, RetrieveOpts } from "./namespace/readNamespace.js";

// --- util ---
export { Eager, type PromiseOrEager } from "./util/eager.js";
