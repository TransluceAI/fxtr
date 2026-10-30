/**
 * `createStaticDispatcher` — the concrete `Dispatcher`
 * implementation backed by a passive `DispatchRegistry` (a bag
 * of `(DispatchKey, entry-factory)` pairs).
 *
 * Owns:
 *   - the transferred `Session` (detached on
 *     `dispatcher.detach()`);
 *   - the dispatcher's session-backed `userState` projection
 *     (top-level `entries[keyUuid][shortId]` shape);
 *   - the per-`(keyUuid, shortId)` entry cache;
 *   - the `closed` flag + derived `status` atom;
 *   - the optional per-key fallback overrides.
 *
 * Per `getEntry` call, it picks the entity- or service-side
 * registry table (by `instanceof`), looks up the
 * `DispatchKeyRegistryEntry`, projects the session sub-atoms
 * down to this entry's slot, merges the key's `fallbacks` with
 * any matching override, and hands everything to
 * `createBaseEntry`. The base helper owns the in-memory
 * machinery (component sink + atomEffect + derived
 * componentAtom).
 *
 * **Unknown-key handling.** If the registry has no entry for
 * the supplied `(key, stub-kind)` pair, `getEntry` still returns
 * an entry — its state machine is a no-op that publishes the
 * key's `fallbacks.NoMatch` (or the override's NoMatch).
 * Behaviourally symmetric with the dynamic dispatcher's
 * "service returned no renderer" terminal: pass either dispatcher
 * a key it can't resolve, get `NoMatchFallback`.
 *
 * Entries persist for the dispatcher's lifetime (until
 * `dispatcher.detach()`); there is no LRU or idle-eviction.
 * Entries are small (a handful of atoms each) and the session
 * layer's slot dedup already prevents duplicate wire loads when
 * two entries resolve to the same entity, so the trade-off is
 * cheap.
 */

import {
	EntityRefStub,
	type FxtrSessionAtom,
	ServiceRefStub,
	type Session,
	type StateValue,
} from "@fxtr-ui/core";
import { atomWithDefault, subAtom } from "@fxtr-ui/state-util";
import { type Atom, type PrimitiveAtom, atom } from "jotai";

import { Provider as JotaiProvider } from "jotai";
import { type ReactElement, createElement } from "react";
import { type EntryUserStateSlot, createBaseEntry } from "./base_entry";
import {
	type AnyDispatchEntry,
	type Dispatcher,
	DispatcherClosedError,
	type DispatcherStatus,
} from "./dispatcher";
import type { AnyDispatchKey, DispatchKey } from "./keys";
import { type KeyFallbackOverride, buildOverrideMap, resolveKeyFallbacks } from "./overrides";
import type { DispatchRegistry } from "./registry";
import type {
	DispatchEffectFactory,
	DispatchKeyRegistryEntry,
	DispatchedComponent,
	ErrorFallbackComponent,
	StubFallbacks,
} from "./registry_types";
import { StaticDispatcherDebugView } from "./static_debug_view";

/**
 * The static dispatcher's userState shape. Concrete dispatcher
 * implementations may extend this with their own top-level
 * sibling fields (caches, lookup tables) — the `entries` slot
 * layout itself is the shared per-`(stub, key)` shape from
 * `base_entry.ts` and stays consistent across them.
 */
export interface DispatcherUserState {
	readonly entries: Record<
		string /* DispatchKey.uuid */,
		Record<string /* stub.shortId */, EntryUserStateSlot>
	>;
}

export const EMPTY_DISPATCHER_USER_STATE: DispatcherUserState = { entries: {} };

export interface CreateStaticDispatcherOptions {
	readonly session: Session;
	readonly registry: DispatchRegistry;
	/**
	 * Optional per-key fallback overrides. Each override (built
	 * via `overrideKeyFallbacks(key, partial)`) replaces specific
	 * fallback fields on the matching key with a dispatcher-
	 * specific version — e.g. to wire `Loading` into a custom
	 * progress display. Absent fields fall through to the key's
	 * own `fallbacks`.
	 */
	readonly overrides?: ReadonlyArray<KeyFallbackOverride>;
}

export function createStaticDispatcher(opts: CreateStaticDispatcherOptions): Dispatcher {
	const { session, registry } = opts;
	const overrideMap = buildOverrideMap(opts.overrides);

	// Project session.userState through `atomWithDefault` so a
	// fresh session reads as `EMPTY_DISPATCHER_USER_STATE` rather
	// than `null`. The cast asserts the consumer is dedicating
	// this session to the dispatcher (no other writers with
	// conflicting top-level shapes).
	const rootAtom: FxtrSessionAtom<DispatcherUserState> = atomWithDefault(
		session.userState as unknown as FxtrSessionAtom<DispatcherUserState | null>,
		EMPTY_DISPATCHER_USER_STATE,
	);

	// Per-`(keyUuid, shortId)` cache. Keys are
	// `${keyUuid}::${shortId}` — also the entry's React key.
	const cache = new Map<string, AnyDispatchEntry>();

	// Reactive view of the cache's key set, for the debug surface.
	// `cache` is a plain Map (not jotai-reactive), so the debug
	// component subscribes to this primitive atom and re-renders
	// when a new entry is added. Updated synchronously in
	// `getEntry` whenever the cache grows.
	const mountedIdsAtom: PrimitiveAtom<readonly string[]> = atom<readonly string[]>([]);

	// Single source of truth for "is this dispatcher closed."
	// `status` derives from it so React consumers see the
	// transition; `getEntry` reads it synchronously via
	// `session.store.get`. `detach()` flips it.
	const closedAtom: PrimitiveAtom<boolean> = atom(false);

	const status: Atom<DispatcherStatus> = atom((get) =>
		get(closedAtom) ? "closed" : get(session.lifecycle),
	);

	function cacheSet(id: string, entry: AnyDispatchEntry): void {
		cache.set(id, entry);
		session.store.set(mountedIdsAtom, (prev) => [...prev, id]);
	}

	function getEntry(key: AnyDispatchKey, stub: EntityRefStub | ServiceRefStub): AnyDispatchEntry {
		if (session.store.get(closedAtom)) throw new DispatcherClosedError();

		const id = `${key.uuid}::${stub.shortId}`;
		const cached = cache.get(id);
		if (cached !== undefined) return cached;

		// Stub-kind dispatch by `instanceof`. The
		// `<RenderDispatch>` prop type already statically
		// enforces (stub kind ⊆ key.Allowed), so this runtime
		// check is just selecting the right registry table.
		const stubKind: "entity" | "service" =
			stub instanceof EntityRefStub
				? "entity"
				: stub instanceof ServiceRefStub
					? "service"
					: (() => {
							throw new Error(
								"StaticDispatcher.getEntry: stub is neither EntityRefStub nor ServiceRefStub. Pass a ref-stub from a connector's entityRef() / serviceRef() / interning path.",
							);
						})();

		const registryEntry =
			stubKind === "entity"
				? registry.forEntity(key as DispatchKey<object, "entity">)
				: registry.forService(key as DispatchKey<object, "service">);

		// Project: rootAtom -> entries -> [keyUuid] -> [shortId]
		// -> {dispatcher_state, component_shared_state}.
		// `atomWithDefault` substitutes empty values where
		// indexed access yields undefined so the chain reads
		// cleanly on a fresh session before anything has been
		// written.
		const entriesAtom = subAtom(rootAtom, "entries");
		const keyEntriesNullable = subAtom(entriesAtom, key.uuid);
		const keyEntriesAtom = atomWithDefault(
			keyEntriesNullable,
			{} as Record<string, EntryUserStateSlot>,
		);
		const entrySlotNullable = subAtom(keyEntriesAtom, stub.shortId);

		// Resolve fallbacks for this entry: pull from key,
		// merge with override (if any), pre-narrow Stub type.
		const baseFallbacks = (key as unknown as { fallbacks: object }).fallbacks;
		const fallbacks = resolveKeyFallbacks(baseFallbacks, overrideMap, key.uuid) as StubFallbacks<
			EntityRefStub | ServiceRefStub,
			object
		>;
		// The two terminal-rendering components used by both the
		// degenerate-entry path (unregistered key) and the normal
		// effect-fn path. Service-side fallbacks have no
		// `Builtin`; entity-side have it as optional. Both have
		// `Loading` / `NoMatch` / `Error` mandatory.
		const loadingFallback = fallbacks.Loading as DispatchedComponent<
			EntityRefStub | ServiceRefStub,
			object
		>;
		const errorFallback = fallbacks.Error as ErrorFallbackComponent<
			EntityRefStub | ServiceRefStub,
			object
		>;
		const noMatchFallback = fallbacks.NoMatch as DispatchedComponent<
			EntityRefStub | ServiceRefStub,
			object
		>;

		if (registryEntry === undefined) {
			// Unknown-key path: render NoMatch directly via a
			// no-op effect-fn that just writes the component
			// once. Same surface as a registered entry whose
			// state machine resolves to no-match — keeps the
			// static dispatcher and the dynamic dispatcher
			// behaviourally interchangeable.
			//
			// Use a marker state (`{phase: "no-match"}`) so
			// reattach preserves the "we settled at no-match"
			// decision rather than running the no-op again.
			type NoMatchState = { readonly phase: "no-match" };
			const unmatchedDefaultSlot: EntryUserStateSlot = {
				dispatcher_state: { phase: "no-match" } as unknown as StateValue,
				component_shared_state: null,
			};
			const unmatchedEntrySlotAtom = atomWithDefault(entrySlotNullable, unmatchedDefaultSlot);
			const unmatchedStateAtom = subAtom(
				unmatchedEntrySlotAtom,
				"dispatcher_state",
			) as unknown as FxtrSessionAtom<NoMatchState>;
			const unmatchedSharedAtom = subAtom(unmatchedEntrySlotAtom, "component_shared_state");
			const unmatchedEffectFactory: DispatchEffectFactory<
				EntityRefStub | ServiceRefStub,
				object,
				NoMatchState
			> = ({ componentAtom }) => {
				return (get, set) => {
					const state = get(unmatchedStateAtom);
					if (state.phase !== "no-match") {
						set(unmatchedStateAtom, { phase: "no-match" });
					}
					if (get(componentAtom) !== noMatchFallback) {
						set(componentAtom, noMatchFallback);
					}
				};
			};
			const entry = createBaseEntry<EntityRefStub | ServiceRefStub, object, NoMatchState>({
				id,
				stub,
				stateAtom: unmatchedStateAtom,
				sharedComponentStateAtom: unmatchedSharedAtom,
				initialComponent: loadingFallback,
				initialProgressMessage: "",
				effectFactory: unmatchedEffectFactory,
				ErrorFallback: errorFallback,
				fallbacks,
			});
			cacheSet(id, entry);
			return entry;
		}

		// Normal path: registry-supplied state machine.
		// Default the slot from the registry entry's `initialState`.
		const defaultSlot: EntryUserStateSlot = {
			dispatcher_state: registryEntry.initialState as StateValue,
			component_shared_state: null,
		};
		const entrySlotAtom = atomWithDefault(entrySlotNullable, defaultSlot);
		const stateAtom = subAtom(
			entrySlotAtom,
			"dispatcher_state",
		) as unknown as FxtrSessionAtom<unknown>;
		const sharedAtomForRegistered = subAtom(entrySlotAtom, "component_shared_state");

		// Erase the entity/service union on `registryEntry` so
		// the component / factory / fallback fields land at the
		// union-stub type. The union narrows weirdly under
		// `DispatchedComponent<Stub, P>`'s contravariance in
		// `Stub`: an entity-side fallback rejects a service stub
		// statically, even though we've already routed by
		// `instanceof` at runtime.
		const erased = registryEntry as unknown as DispatchKeyRegistryEntry<
			EntityRefStub | ServiceRefStub,
			object,
			unknown
		>;
		const entry = createBaseEntry({
			id,
			stub,
			stateAtom,
			sharedComponentStateAtom: sharedAtomForRegistered,
			initialComponent: loadingFallback,
			initialProgressMessage: erased.initialProgressMessage ?? "",
			effectFactory: erased.effectFn,
			ErrorFallback: errorFallback,
			fallbacks,
		});

		cacheSet(id, entry);
		return entry;
	}

	// Idempotent detach: concurrent / repeated calls await the
	// same in-flight promise. The first call flips the flag,
	// drops the cache, and awaits the session detach; later
	// calls return the same promise.
	let detachPromise: Promise<void> | null = null;

	function detach(): Promise<void> {
		if (detachPromise !== null) return detachPromise;
		detachPromise = (async () => {
			// `session.store` is the jotai store the session's
			// internal atoms (including `lifecycle`) live in;
			// `closedAtom` is a plain atom we set imperatively.
			// Reusing `session.store` matches the single-store
			// assumption documented for `<DispatcherProvider>`.
			session.store.set(closedAtom, true);
			cache.clear();
			session.store.set(mountedIdsAtom, []);
			await session.detach();
		})();
		return detachPromise;
	}

	function debugView(): ReactElement {
		// Wrap in a jotai `<Provider store={session.store}>` so the
		// debug view's `useAtomValue` calls read from the same
		// store the dispatcher writes to. Without this, the view's
		// reads go to the surrounding consumer's store (or the
		// default), missing the dispatcher's updates.
		//
		// `.ts` file (not `.tsx`) so we use `createElement` rather
		// than JSX. The static debug view component itself lives
		// in `static_debug_view.tsx` and uses plain JSX.
		return createElement(
			JotaiProvider,
			{ store: session.store },
			createElement(StaticDispatcherDebugView, {
				status,
				sessionId: session.sessionId,
				sessionLifecycle: session.lifecycle,
				mountedIds: mountedIdsAtom,
				getEntryById: (id: string) => cache.get(id),
			}),
		);
	}

	return {
		status,
		session,
		getEntry,
		detach,
		debugView,
	};
}
