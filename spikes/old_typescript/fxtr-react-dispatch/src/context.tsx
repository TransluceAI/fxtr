/**
 * DispatchProvider: sets up the dispatch handler context and cache.
 *
 * Sits inside a <FxtrProvider> in the tree. The cache maps
 * (dispatchKey, typeId) → resolved component, so repeated renders
 * of the same entity type don't re-resolve.
 */

import type { FxtrID } from "@fxtr/core";
import { type ReactNode, createContext, useContext, useRef } from "react";
import type { DispatchHandler, DispatchResolution } from "./types.js";

// ---------------------------------------------------------------------------
// Cache types
// ---------------------------------------------------------------------------

/**
 * Cache entry storing the result of a dispatch lookup.
 *
 * Props are erased here (the cache holds entries for all dispatch keys
 * in a single Map). The Props type is recovered via cast when the entry
 * is consumed in `<Render>`, which knows the key's Props type.
 *
 * `promise` is always a real `Promise<void>`, never an Eager — for eager
 * resolutions we use `Promise.resolve()`. This guarantees we never
 * accidentally throw an Eager at React Suspense.
 */
export interface DispatchCacheEntry {
	/** Async notification signal. Always a real Promise, never Eager. */
	promise: Promise<void>;
	/** Set once resolved successfully. Props type erased to `object`. */
	resolution?: DispatchResolution;
	/** Set if lookup fails. */
	error?: unknown;
}

/**
 * Two-level map: dispatch key symbol → typeId string → cache entry.
 *
 * The cache is per-provider instance and lives in a ref so it survives
 * React re-renders (including Suspense retries).
 */
export type DispatchCache = Map<symbol, Map<string, DispatchCacheEntry>>;

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

export interface DispatchContextValue {
	handler: DispatchHandler;
	cache: DispatchCache;
}

export const DispatchContext = createContext<DispatchContextValue | null>(null);

export function useDispatchContext(): DispatchContextValue {
	const ctx = useContext(DispatchContext);
	if (!ctx) {
		throw new Error("<Render> must be used within a <DispatchProvider>");
	}
	return ctx;
}

// ---------------------------------------------------------------------------
// Cache helpers
// ---------------------------------------------------------------------------

export function getCacheEntry(
	cache: DispatchCache,
	key: symbol,
	typeId: FxtrID,
): DispatchCacheEntry | undefined {
	return cache.get(key)?.get(typeId.key());
}

export function setCacheEntry(
	cache: DispatchCache,
	key: symbol,
	typeId: FxtrID,
	entry: DispatchCacheEntry,
): void {
	let inner = cache.get(key);
	if (!inner) {
		inner = new Map();
		cache.set(key, inner);
	}
	inner.set(typeId.key(), entry);
}

// ---------------------------------------------------------------------------
// DispatchProvider
// ---------------------------------------------------------------------------

/**
 * Provides a dispatch handler and cache to the subtree.
 *
 * Must be rendered inside a <FxtrProvider> (the dispatch system needs
 * access to fxtr scopes for entity loading and codec checks).
 *
 * If the `handler` prop changes (by reference identity), the dispatch
 * cache is cleared and all `<Render>` components re-resolve against
 * the new handler.
 *
 * ```tsx
 * <FxtrProvider scope={scope}>
 *   <DispatchProvider handler={myHandler}>
 *     <Render entity={someId} as={MainView} />
 *   </DispatchProvider>
 * </FxtrProvider>
 * ```
 */
export function DispatchProvider({
	handler,
	children,
}: {
	handler: DispatchHandler;
	children: ReactNode;
}) {
	const stateRef = useRef<DispatchContextValue | null>(null);

	if (!stateRef.current || stateRef.current.handler !== handler) {
		stateRef.current = { handler, cache: new Map() };
	}

	return <DispatchContext.Provider value={stateRef.current}>{children}</DispatchContext.Provider>;
}
