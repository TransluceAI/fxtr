/**
 * <Render> — the user-facing dispatch component.
 *
 * Resolves an entity's type, dispatches to the appropriate renderer
 * via the DispatchHandler, and renders it with the provided props.
 *
 * Flow:
 * 1. Load entity as UnknownEntity to extract its $fxtr.type
 * 2. Look up (dispatchKey, typeId) in the dispatch cache
 * 3. Render the resolved Component, or show a loading fallback / suspend
 *
 * The entity load is managed directly (not via useFxtrLoad) so that if the
 * handler provides a loading fallback, we can show it during the entity load
 * phase too — rather than unconditionally suspending.
 */

import {
	Eager,
	EntityRef,
	type FxtrCbor,
	type FxtrID,
	RawStrongFxtrID,
	RawWeakFxtrID,
	type UnknownEntity,
	unknownLoadCodec,
} from "@fxtr/core";
import { useFxtrScope } from "@fxtr/react";
import { useReducer, useRef } from "react";
import {
	type DispatchCacheEntry,
	getCacheEntry,
	setCacheEntry,
	useDispatchContext,
} from "./context.js";
import type { DispatchKey, DispatchResolution, ExtractDispatchProps } from "./types.js";

// ---------------------------------------------------------------------------
// Type ID extraction (duplicated from unionLoadCodec.ts — 5 lines, not exported)
// ---------------------------------------------------------------------------

function extractTypeId(raw_cbor: FxtrCbor): FxtrID {
	const typeField = (raw_cbor as Record<string, FxtrCbor>)["$fxtr.type"];
	if (typeField === undefined) {
		throw new Error("Entity CBOR is missing $fxtr.type field");
	}
	if (typeField instanceof RawWeakFxtrID) return typeField.innerId;
	if (typeField instanceof RawStrongFxtrID) return typeField.innerId;
	throw new Error(
		`Expected RawWeakFxtrID or RawStrongFxtrID for $fxtr.type, got ${typeof typeField}`,
	);
}

// ---------------------------------------------------------------------------
// Entity load state (managed directly, not via useFxtrLoad)
// ---------------------------------------------------------------------------

/**
 * Tracks the state of loading an entity as UnknownEntity.
 *
 * `promise` is always a real `Promise<void>`, never an Eager — for eager
 * resolutions we use `Promise.resolve()`. This guarantees we never
 * accidentally throw an Eager at React Suspense.
 */
interface EntityLoadState {
	/** The entity ID key this state was created for. */
	entityKey: string;
	/** Async notification signal. Always a real Promise, never Eager. */
	promise: Promise<void>;
	/** Set once loaded successfully. */
	value?: UnknownEntity;
	/** Set if load fails. */
	error?: unknown;
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

/**
 * Props for <Render>. Generic over the dispatch key so that extra props
 * are type-checked against the key's Props type.
 *
 * ```tsx
 * const MainView = declareDispatchKey<{ highlighted?: boolean }>("MainView");
 *
 * <Render entity={articleId} as={MainView} highlighted />
 * //                                       ^^^^^^^^^^^ type-checked
 * ```
 */
export type RenderProps<K extends DispatchKey> = {
	entity: FxtrID;
	as: K;
} & ExtractDispatchProps<K>;

/**
 * Dispatch-render an fxtr entity.
 *
 * Must be inside both a `<FxtrProvider>` and a `<DispatchProvider>`.
 * Should be wrapped in a `<Suspense>` boundary (unless the handler
 * provides a loading fallback for the key via `getLoadingFallback`).
 */
export function Render<K extends DispatchKey>(props: RenderProps<K>) {
	const {
		entity,
		as: key,
		...extraProps
	} = props as {
		entity: FxtrID;
		as: K;
	} & Record<string, unknown>;
	const typedExtraProps = extraProps as ExtractDispatchProps<K>;
	const { handler, cache } = useDispatchContext();
	const scope = useFxtrScope();
	const [, rerender] = useReducer((x: number) => x + 1, 0);
	const subscribedRef = useRef<Promise<void> | null>(null);
	const loadStateRef = useRef<EntityLoadState | null>(null);

	type Props = ExtractDispatchProps<K>;

	// Contract: <Render> dispatches by loading the entity to extract its
	// $fxtr.type. IDs with tags '.' (the "here" anonymous node) or '!'
	// (builtin) carry no loadable content, so they can't be dispatched.
	// Fail-fast here rather than letting a confusing "contentless ID"
	// error surface from deep inside the arena. Callers (e.g. JsonTree
	// walking CBOR) should filter these out and render them locally.
	const tag = entity.getTag();
	if (tag === "." || tag === "!") {
		throw new Error(
			`<Render> cannot dispatch contentless FxtrID '${entity.idString()}' — IDs with tag '${tag}' carry no loadable entity. Filter contentless IDs before passing to <Render>.`,
		);
	}

	// Step 1: Load entity as unknown to extract its type ID.
	// Managed directly so we can show the loading fallback during this phase
	// instead of unconditionally suspending.
	const entityKey = entity.key();
	if (!loadStateRef.current || loadStateRef.current.entityKey !== entityKey) {
		const unknownRef = new EntityRef(entity, unknownLoadCodec);
		const loadResult = scope.load(unknownRef);
		if (loadResult instanceof Eager) {
			try {
				loadStateRef.current = {
					entityKey,
					promise: Promise.resolve(),
					value: loadResult.unwrap(),
				};
			} catch (error) {
				loadStateRef.current = { entityKey, promise: Promise.resolve(), error };
			}
		} else {
			const state: EntityLoadState = {
				entityKey,
				promise: loadResult.then(
					(value) => {
						state.value = value;
					},
					(error) => {
						state.error = error;
					},
				),
			};
			loadStateRef.current = state;
		}
	}

	const loadState = loadStateRef.current;
	if (loadState.error) throw loadState.error;
	if (!loadState.value) {
		// Entity data still loading — show fallback or suspend.
		return showPendingOrSuspend(
			key,
			entity,
			typedExtraProps,
			loadState.promise,
			handler,
			subscribedRef,
			rerender,
		);
	}

	const typeId = extractTypeId(loadState.value.raw_cbor);

	// Step 2: Check dispatch cache.
	// The cache stores Props-erased entries; we cast back to the key's
	// Props type. Sound because the cache is keyed by the dispatch key
	// symbol, so entries for a given key always have the matching Props type.
	const existing = getCacheEntry(cache, key, typeId);
	if (existing) {
		if (existing.error) throw existing.error;
		if (existing.resolution) {
			const { Component } = existing.resolution as DispatchResolution<Props>;
			return <Component entityId={entity} {...typedExtraProps} />;
		}
		// Still pending — show loading or suspend.
		return showPendingOrSuspend(
			key,
			entity,
			typedExtraProps,
			existing.promise,
			handler,
			subscribedRef,
			rerender,
		);
	}

	// Step 3: Cache miss — call handler.lookup().
	const lookupResult = handler.lookup(key, typeId, scope);

	// Eager path: lookup resolved synchronously — render immediately.
	if (lookupResult instanceof Eager) {
		let entry: DispatchCacheEntry;
		try {
			const resolution = lookupResult.unwrap();
			entry = { promise: Promise.resolve(), resolution };
		} catch (error) {
			entry = { promise: Promise.resolve(), error };
		}
		setCacheEntry(cache, key, typeId, entry);
		if (entry.error) throw entry.error;
		const { Component } = entry.resolution as DispatchResolution<Props>;
		return <Component entityId={entity} {...typedExtraProps} />;
	}

	// Async path: lookup returned a Promise.
	const entry: DispatchCacheEntry = {
		promise: lookupResult.then(
			(resolution) => {
				entry.resolution = resolution;
			},
			(error) => {
				entry.error = error;
			},
		),
	};
	setCacheEntry(cache, key, typeId, entry);

	return showPendingOrSuspend(
		key,
		entity,
		typedExtraProps,
		entry.promise,
		handler,
		subscribedRef,
		rerender,
	);
}

// ---------------------------------------------------------------------------
// Internal: show loading fallback or suspend
// ---------------------------------------------------------------------------

/**
 * Shared pending handler for both the entity load phase and the dispatch
 * lookup phase. If the handler provides a loading fallback for the key,
 * shows it and subscribes for re-render. Otherwise suspends.
 */
function showPendingOrSuspend<K extends DispatchKey>(
	key: K,
	entityId: FxtrID,
	extraProps: ExtractDispatchProps<K>,
	promise: Promise<void>,
	handler: {
		getLoadingFallback?<Props extends object>(
			key: DispatchKey<Props>,
		): React.ComponentType<{ entityId: FxtrID } & Props> | undefined;
	},
	subscribedRef: React.RefObject<Promise<void> | null>,
	rerender: () => void,
) {
	const Loading = handler.getLoadingFallback?.(key);
	if (Loading) {
		if (subscribedRef.current !== promise) {
			subscribedRef.current = promise;
			promise.then(rerender, rerender);
		}
		return <Loading entityId={entityId} {...extraProps} />;
	}

	// No loading fallback — suspend.
	throw promise;
}
