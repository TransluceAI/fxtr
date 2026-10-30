// @fxtr/core/internal — Internal API
//
// Arena types and unsafe symbols. These are implementation details that
// most consumers don't need. The HTTP packages use them.

// --- arena ---
export {
	type ReadableArena,
	type ReadWriteArena,
	type CopyTargetArena,
	type DeepFetchable,
	DepKind,
	type FxtrEntityFlatData,
} from "./arena/types.js";

export {
	MemoryStore,
	MemoryArena,
	InMemoryBytesBackingStore,
	GLOBAL_BACKING_STORE,
} from "./arena/memoryArena.js";

// --- scope internals ---
export { FXTR_SCOPE_INTERNALS, type FxtrScopeInternals } from "./scope/scope.js";
