/**
 * React hooks for loading fxtr entities.
 *
 * - useFxtrLoad:       load a single ref via Suspense
 * - useFxtrLoadQuery: load without Suspense, returns { data, loading, error }
 *                      (supports both ref and callback forms)
 * - useFxtrScope:      get a mutable child scope for advanced use cases
 */

import {
	Eager,
	type Entity,
	type EntityLoadCodec,
	EntityNotLoadedError,
	type EntityRef,
	FXTR_CACHEABLE,
	type FxtrScope,
	type PromiseOrEager,
	createFxtrScope,
} from "@fxtr/core";
import { type DependencyList, useContext, useEffect, useMemo, useReducer, useRef } from "react";
import {
	type CallbackCache,
	type CallbackCacheEntry,
	FxtrContext,
	type FxtrContextValue,
	type LoadCache,
	type LoadCacheEntry,
} from "./context.js";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** The load function passed into callback-form hooks. */
export type LoadFn = <T extends Entity>(ref: EntityRef<T>) => PromiseOrEager<T>;

/** Return type of useFxtrLoadQuery. */
export interface LoadStatus<T> {
	data: T | undefined;
	loading: boolean;
	error: unknown;
}

/** Options for useFxtrLoadQuery. */
export interface LoadQueryOptions {
	enabled?: boolean;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function useFxtrContext(): FxtrContextValue {
	const ctx = useContext(FxtrContext);
	if (!ctx) {
		throw new Error(
			"useFxtrLoad/useFxtrLoadQuery/useFxtrScope must be used within a <FxtrProvider>",
		);
	}
	return ctx;
}

function getLoadCacheEntry(
	cache: LoadCache,
	entityKey: string,
	codec: EntityLoadCodec<Entity>,
): LoadCacheEntry | undefined {
	return cache.get(entityKey)?.get(codec);
}

function setLoadCacheEntry(
	cache: LoadCache,
	entityKey: string,
	codec: EntityLoadCodec<Entity>,
	entry: LoadCacheEntry,
): void {
	let inner = cache.get(entityKey);
	if (!inner) {
		inner = new Map();
		cache.set(entityKey, inner);
	}
	inner.set(codec, entry);
}

/** Check whether the first argument is a function (callback form) or an EntityRef. */
function isCallbackForm(
	refOrFn: EntityRef<Entity> | ((load: LoadFn) => PromiseOrEager<unknown>),
): refOrFn is (load: LoadFn) => PromiseOrEager<unknown> {
	return typeof refOrFn === "function";
}

// ---------------------------------------------------------------------------
// Ref-based load helpers (shared by useFxtrLoad and useFxtrLoadQuery)
// ---------------------------------------------------------------------------

/**
 * Try to resolve a ref from the provider-level load cache or the scope cache.
 * Returns the LoadCacheEntry if found/created, or undefined if a new async
 * load needs to be started.
 */
function resolveRefFromCache<T extends Entity>(
	scope: FxtrScope,
	loadCache: LoadCache,
	ref: EntityRef<T>,
): LoadCacheEntry | undefined {
	const entityKey = ref.entityId.key();
	const codec = ref.loadCodec as EntityLoadCodec<Entity>;

	// Provider-level cache
	const existing = getLoadCacheEntry(loadCache, entityKey, codec);
	if (existing) return existing;

	// Scope cache fast path
	try {
		scope.lift(ref);
		const cached = scope.getCachedValue(ref);
		const entry: LoadCacheEntry = { promise: Promise.resolve(), value: cached };
		setLoadCacheEntry(loadCache, entityKey, codec, entry);
		return entry;
	} catch (e) {
		if (
			!(e instanceof EntityNotLoadedError) &&
			!(e instanceof Error && e.message.includes("not found"))
		) {
			throw e;
		}
	}

	return undefined;
}

/** Start a ref load, store it in the cache, and return the entry. */
function startRefLoad<T extends Entity>(
	scope: FxtrScope,
	loadCache: LoadCache,
	ref: EntityRef<T>,
): LoadCacheEntry {
	const entityKey = ref.entityId.key();
	const codec = ref.loadCodec as EntityLoadCodec<Entity>;

	const loadResult = scope.load(ref);

	// Eager path: resolve synchronously. Use Promise.resolve() as the
	// notification signal (already settled, but always a real Promise).
	if (loadResult instanceof Eager) {
		let entry: LoadCacheEntry;
		try {
			const value = loadResult.unwrap();
			if (!(value as Entity)[FXTR_CACHEABLE]) {
				entry = {
					promise: Promise.resolve(),
					error: new Error(
						`useFxtrLoad requires cacheable entities ([FXTR_CACHEABLE]: true). Entity ${ref.entityId.idString()} is not cacheable. Use useFxtrScope() + scope.load() for non-cacheable entities.`,
					),
				};
			} else {
				entry = { promise: Promise.resolve(), value };
			}
		} catch (error) {
			entry = { promise: Promise.resolve(), error };
		}
		setLoadCacheEntry(loadCache, entityKey, codec, entry);
		return entry;
	}

	// Promise path: settle asynchronously.
	const entry: LoadCacheEntry = {
		promise: loadResult.then(
			(value) => {
				if (!(value as Entity)[FXTR_CACHEABLE]) {
					entry.error = new Error(
						`useFxtrLoad requires cacheable entities ([FXTR_CACHEABLE]: true). Entity ${ref.entityId.idString()} is not cacheable. Use useFxtrScope() + scope.load() for non-cacheable entities.`,
					);
					return;
				}
				entry.value = value;
			},
			(error) => {
				entry.error = error;
			},
		),
	};
	setLoadCacheEntry(loadCache, entityKey, codec, entry);
	return entry;
}

// ---------------------------------------------------------------------------
// Callback-based load helpers (useFxtrLoadQuery only)
// ---------------------------------------------------------------------------

function resolveCallbackFromCache(
	callbackCache: CallbackCache,
	memoizedFn: (load: LoadFn) => PromiseOrEager<unknown>,
): CallbackCacheEntry | undefined {
	return callbackCache.get(memoizedFn);
}

function startCallbackLoad(
	scope: FxtrScope,
	callbackCache: CallbackCache,
	memoizedFn: (load: LoadFn) => PromiseOrEager<unknown>,
): CallbackCacheEntry {
	const loadFn: LoadFn = (ref) => scope.load(ref);
	const callbackResult = memoizedFn(loadFn);

	// Eager path: resolve synchronously. Promise.resolve() as notification signal.
	if (callbackResult instanceof Eager) {
		let entry: CallbackCacheEntry;
		try {
			entry = { promise: Promise.resolve(), value: callbackResult.unwrap() };
		} catch (error) {
			entry = { promise: Promise.resolve(), error };
		}
		callbackCache.set(memoizedFn, entry);
		return entry;
	}

	// Promise path.
	const entry: CallbackCacheEntry = {
		promise: callbackResult.then(
			(value) => {
				entry.value = value;
			},
			(error) => {
				entry.error = error;
			},
		),
	};
	callbackCache.set(memoizedFn, entry);
	return entry;
}

// ---------------------------------------------------------------------------
// useFxtrLoad
// ---------------------------------------------------------------------------

/**
 * Load an entity by ref, suspending via React Suspense if not yet cached.
 *
 * The load cache is stored on the FxtrProvider (not in a useRef), so it
 * survives component remounts that React performs during Suspense retries.
 *
 * Only works with cacheable entities ([FXTR_CACHEABLE]: true). This is the
 * default for entities created via bicodec.entity(). Non-cacheable entities
 * should be loaded manually via useFxtrScope() + scope.load().
 *
 * Must be used within a <FxtrProvider> and wrapped in a <Suspense> boundary.
 */
export function useFxtrLoad<T extends Entity>(ref: EntityRef<T>): T {
	const { scope, loadCache } = useFxtrContext();

	const cached = resolveRefFromCache(scope, loadCache, ref);
	if (cached) {
		if (cached.error) throw cached.error;
		if (cached.value !== undefined) return cached.value as T;
		throw cached.promise;
	}
	const entry = startRefLoad(scope, loadCache, ref);
	if (entry.error) throw entry.error;
	if (entry.value !== undefined) return entry.value as T;
	throw entry.promise;
}

// ---------------------------------------------------------------------------
// useFxtrLoadQuery
// ---------------------------------------------------------------------------

/**
 * Load an entity by ref without Suspense. Returns { data, loading, error }.
 *
 * Only works with cacheable entities ([FXTR_CACHEABLE]: true), same as
 * useFxtrLoad. Pass `enabled: false` to skip the load entirely.
 */
export function useFxtrLoadQuery<T extends Entity>(
	ref: EntityRef<T>,
	opts?: LoadQueryOptions,
): LoadStatus<T>;

/**
 * Run an async callback with a load function without Suspense.
 * Returns { data, loading, error }.
 *
 * The deps array controls memoization (like useMemo). No cacheability
 * enforcement. Pass `enabled: false` to skip the load entirely.
 */
export function useFxtrLoadQuery<R>(
	fn: (load: LoadFn) => PromiseOrEager<R>,
	deps: DependencyList,
	opts?: LoadQueryOptions,
): LoadStatus<R>;

export function useFxtrLoadQuery<T extends Entity, R>(
	refOrFn: EntityRef<T> | ((load: LoadFn) => PromiseOrEager<R>),
	depsOrOpts?: DependencyList | LoadQueryOptions,
	opts?: LoadQueryOptions,
): LoadStatus<T | R> {
	const { scope, loadCache, callbackCache } = useFxtrContext();
	const [, rerender] = useReducer((x: number) => x + 1, 0);
	const subscribedRef = useRef<Promise<void> | null>(null);
	// Parse overloaded args
	const isCb = isCallbackForm(refOrFn);
	const deps: DependencyList = isCb ? (depsOrOpts as DependencyList) : [refOrFn];
	const options: LoadQueryOptions = isCb ? (opts ?? {}) : ((depsOrOpts as LoadQueryOptions) ?? {});
	const enabled = options.enabled ?? true;

	// biome-ignore lint/correctness/useExhaustiveDependencies: deps are user-controlled (like useMemo)
	const memoizedFn = useMemo(() => (isCb ? refOrFn : null), deps);

	const idle: LoadStatus<T | R> = { data: undefined, loading: false, error: undefined };

	if (!enabled) return idle;

	/** Subscribe to an entry's promise, triggering rerender when it settles. */
	const subscribe = (promise: Promise<void>) => {
		if (subscribedRef.current !== promise) {
			subscribedRef.current = promise;
			promise.then(rerender, rerender);
		}
	};

	/** Convert a settled cache entry to a LoadStatus. */
	const fromSettled = (entry: { value?: unknown; error?: unknown }): LoadStatus<T | R> | null => {
		if (entry.error) return { data: undefined, loading: false, error: entry.error };
		if (entry.value !== undefined)
			return { data: entry.value as T | R, loading: false, error: undefined };
		return null;
	};

	if (memoizedFn) {
		// Callback form
		const existing = resolveCallbackFromCache(callbackCache, memoizedFn);
		if (existing) {
			const settled = fromSettled(existing);
			if (settled) return settled;
			subscribe(existing.promise);
			return { data: undefined, loading: true, error: undefined };
		}
		const entry = startCallbackLoad(scope, callbackCache, memoizedFn);
		const settled = fromSettled(entry);
		if (settled) return settled;
		subscribe(entry.promise);
		return { data: undefined, loading: true, error: undefined };
	}

	// Ref form
	const ref = refOrFn as EntityRef<T>;
	const cached = resolveRefFromCache(scope, loadCache, ref);
	if (cached) {
		const settled = fromSettled(cached);
		if (settled) return settled;
		subscribe(cached.promise);
		return { data: undefined, loading: true, error: undefined };
	}
	const entry = startRefLoad(scope, loadCache, ref);
	const settled = fromSettled(entry);
	if (settled) return settled;
	subscribe(entry.promise);
	return { data: undefined, loading: true, error: undefined };
}

// ---------------------------------------------------------------------------
// useFxtrScope
// ---------------------------------------------------------------------------

/**
 * Get a fresh mutable child scope of the current internal scope.
 *
 * The scope is created on first render and scheduleClose'd on unmount.
 * Use this for advanced cases: programmatic stores, complex async loads,
 * or bridging into the component tree via <FxtrSubscope include={[myScope]}>.
 */
// TODO(strict-mode): not StrictMode-safe. The child scope is created in
// render and stashed in a ref; the effect cleanup scheduleClose()s it.
// StrictMode's mount → cleanup → re-setup cycle closes the scope without
// re-rendering, leaving the ref null and the closed scope still referenced
// by anything that captured the returned value. Fix: switch to useState
// and create the scope inside the effect so setup/cleanup are symmetric.
// Same problem in FxtrProvider in context.tsx.
export function useFxtrScope(): FxtrScope {
	const { scope: parentScope } = useFxtrContext();

	const scopeRef = useRef<FxtrScope | null>(null);
	if (!scopeRef.current) {
		scopeRef.current = createFxtrScope({ include: [parentScope] });
	}

	useEffect(() => {
		return () => {
			scopeRef.current?.scheduleClose();
			scopeRef.current = null;
		};
	}, []);

	return scopeRef.current;
}
