/**
 * FxtrScope: entity lifetime management, caching, hierarchy, deep fetch.
 *
 * Ported from Python's entity_ref_scope.py. Key differences from Python:
 * - No context variables — scopes are always passed explicitly
 * - State/Surface split replaces Python's _load_tracer context var
 * - Store operations are async (Web Crypto SHA-256)
 * - cachedSupportsEntityType replaces Python's handler_supports_entity_type
 */

import { type MemoryArena, MemoryStore } from "../arena/memoryArena.js";
import type { DeepFetchable, ReadableArena } from "../arena/types.js";
import { type FxtrCbor, FxtrID, RawStrongFxtrID, RawWeakFxtrID } from "../core/entityCore.js";
import type { RawFxtrRef } from "../core/entityCore.js";
import { visitIds } from "../core/visitor.js";
import { Eager, type PromiseOrEager } from "../util/eager.js";
import {
	type Entity,
	type EntityLoadCodec,
	EntityRef,
	FXTR_CACHEABLE,
	FXTR_CODEC,
	type UnknownEntity,
	unknownLoadCodec,
} from "./entityRef.js";

// ---------------------------------------------------------------------------
// FXTR_SCOPE_INTERNALS — symbol-gated access to scope internals
// ---------------------------------------------------------------------------

/**
 * Symbol key for accessing internal scope operations.
 *
 * Import this from `@fxtr/core/internal` to opt into internal APIs.
 * Without importing this symbol, internal methods are inaccessible.
 */
export const FXTR_SCOPE_INTERNALS: unique symbol = Symbol("fxtrScopeInternals");

/**
 * Internal scope operations, accessible via `scope[FXTR_SCOPE_INTERNALS]`.
 *
 * These are implementation details used by sibling packages like
 * `@fxtr/http-server` and `@fxtr/http-client`. Not part of the public API.
 */
export interface FxtrScopeInternals {
	/** Search all backing stores for an entity by ID. Only callable if scope is deeply fetchable. */
	deepFetch(nodeId: FxtrID): PromiseOrEager<ReadableArena | null>;
	/** Set of external DeepFetchable sources (e.g., HTTP stores). */
	readonly deepFetchSources: Set<DeepFetchable>;
	/** Attach an entity from an external arena into this scope's memory arena. */
	unsafeAttachFromExternalArena(nodeId: FxtrID, source: ReadableArena): void;
	/** The scope's main arena. */
	readonly mainArena: MemoryArena;
	/** The scope's backing store. */
	readonly backingStore: MemoryStore;
	/** The scope's internal state object. */
	readonly state: FxtrScopeState;
}

// ---------------------------------------------------------------------------
// Sequential find helper
// ---------------------------------------------------------------------------

/**
 * Iterate items sequentially, calling fn on each. Return the first non-null result,
 * or null if all items return null. Builds the .then() chain lazily — only calls
 * fn for the next item when the current one returns null.
 */
function sequentialFind<TItem, TResult>(
	items: TItem[],
	fn: (item: TItem) => PromiseOrEager<TResult | null>,
): PromiseOrEager<TResult | null> {
	function step(index: number): PromiseOrEager<TResult | null> {
		if (index >= items.length) return Eager.resolve(null);
		// biome-ignore lint/style/noNonNullAssertion: bounds checked above
		return fn(items[index]!).then((found) => {
			if (found !== null) return found;
			return step(index + 1);
		});
	}
	return step(0);
}

// ---------------------------------------------------------------------------
// Cache entry types
// ---------------------------------------------------------------------------

export interface RefLoadCacheEntry<T extends Entity> {
	readonly ref: EntityRef<T>;
	readonly value: T;
	readonly sideEffectLoads: ReadonlyArray<
		RefLoadCacheEntry<Entity> | RefLoadUncacheableMarker<Entity>
	>;
}

export interface RefLoadUncacheableMarker<T extends Entity> {
	readonly ref: EntityRef<T>;
	readonly sideEffectLoads: ReadonlyArray<
		RefLoadCacheEntry<Entity> | RefLoadUncacheableMarker<Entity>
	>;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export class EntityNotLoadedError extends Error {
	readonly ref: EntityRef<Entity>;

	constructor(ref: EntityRef<Entity>, message: string) {
		super(message);
		this.name = "EntityNotLoadedError";
		this.ref = ref;
	}
}

// ---------------------------------------------------------------------------
// KeepAlive (internal)
// ---------------------------------------------------------------------------

/**
 * Allows child scopes to keep a parent scope alive.
 *
 * `hold()` registers a hold; `shutdown()` waits for all holds to be released.
 * This is simpler than Python's KeepAlive, which also supports an
 * `on_unexpected_close` callback for when anyio delivers a cancellation
 * exception during shutdown. JS Promises don't have cancellation semantics,
 * so that mechanism is omitted here. If we later add AbortSignal support to
 * withFxtrScope, we'd revisit this.
 */
class KeepAlive {
	private _shutdownRequested = false;
	private _holdCount = 0;
	private _shutdownResolve: (() => void) | null = null;

	/** Register a keep-alive hold. Returns a release function. */
	hold(): () => void {
		if (this._shutdownRequested) {
			throw new Error("Scope has already been shut down");
		}
		let released = false;
		this._holdCount++;

		return () => {
			if (released) return;
			released = true;
			this._holdCount--;
			// If shutdown is waiting and all holds released, resolve it.
			if (this._shutdownRequested && this._holdCount === 0 && this._shutdownResolve) {
				this._shutdownResolve();
			}
		};
	}

	/** Request shutdown and wait for all holds to be released. */
	shutdown(): PromiseOrEager<void> {
		this._shutdownRequested = true;
		if (this._holdCount === 0) return Eager.resolve(undefined);
		return new Promise<void>((resolve) => {
			this._shutdownResolve = resolve;
		});
	}
}

// ---------------------------------------------------------------------------
// FxtrScopeState (internal)
// ---------------------------------------------------------------------------

let scopeIdCounter = 0;

class FxtrScopeState {
	// Identity & hierarchy
	readonly scopeId: number;
	readonly name: string | null;
	readonly parents: readonly FxtrScopeState[];
	readonly allAncestorIds: ReadonlySet<number>;
	readonly isRootScope: boolean;

	// Arena layer
	readonly store: MemoryStore;
	readonly mainArena: MemoryArena;
	readonly contextMarker: FxtrID;

	// Lifecycle
	started = false;
	closeScheduled = false;
	shutdownComplete = false;
	private _frozen = false;
	private _disposePromise: PromiseOrEager<void> | null = null;
	readonly parentHoldReleases: Array<() => void> = [];
	readonly keepAlive = new KeepAlive();

	// Caching
	// Outer key: entityId.key(), inner key: codec instance (by reference)
	private readonly _refLoadedValueCache = new Map<
		string,
		Map<EntityLoadCodec<Entity>, RefLoadCacheEntry<Entity>>
	>();
	// Outer key: entityId.key(), inner: codec instances loaded since last abandon
	private readonly _refsLoadedSinceAbandon = new Map<string, Set<EntityLoadCodec<Entity>>>();

	// Deep fetchability
	private _deeplyFetchableFlag: boolean | null = null;
	readonly deepFetchSources = new Set<DeepFetchable>();

	constructor(opts: {
		name?: string | undefined;
		parents: FxtrScopeState[];
		isRootScope?: boolean | undefined;
	}) {
		this.scopeId = scopeIdCounter++;
		this.name = opts.name ?? null;
		this.parents = opts.parents;
		this.isRootScope = opts.isRootScope ?? false;

		// Compute ancestor IDs from parents.
		const ancestorIds = new Set<number>();
		for (const parent of opts.parents) {
			ancestorIds.add(parent.scopeId);
			for (const aid of parent.allAncestorIds) {
				ancestorIds.add(aid);
			}
		}
		this.allAncestorIds = ancestorIds;

		// Arena layer.
		this.store = new MemoryStore();
		this.mainArena = this.store.newEmptyArena();
		this.contextMarker = this.mainArena.trackBuiltin(
			FxtrID.fromTagAndPayload(".", new Uint8Array(0)),
		);
	}

	// -- Lifecycle --

	get frozen(): boolean {
		return this._frozen;
	}

	checkAlive(): void {
		if (!this.started) {
			throw new Error("Scope has not been started!");
		}
		if (this.closeScheduled) {
			throw new Error("Scope is closing!");
		}
		if (this.shutdownComplete) {
			throw new Error("Scope has already been shut down!");
		}
	}

	checkNotFrozen(method: string): void {
		if (this._frozen) {
			throw new Error(
				`Scope has been frozen! \`${method}\` operations are not allowed on a frozen scope. To perform operations in a frozen scope, create a new FxtrScope that inherits from this one.`,
			);
		}
	}

	checkNotTracingLoads(method: string, loadTrace: LoadTraceList | null): void {
		if (loadTrace !== null) {
			throw new Error(
				`Scope is actively loading an entity! \`${method}\` operations are not allowed on a scope that is in the middle of running a load handler; the only allowed operation is to load more entities.`,
			);
		}
	}

	checkNotFrozenAndNotTracing(method: string, loadTrace: LoadTraceList | null): void {
		this.checkNotFrozen(method);
		this.checkNotTracingLoads(method, loadTrace);
	}

	// -- Freezing --

	freeze(): void {
		this.checkAlive();
		if (this.isRootScope) {
			throw new Error("Root scopes cannot be frozen");
		}
		if (this._frozen) return; // idempotent
		for (const parent of this.parents) {
			if (!parent.frozen) {
				throw new Error(
					`Cannot freeze scope: parent ${parent.debugName()} is not frozen. All parent scopes must be frozen before freezing a child scope.`,
				);
			}
		}
		this._frozen = true;
	}

	// -- Deep fetchability --

	isDeeplyFetchable(): boolean {
		if (this._deeplyFetchableFlag === null) {
			throw new Error("Scope has not been entered or marked not-deeply-fetchable");
		}
		return this._deeplyFetchableFlag;
	}

	setDeeplyFetchable(value: boolean): void {
		this._deeplyFetchableFlag = value;
	}

	inferDeepFetchability(): void {
		if (this._deeplyFetchableFlag !== null) return;
		let inferred = true;
		for (const parent of this.parents) {
			if (!parent.isDeeplyFetchable()) {
				inferred = false;
				break;
			}
		}
		this._deeplyFetchableFlag = inferred;
	}

	// -- Caching --

	getCacheEntriesForId(nodeId: FxtrID): Iterable<RefLoadCacheEntry<Entity>> {
		const inner = this._refLoadedValueCache.get(nodeId.key());
		return inner ? inner.values() : [];
	}

	getCacheEntryForRef(ref: EntityRef<Entity>): RefLoadCacheEntry<Entity> | null {
		const inner = this._refLoadedValueCache.get(ref.entityId.key());
		if (!inner) return null;
		return inner.get(ref.loadCodec) ?? null;
	}

	setCacheEntry(entry: RefLoadCacheEntry<Entity>): void {
		const idKey = entry.ref.entityId.key();
		let inner = this._refLoadedValueCache.get(idKey);
		if (!inner) {
			inner = new Map();
			this._refLoadedValueCache.set(idKey, inner);
		}
		inner.set(entry.ref.loadCodec, entry);
	}

	removeCacheEntriesForId(nodeId: FxtrID): void {
		this._refLoadedValueCache.delete(nodeId.key());
	}

	clearCache(includeParents: boolean): void {
		this._refLoadedValueCache.clear();
		this._refsLoadedSinceAbandon.clear();
		if (includeParents) {
			for (const parent of this.parents) {
				parent.clearCache(true);
			}
		}
	}

	// -- Side-effect tracking --

	isRefLoadedSinceAbandon(ref: EntityRef<Entity>): boolean {
		const inner = this._refsLoadedSinceAbandon.get(ref.entityId.key());
		return inner?.has(ref.loadCodec) ?? false;
	}

	markRefLoadedSinceAbandon(ref: EntityRef<Entity>): void {
		const idKey = ref.entityId.key();
		let inner = this._refsLoadedSinceAbandon.get(idKey);
		if (!inner) {
			inner = new Set();
			this._refsLoadedSinceAbandon.set(idKey, inner);
		}
		inner.add(ref.loadCodec);
	}

	clearRefsLoadedSinceAbandon(): void {
		this._refsLoadedSinceAbandon.clear();
	}

	// -- Lift (internal, unchecked) --

	liftUnchecked(node: FxtrID | EntityRef<Entity>): void {
		const isRef = node instanceof EntityRef;
		const nodeId = isRef ? node.entityId : node;
		let trackedHere = this.mainArena.isTracked(nodeId);
		let requestedRef: EntityRef<Entity> | null = isRef ? node : null;

		// If already tracked and (not a ref OR ref is cached), nothing to do.
		if (trackedHere) {
			if (requestedRef === null || this.getCacheEntryForRef(requestedRef) !== null) return;
		}

		// BFS through parent states to find the node.
		const stack: FxtrScopeState[] = [this];
		const seen = new Set<number>([this.scopeId]);

		while (stack.length > 0 && (!trackedHere || requestedRef !== null)) {
			// biome-ignore lint/style/noNonNullAssertion: length checked in while condition
			const current = stack.pop()!;
			for (const parent of current.parents) {
				if (seen.has(parent.scopeId)) continue;

				if (!trackedHere && parent.mainArena.isTracked(nodeId)) {
					// Found in parent. Bridge it into this scope.
					this.mainArena.unsafeLazilyAttachFromArena(nodeId, parent.mainArena);
					// Adopt cached refs for this ID.
					for (const cacheEntry of parent.getCacheEntriesForId(nodeId)) {
						this.setCacheEntry(cacheEntry);
					}
					trackedHere = true;
				}

				if (requestedRef !== null) {
					const cacheEntry = parent.getCacheEntryForRef(requestedRef);
					if (cacheEntry !== null) {
						this.setCacheEntry(cacheEntry);
						requestedRef = null;
					}
				}

				seen.add(parent.scopeId);
				stack.push(parent);
			}
		}

		if (!trackedHere) {
			throw new Error(`Node ${nodeId.idString()} was not found in any parent scope!`);
		}
	}

	// -- Deep fetch --

	deepFetch(nodeId: FxtrID): PromiseOrEager<ReadableArena | null> {
		if (!this.isDeeplyFetchable()) {
			throw new Error("Scope is not deeply fetchable; cannot use deepFetch");
		}
		// Phase 1: check stores.
		return this._deepFetchCached(nodeId).then((cached) => {
			if (cached) return cached;
			// Phase 2: scan external sources.
			return this._deepFetchScanSources(nodeId);
		});
	}

	private _deepFetchCached(nodeId: FxtrID): PromiseOrEager<ReadableArena | null> {
		return sequentialFind(
			[
				this.store,
				...this.parents.map((p) => ({ tryLookup: (id: FxtrID) => p._deepFetchCached(id) })),
			],
			(source) => source.tryLookup(nodeId),
		);
	}

	private _deepFetchScanSources(nodeId: FxtrID): PromiseOrEager<ReadableArena | null> {
		const self = this;
		// First scan our own sources.
		const sourceItems = [...this.deepFetchSources].map((source) => ({
			check(): PromiseOrEager<ReadableArena | null> {
				return source.tryLookup(nodeId).then((found) => {
					if (found) {
						const newArena = self.store.newEmptyArena();
						newArena.unsafeLazilyAttachFromArena(nodeId, found);
						found.close();
						return newArena;
					}
					return null;
				});
			},
		}));
		// Then scan parent sources.
		const parentItems = this.parents.map((parent) => ({
			check(): PromiseOrEager<ReadableArena | null> {
				return parent._deepFetchScanSources(nodeId);
			},
		}));
		const allItems = [...sourceItems, ...parentItems];
		return sequentialFind(allItems, (item) => item.check());
	}

	// -- Lifecycle: scheduleClose / dispose --

	scheduleClose(): void {
		if (this.closeScheduled || this.shutdownComplete) return; // idempotent
		this.closeScheduled = true;
		this._disposePromise = this._doShutdown();
	}

	dispose(): PromiseOrEager<void> {
		if (!this._disposePromise) this.scheduleClose();
		// biome-ignore lint/style/noNonNullAssertion: scheduleClose always sets _disposePromise
		return this._disposePromise!;
	}

	private _doShutdown(): PromiseOrEager<void> {
		return this.keepAlive.shutdown().then(() => {
			this.mainArena.close();
			this.shutdownComplete = true;
			if (!this.store.hasOpenArenas()) {
				this.store.close();
			}
			for (const release of this.parentHoldReleases) {
				release();
			}
		});
	}

	// -- Debug --

	debugName(): string {
		if (this.name) return `<FxtrScope ${JSON.stringify(this.name)}>`;
		return `<FxtrScope ${this.scopeId}>`;
	}
}

// ---------------------------------------------------------------------------
// FxtrScope (public interface)
// ---------------------------------------------------------------------------

/**
 * User-facing handle for a scope. All public operations are defined here.
 * Internal operations are accessible via `scope[FXTR_SCOPE_INTERNALS]`.
 */
export interface FxtrScope {
	/** Store an entity that carries [FXTR_CODEC]. */
	store<T extends Entity>(entity: T): PromiseOrEager<EntityRef<T>>;
	/** Store raw CBOR data. Lifts all child IDs first. Returns content-addressed ID. */
	storeCbor(cbor: FxtrCbor): PromiseOrEager<FxtrID>;
	/** Load an EntityRef, using the ref's load codec. */
	load<T extends Entity>(ref: EntityRef<T>): PromiseOrEager<T>;
	/** Load raw CBOR for an entity. Cached (uses unknownLoadCodec). */
	loadCbor(nodeId: FxtrID): PromiseOrEager<FxtrCbor>;
	/** Ensure a node is tracked in this scope (must be in a parent). */
	lift<N extends FxtrID | EntityRef<Entity>>(node: N): N;
	/** Track a builtin ("." or "!") node. */
	trackBuiltin(builtinId: FxtrID): FxtrID;
	/** Copy an entity (and transitive deps) from another scope into this one. */
	copyFromScope<N extends FxtrID | EntityRef<Entity>>(
		node: N,
		sourceScope: FxtrScope,
	): PromiseOrEager<N>;
	/** Release an entity from this scope. Clears its cache entries. */
	abandon(node: FxtrID | EntityRef<Entity>): void;
	/** Check if a node is tracked. */
	isTracked(node: FxtrID | EntityRef<Entity>): boolean;
	/** Get a cached value for a ref, or throw EntityNotLoadedError. */
	getCachedValue<T extends Entity>(ref: EntityRef<T>): T;
	/** Check if this scope is alive (started, not closing, and not shut down). */
	isAlive(): boolean;
	/** Check if this scope is included as an ancestor of another scope. */
	includedInScope(other: FxtrScope): boolean;
	/** Get the context marker ID (always "."). */
	getContextMarkerId(): FxtrID;
	/** Clear cached loaded values. Not allowed on frozen scopes. */
	clearCache(includeParents?: boolean): void;
	/** Freeze this scope — no further mutations allowed. Idempotent. */
	freeze(): void;
	/** Whether this scope is frozen. */
	readonly frozen: boolean;
	/**
	 * Schedule this scope for closure. Sync, safe for React useEffect cleanup.
	 * Idempotent — calling multiple times is safe.
	 */
	scheduleClose(): void;
	/** Fully close this scope. Async — waits for all children to close first. */
	dispose(): PromiseOrEager<void>;
	/** Internal scope operations. Import FXTR_SCOPE_INTERNALS from @fxtr/core/internal to access. */
	readonly [FXTR_SCOPE_INTERNALS]: FxtrScopeInternals;
}

// ---------------------------------------------------------------------------
// FxtrScopeImpl (concrete, not exported)
// ---------------------------------------------------------------------------

type LoadTraceList = Array<RefLoadCacheEntry<Entity> | RefLoadUncacheableMarker<Entity>>;

class FxtrScopeImpl implements FxtrScope, FxtrScopeInternals {
	private readonly _state: FxtrScopeState;
	private readonly _loadTrace: LoadTraceList | null;

	constructor(state: FxtrScopeState, loadTrace: LoadTraceList | null = null) {
		this._state = state;
		this._loadTrace = loadTrace;
	}

	// -- FxtrScopeInternals implementation --

	get [FXTR_SCOPE_INTERNALS](): FxtrScopeInternals {
		return this;
	}

	deepFetch(nodeId: FxtrID): PromiseOrEager<ReadableArena | null> {
		this._state.checkAlive();
		return this._state.deepFetch(nodeId);
	}

	get deepFetchSources(): Set<DeepFetchable> {
		return this._state.deepFetchSources;
	}

	unsafeAttachFromExternalArena(nodeId: FxtrID, source: ReadableArena): void {
		this._state.checkAlive();
		this._state.checkNotFrozen("unsafeAttachFromExternalArena");
		this._state.mainArena.unsafeLazilyAttachFromArena(nodeId, source);
	}

	get mainArena(): MemoryArena {
		return this._state.mainArena;
	}

	get backingStore(): MemoryStore {
		return this._state.store;
	}

	get state(): FxtrScopeState {
		return this._state;
	}

	// -- Store operations (async, mutating) --

	store<T extends Entity>(entity: T): PromiseOrEager<EntityRef<T>> {
		this._state.checkAlive();
		this._state.checkNotFrozenAndNotTracing("store", this._loadTrace);
		const codec = entity[FXTR_CODEC];
		return codec.store(entity, this);
	}

	storeCbor(cbor: FxtrCbor): PromiseOrEager<FxtrID> {
		this._state.checkAlive();
		this._state.checkNotFrozenAndNotTracing("storeCbor", this._loadTrace);
		visitIds(cbor, (ref: RawFxtrRef) => {
			this._state.liftUnchecked(ref.innerId);
		});
		return this._state.mainArena.store(cbor);
	}

	// -- Load operations (async, allowed on tracing surfaces) --

	load<T extends Entity>(ref: EntityRef<T>): PromiseOrEager<T> {
		this._state.checkAlive();
		this._state.checkNotFrozen("load");

		// Check cache.
		let cached = this._state.getCacheEntryForRef(ref as EntityRef<Entity>);
		if (cached === null) {
			// Try lifting to pick up cached entries from parents.
			this._state.liftUnchecked(ref as EntityRef<Entity>);
			cached = this._state.getCacheEntryForRef(ref as EntityRef<Entity>);
		}

		if (cached !== null) {
			// Replay side effects.
			const cachedEntry = cached;
			return this._loadForSideEffects(cachedEntry).then(() => {
				// Log to trace if tracing.
				if (this._loadTrace !== null) {
					this._loadTrace.push(cachedEntry);
				}
				return cachedEntry.value as T;
			});
		}

		// Not in cache. The ID should be tracked after lift.
		return this._state.mainArena.load(ref.entityId).then((loadedCbor) => {
			if (typeof loadedCbor !== "object" || loadedCbor === null || Array.isArray(loadedCbor)) {
				throw new Error(`Expected object CBOR for entity ${ref.entityId.idString()}`);
			}
			const typeField = (loadedCbor as Record<string, FxtrCbor>)["$fxtr.type"];
			if (!(typeField instanceof RawWeakFxtrID) && !(typeField instanceof RawStrongFxtrID)) {
				throw new Error(
					`Expected RawWeakFxtrID or RawStrongFxtrID for $fxtr.type, got ${typeof typeField}`,
				);
			}
			const typeId = typeField.innerId;

			// Check if the codec supports this entity type (cached + isolated).
			return cachedSupportsEntityType(ref.loadCodec, typeId, this).then((supported) => {
				if (!supported) {
					throw new Error(
						`Load codec does not support entity type ${typeId.idString()} ` +
							`for entity ${ref.entityId.idString()}`,
					);
				}

				// Decode with a tracing surface.
				const traceList: LoadTraceList = [];
				const tracingSurface = new FxtrScopeImpl(this._state, traceList);
				return ref.loadCodec.decode(loadedCbor, tracingSurface).then((decoded) => {
					const cacheable = (decoded as Entity)[FXTR_CACHEABLE] === true;

					if (cacheable) {
						const newEntry: RefLoadCacheEntry<Entity> = {
							ref: ref as EntityRef<Entity>,
							value: decoded,
							sideEffectLoads: traceList,
						};
						this._state.setCacheEntry(newEntry);
						this._state.markRefLoadedSinceAbandon(ref as EntityRef<Entity>);
						if (this._loadTrace !== null) {
							this._loadTrace.push(newEntry);
						}
					} else if (this._loadTrace !== null) {
						this._loadTrace.push({
							ref: ref as EntityRef<Entity>,
							sideEffectLoads: traceList,
						} as RefLoadUncacheableMarker<Entity>);
						this._state.markRefLoadedSinceAbandon(ref as EntityRef<Entity>);
					}

					return decoded;
				});
			});
		});
	}

	loadCbor(nodeId: FxtrID): PromiseOrEager<FxtrCbor> {
		return this.load(new EntityRef<UnknownEntity>(nodeId, unknownLoadCodec)).then(
			(entity) => entity.raw_cbor,
		);
	}

	// -- Side-effect replay --

	private _loadForSideEffects(
		cachedLoad: RefLoadCacheEntry<Entity> | RefLoadUncacheableMarker<Entity>,
	): PromiseOrEager<void> {
		// Fast path: already loaded since last abandon.
		if (this._state.isRefLoadedSinceAbandon(cachedLoad.ref)) return Eager.resolve(undefined);
		// Slow path: load the entity (tracking deps), replay side effects.
		return this._state.mainArena.load(cachedLoad.ref.entityId).then(() => {
			const sideEffectResults = cachedLoad.sideEffectLoads.map((sideEffect) =>
				this._loadForSideEffects(sideEffect),
			);
			return Eager.all(sideEffectResults).then(() => {
				this._state.markRefLoadedSinceAbandon(cachedLoad.ref);
			});
		});
	}

	// -- Tracking operations --

	lift<N extends FxtrID | EntityRef<Entity>>(node: N): N {
		this._state.checkAlive();
		this._state.checkNotFrozenAndNotTracing("lift", this._loadTrace);
		this._state.liftUnchecked(node);
		return node;
	}

	trackBuiltin(builtinId: FxtrID): FxtrID {
		this._state.checkAlive();
		this._state.checkNotFrozenAndNotTracing("trackBuiltin", this._loadTrace);
		return this._state.mainArena.trackBuiltin(builtinId);
	}

	// -- Copy operations --

	copyFromScope<N extends FxtrID | EntityRef<Entity>>(
		node: N,
		sourceScope: FxtrScope,
	): PromiseOrEager<N> {
		this._state.checkAlive();
		this._state.checkNotFrozenAndNotTracing("copyFromScope", this._loadTrace);
		const sourceState = sourceScope[FXTR_SCOPE_INTERNALS].state;
		// Lift in source scope first.
		sourceState.liftUnchecked(node);
		const nodeId: FxtrID = node instanceof EntityRef ? node.entityId : node;
		return this._state.mainArena.copyFromArena(nodeId, sourceState.mainArena).then(() => {
			// Copy cache entries.
			for (const cacheEntry of sourceState.getCacheEntriesForId(nodeId)) {
				this._state.setCacheEntry(cacheEntry);
			}
			return node;
		});
	}

	// -- Abandon --

	abandon(node: FxtrID | EntityRef<Entity>): void {
		this._state.checkAlive();
		this._state.checkNotFrozenAndNotTracing("abandon", this._loadTrace);
		const nodeId = node instanceof EntityRef ? node.entityId : node;
		if (!this._state.mainArena.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId.idString()} is not tracked in this scope!`);
		}
		this._state.mainArena.abandon(nodeId);
		this._state.removeCacheEntriesForId(nodeId);
		this._state.clearRefsLoadedSinceAbandon();
	}

	// -- Read-only operations --

	isTracked(node: FxtrID | EntityRef<Entity>): boolean {
		const nodeId = node instanceof EntityRef ? node.entityId : node;
		return this._state.mainArena.isTracked(nodeId);
	}

	getCachedValue<T extends Entity>(ref: EntityRef<T>): T {
		this._state.checkAlive();
		const cached = this._state.getCacheEntryForRef(ref as EntityRef<Entity>);
		if (cached === null) {
			throw new EntityNotLoadedError(
				ref as EntityRef<Entity>,
				`${ref} is not cached in this scope, so its value cannot be accessed synchronously! Please use ref.load(scope) to load it asynchronously.`,
			);
		}

		// Fast path.
		if (this._state.isRefLoadedSinceAbandon(ref as EntityRef<Entity>)) {
			return cached.value as T;
		}

		// Slow path: check all side-effect loads recursively.
		for (const sideEffect of cached.sideEffectLoads) {
			// This will throw EntityNotLoadedError if any side effect is missing.
			this.getCachedValue(sideEffect.ref);
		}

		this._state.markRefLoadedSinceAbandon(ref as EntityRef<Entity>);
		return cached.value as T;
	}

	isAlive(): boolean {
		return this._state.started && !this._state.closeScheduled && !this._state.shutdownComplete;
	}

	includedInScope(other: FxtrScope): boolean {
		const otherState = other[FXTR_SCOPE_INTERNALS].state;
		return this._state === otherState || otherState.allAncestorIds.has(this._state.scopeId);
	}

	getContextMarkerId(): FxtrID {
		return this._state.contextMarker;
	}

	// -- Cache management --

	clearCache(includeParents = false): void {
		this._state.checkNotFrozenAndNotTracing("clearCache", this._loadTrace);
		this._state.clearCache(includeParents);
	}

	// -- Freezing --

	freeze(): void {
		this._state.checkNotTracingLoads("freeze", this._loadTrace);
		if (this._state.frozen) return; // idempotent
		this._state.freeze();
	}

	get frozen(): boolean {
		return this._state.frozen;
	}

	// -- Lifecycle: scheduleClose / dispose --

	scheduleClose(): void {
		this._state.scheduleClose();
	}

	dispose(): PromiseOrEager<void> {
		return this._state.dispose();
	}

	toString(): string {
		return this._state.debugName();
	}
}

// Conditionally define Symbol.asyncDispose if the runtime supports it.
// This enables `await using scope = createFxtrScope(...)` in environments
// that support the Explicit Resource Management proposal (Node 22+,
// Chrome 134+). In older environments, use dispose() directly.
if (typeof Symbol.asyncDispose === "symbol") {
	Object.defineProperty(FxtrScopeImpl.prototype, Symbol.asyncDispose, {
		value: FxtrScopeImpl.prototype.dispose,
		writable: true,
		configurable: true,
	});
}

// ---------------------------------------------------------------------------
// createFxtrScope / withFxtrScope
// ---------------------------------------------------------------------------

export interface FxtrScopeOptions {
	/** Human-readable name for debugging. */
	name?: string;
	/** Parent scopes to include. Entities in parent scopes are accessible. */
	include?: FxtrScope[];
}

/**
 * Create a new scope and return it directly. The caller is responsible for
 * cleanup via `scope.scheduleClose()` (sync) or `await scope.dispose()` (async).
 *
 * This is the non-callback alternative to `withFxtrScope`. Use it when the
 * scope's lifetime is tied to something other than a single async function
 * (e.g., a React component tree, a long-lived server, etc.).
 */
export function createFxtrScope(opts: FxtrScopeOptions = {}): FxtrScope {
	const parentStates = (opts.include ?? []).map((s) => s[FXTR_SCOPE_INTERNALS].state);
	const state = new FxtrScopeState({
		name: opts.name,
		parents: parentStates,
	});

	// Validate parents and register keep-alive holds.
	for (const parent of parentStates) {
		if (!parent.started) {
			throw new Error(`Parent scope ${parent.debugName()} has not been started!`);
		}
		if (parent.shutdownComplete) {
			throw new Error(`Parent scope ${parent.debugName()} has already been shut down!`);
		}
		if (parent.closeScheduled) {
			throw new Error(`Parent scope ${parent.debugName()} is closing!`);
		}
		state.parentHoldReleases.push(parent.keepAlive.hold());
	}

	// Compute deep fetchability from parents.
	state.inferDeepFetchability();

	// Mark started.
	state.started = true;
	return new FxtrScopeImpl(state);
}

/**
 * Create a new scope, run a callback, and clean up afterwards.
 *
 * This is a convenience wrapper around `createFxtrScope` + `dispose()`.
 */
export function withFxtrScope<T>(
	opts: FxtrScopeOptions,
	callback: (scope: FxtrScope) => PromiseOrEager<T>,
): PromiseOrEager<T> {
	const scope = createFxtrScope(opts);
	let callbackResult: PromiseOrEager<T>;
	try {
		callbackResult = callback(scope);
	} catch (error) {
		return scope.dispose().then(() => {
			throw error;
		});
	}
	return callbackResult.then(
		(result) => scope.dispose().then(() => result),
		(error) =>
			scope.dispose().then(
				() => {
					throw error;
				},
				() => {
					throw error;
				},
			),
	);
}

// ---------------------------------------------------------------------------
// createRootScope / GLOBAL_SCOPE
// ---------------------------------------------------------------------------

function createRootScope(name?: string): FxtrScope {
	const state = new FxtrScopeState({
		name: name ?? "GLOBAL",
		parents: [],
		isRootScope: true,
	});
	state.setDeeplyFetchable(true);
	state.started = true;
	return new FxtrScopeImpl(state);
}

/** The global root scope, where type builtins are stored. */
export const GLOBAL_SCOPE: FxtrScope = createRootScope();

// ---------------------------------------------------------------------------
// cachedSupportsEntityType
// ---------------------------------------------------------------------------

const _supportsCache = new WeakMap<EntityLoadCodec<Entity>, Map<string, boolean>>();

/**
 * Check if a codec supports a given entity type, with caching.
 *
 * Creates a fresh child scope to isolate any loads the type check triggers.
 * Results are cached per (codec, typeId) pair.
 */
export function cachedSupportsEntityType(
	codec: EntityLoadCodec<Entity>,
	typeId: FxtrID,
	scope: FxtrScope,
): PromiseOrEager<boolean> {
	let codecCache = _supportsCache.get(codec);
	if (!codecCache) {
		codecCache = new Map();
		_supportsCache.set(codec, codecCache);
	}
	const key = typeId.key();
	const existing = codecCache.get(key);
	if (existing !== undefined) return Eager.resolve(existing);

	// Run in a fresh child scope to isolate side effects.
	const cache = codecCache;
	return withFxtrScope({ include: [scope] }, (freshScope) => {
		return codec.supportsEntityType(typeId, freshScope);
	}).then((result) => {
		cache.set(key, result);
		return result;
	});
}
