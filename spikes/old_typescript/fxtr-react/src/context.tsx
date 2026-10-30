/**
 * React context and FxtrProvider for managing the internal scope.
 *
 * A single FxtrContext provides the scope, ref-based load cache, and
 * callback-based load cache to the subtree.  Every scope boundary
 * (FxtrProvider, FxtrSubscope) creates a fresh context value so that
 * caches are isolated per boundary.
 */

import type { Entity, EntityLoadCodec } from "@fxtr/core";
import { type FxtrScope, createFxtrScope } from "@fxtr/core";
import { type ReactNode, createContext, useEffect, useRef } from "react";

// ---------------------------------------------------------------------------
// Cache types
// ---------------------------------------------------------------------------

/**
 * Cache entry for a ref-based entity load.
 *
 * `promise` is always a real `Promise<void>`, never an Eager — for eager
 * resolutions we use `Promise.resolve()`. This guarantees by construction
 * that we never accidentally throw an Eager at React Suspense. The promise
 * serves as an async notification signal: you only look at it if value and
 * error are both unset and you need to wait.
 */
export interface LoadCacheEntry {
	promise: Promise<void>;
	value?: Entity;
	error?: unknown;
}

/**
 * Nested map: entityId.key() -> Map<EntityLoadCodec, LoadCacheEntry>.
 * Each scope boundary gets its own cache.
 */
export type LoadCache = Map<string, Map<EntityLoadCodec<Entity>, LoadCacheEntry>>;

/** Cache entry for a callback-based load. Same promise semantics as LoadCacheEntry. */
export interface CallbackCacheEntry {
	promise: Promise<void>;
	value?: unknown;
	error?: unknown;
}

/**
 * WeakMap keyed by memoized callback identity.
 * Each scope boundary gets its own cache.
 */
// biome-ignore lint/suspicious/noExplicitAny: keys are memoized callback identities with arbitrary signatures
export type CallbackCache = WeakMap<(...args: any[]) => any, CallbackCacheEntry>;

// ---------------------------------------------------------------------------
// Unified context
// ---------------------------------------------------------------------------

export interface FxtrContextValue {
	scope: FxtrScope;
	loadCache: LoadCache;
	callbackCache: CallbackCache;
}

/** @internal — the unified fxtr context for the current subtree. */
export const FxtrContext = createContext<FxtrContextValue | null>(null);

// ---------------------------------------------------------------------------
// Helper: create a fresh context value
// ---------------------------------------------------------------------------

export function createFxtrContextValue(scope: FxtrScope): FxtrContextValue {
	return { scope, loadCache: new Map(), callbackCache: new WeakMap() };
}

// ---------------------------------------------------------------------------
// FxtrProvider
// ---------------------------------------------------------------------------

interface ProviderState {
	ctx: FxtrContextValue;
	parentScope: FxtrScope;
}

/**
 * Root provider for fxtr React integration.
 *
 * Accepts any FxtrScope (frozen or not) and creates an internal child scope
 * that the hooks operate on. The internal scope is cleaned up when the
 * provider unmounts.
 *
 * If the `scope` prop changes (by reference identity), the provider closes
 * the old child scope, creates a fresh one from the new parent, and clears
 * all load caches. This forces the subtree to re-resolve entities against
 * the new scope.
 */
// TODO(strict-mode): this provider is not StrictMode-safe. The internal
// scope is created during render and stashed in a ref; the effect cleanup
// scheduleClose()s it. Under StrictMode's mount → cleanup → re-setup cycle,
// cleanup closes the scope but no re-render happens to recreate it, so
// committed children (and any later useFxtrScope() calls via the stale ctx)
// end up referencing a closed scope. Fix: move scope creation into the
// effect setup so cleanup/setup are symmetric (and accept one render where
// ctx is null). Same problem in useFxtrScope in hooks.ts.
export function FxtrProvider({ scope, children }: { scope: FxtrScope; children: ReactNode }) {
	const stateRef = useRef<ProviderState | null>(null);

	if (!stateRef.current || stateRef.current.parentScope !== scope) {
		stateRef.current?.ctx.scope.scheduleClose();
		stateRef.current = {
			ctx: createFxtrContextValue(
				createFxtrScope({ name: "fxtr-react-provider", include: [scope] }),
			),
			parentScope: scope,
		};
	}

	useEffect(() => {
		return () => {
			stateRef.current?.ctx.scope.scheduleClose();
			stateRef.current = null;
		};
	}, []);

	return <FxtrContext.Provider value={stateRef.current.ctx}>{children}</FxtrContext.Provider>;
}
