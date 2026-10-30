/**
 * `createDynamicDispatcher` — a `Dispatcher` implementation that
 * resolves renderers at runtime through a remote dispatcher service
 * + a session-backed type/renderer cache + an in-memory ESModule
 * cache.
 *
 * Unlike the static dispatcher, the dynamic dispatcher carries no
 * per-key registry. Every `DispatchKey` already declares its own
 * fallback components (Loading / NoMatch / Error / optional
 * Builtin); the dispatcher reads those off the key directly, with
 * optional `overrides` for per-key fallback customisation. This
 * lets the dispatcher resolve any key thrown at it — including keys
 * declared inside renderer modules loaded at runtime.
 *
 * Per-entry state machine:
 *
 *   ENTITY:
 *     init
 *       → resolved-builtin (terminal) for `.` / `!` shortIds
 *       → loading-entity (allocateLoad) otherwise
 *     loading-entity
 *       → errored (load failed)
 *       → errored (loaded value missing $fxtr.type EntityRefStub)
 *       → resolving-renderer (typeRef extracted)
 *     resolving-renderer
 *       → loading-renderer-bytes (cache hit on
 *         type_renderer_cache[typeRef.shortId][key.uuid])
 *       → wait (an in-flight invoke for this (typeRef, key.uuid)
 *         pair is already pending in type_renderer_active_lookups)
 *       → wait + write a fresh `resolve(typeRef, key.uuid)` invoke
 *         into type_renderer_active_lookups (cold path)
 *       → errored (invoke errored, or the result didn't match
 *         the {subtype_key, renderer} shape)
 *     loading-renderer-bytes
 *       → kick `ensureCompiled` (in-memory cache miss): async
 *         loads the renderer entity's bytes, compiles to ESM,
 *         caches by renderer.shortId
 *       → wait (compile in progress)
 *       → errored (load or compile failed)
 *       → publish the resolved component from
 *         `module[subtype_key][key.uuid]`, or NoMatch + console.warn
 *         on subtype/key.uuid miss
 *
 *   SERVICE:
 *     init
 *       → resolving-renderer (cached typeRef in service_type_cache)
 *       → loading-service-type (using a shared in-flight invoke
 *         from service_type_active_lookups, or allocating a new one)
 *         otherwise
 *     loading-service-type
 *       → errored (invoke failed or returned a non-entity)
 *       → resolving-renderer — in one flush: promote into
 *         service_type_cache, delete from
 *         service_type_active_lookups (drops the slot's wire footprint)
 *     resolving-renderer / loading-renderer-bytes — same as
 *       the entity branch.
 *
 * The in-memory compiled-module cache (`compiledModulesAtom`) is
 * keyed by renderer entity short_id and shared across all entries
 * targeting the same renderer. It evaporates on page reload;
 * reattach to an existing session repopulates on first reference
 * via the same `ensureCompiled` path.
 *
 * See `projects/fxtr_workstation/plans/phase0/part3_frontend_ui/stage10b_dynamic_dispatch.md`
 * for the full design context.
 */

import {
	type CallbackInboxStub,
	EntityRefStub,
	type FxtrSessionAtom,
	type OpResultStub,
	ServiceRefStub,
	type Session,
	type StateValue,
} from "@fxtr-ui/core";
import { atomWithDefault, subAtom } from "@fxtr-ui/state-util";
import {
	type Atom,
	Provider as JotaiProvider,
	type PrimitiveAtom,
	type Setter,
	atom,
	useAtomValue,
} from "jotai";
import { atomEffect } from "jotai-effect";
import { type ReactElement, createElement } from "react";

import { DynamicDispatcherDebugView } from "./dynamic_debug_view";
import { type EntryWrapper, makeEntryWrapper } from "./staleness_wrapper";

import { type EntryUserStateSlot, createBaseEntry } from "./base_entry";
import {
	type AnyDispatchEntry,
	type Dispatcher,
	DispatcherClosedError,
	type DispatcherStatus,
} from "./dispatcher";
import type { AnyKeyFallbacks, EntityKeyFallbacks, ServiceKeyFallbacks } from "./fallbacks";
import type { AnyDispatchKey } from "./keys";
import { type KeyFallbackOverride, buildOverrideMap, resolveKeyFallbacks } from "./overrides";
import type {
	DispatchEffectFactory,
	DispatchedComponent,
	ErrorFallbackComponent,
} from "./registry_types";

// --- userState shape -------------------------------------------------

/**
 * One renderer-resolution wire value: the dispatcher service's
 * `resolve(type, key_uuid)` returns this shape (`subtype_key` opaque
 * to the frontend; `renderer` carries the renderer entity's ref).
 * Cached into `userState.type_renderer_cache` and lookup-shared via
 * `type_renderer_active_lookups` (both nested by
 * `[typeShortId][keyUuid]`; the in-flight invoke is removed from
 * `active_lookups` and the answer promoted into `cache` in the same
 * flush; the wire slot's footprint drops immediately).
 *
 * A `null` result is the dispatcher's "no match" signal — the
 * backend has no renderer for the supplied (type, dispatch key)
 * pair. The frontend caches the null the same way as a hit (so
 * subsequent dispatches for the same (type, key) don't re-invoke),
 * and routes the affected entry to its dispatch key's
 * ``NoMatchFallback`` via the ``no-match`` terminal phase.
 */
export interface RendererResolution {
	readonly subtype_key: string;
	readonly renderer: EntityRefStub;
}

/**
 * The dynamic dispatcher's `userState` layout. The four sibling
 * cache fields survive across reattach (the workstation guarantees
 * entity short_ids are stable for the server's lifetime, so the
 * caches never need invalidation). `*_active_lookups` slots are
 * promoted into the matching `*_cache` as soon as they fill and
 * removed in the same updater, so the slot's wire footprint drops
 * immediately.
 *
 * The renderer caches are nested `Record<typeShortId, Record<keyUuid,
 * ...>>` because `WorkstationDispatcherProto.resolve` now takes both
 * the type ref *and* the dispatch-key uuid — backends can route
 * different keys for the same type to different renderer modules. The
 * inner record is keyed by the dispatch key's stringified uuid.
 */
export interface DynamicDispatcherUserState {
	readonly entries: Record<
		string /* DispatchKey.uuid */,
		Record<string /* stub.shortId */, EntryUserStateSlot>
	>;
	readonly service_type_cache: Record<string /* service shortId */, EntityRefStub>;
	readonly service_type_active_lookups: Record<
		string /* service shortId */,
		OpResultStub<EntityRefStub>
	>;
	readonly type_renderer_cache: Record<
		string /* type shortId */,
		Record<string /* keyUuid */, RendererResolution | null>
	>;
	readonly type_renderer_active_lookups: Record<
		string /* type shortId */,
		Record<string /* keyUuid */, OpResultStub<RendererResolution | null>>
	>;
	/**
	 * Callback inbox the dispatcher uses to receive
	 * :class:`ReloadEverythingNotification` events from the backend's
	 * ``subscribe`` event source (the dispatcher service re-declares
	 * the fxtr2 ``EventSource.subscribe``). ``null`` until the
	 * dispatcher's lifecycle effect allocates the callback +
	 * dispatches the ``subscribe_to`` invoke *on the inbox itself*
	 * (a callback-target invoke — same tick as the allocate, no
	 * round trip). The inbox survives reattach — the next mount
	 * picks it back up from userState and continues watching for
	 * events; the workstation-side subscription survived with it.
	 *
	 * The payload type is ``EntityRefStub`` because each reload
	 * notification is broadcast as an entity ref (the workstation
	 * decodes the backend's ``ReloadEverythingNotification`` payload
	 * into one). The dispatcher doesn't inspect the value — only the
	 * count of arrived events matters.
	 */
	readonly reload_inbox: CallbackInboxStub<EntityRefStub> | null;
	/**
	 * Result stub for the ``subscribe_to(dispatchServiceRef)``
	 * callback-target invoke that subscribes the inbox to the
	 * dispatcher's change events. Kept in userState alongside
	 * ``reload_inbox`` because the workstation session's
	 * slot-allocation contract requires every allocated stub to be
	 * referenced from userState by end-of-flush — otherwise the
	 * slot gets garbage-collected before its ``CallbackInvokeOp``
	 * wire emission. The dispatcher doesn't read the return value
	 * (``subscribe_to`` returns ``None``); the field exists purely
	 * to anchor the slot.
	 */
	readonly reload_subscribe_result: OpResultStub<null> | null;
}

export const EMPTY_DYNAMIC_DISPATCHER_USER_STATE: DynamicDispatcherUserState = {
	entries: {},
	service_type_cache: {},
	service_type_active_lookups: {},
	type_renderer_cache: {},
	type_renderer_active_lookups: {},
	reload_inbox: null,
	reload_subscribe_result: null,
};

// --- Per-entry state-machine state -----------------------------------

/**
 * Entity-side dispatch state machine. Carries values needed to drive
 * the next transition; terminal phases carry only what
 * `componentAtom` re-syncs against on reattach.
 */
export type DynamicEntityDispatchState =
	| { readonly phase: "init" }
	| { readonly phase: "loading-entity"; readonly load: OpResultStub<unknown> }
	| { readonly phase: "resolving-renderer"; readonly typeRef: EntityRefStub }
	| {
			readonly phase: "loading-renderer-bytes";
			readonly subtype_key: string;
			readonly renderer: EntityRefStub;
	  }
	| { readonly phase: "resolved-builtin" }
	| { readonly phase: "no-match" }
	| { readonly phase: "errored"; readonly message: string };

/**
 * Service-side dispatch state machine. Symmetric to the entity
 * branch from `resolving-renderer` onward; `loading-service-type`
 * replaces `loading-entity`.
 */
export type DynamicServiceDispatchState =
	| { readonly phase: "init" }
	| {
			readonly phase: "loading-service-type";
			readonly typeInvoke: OpResultStub<EntityRefStub>;
	  }
	| { readonly phase: "resolving-renderer"; readonly typeRef: EntityRefStub }
	| {
			readonly phase: "loading-renderer-bytes";
			readonly subtype_key: string;
			readonly renderer: EntityRefStub;
	  }
	| { readonly phase: "no-match" }
	| { readonly phase: "errored"; readonly message: string };

/**
 * `.` / `!` / `c` / `u` shortIds short-circuit to the
 * `resolved-builtin` terminal phase, which publishes the dispatch
 * key's `Builtin` fallback (or `NoMatch` if the key has no Builtin).
 * No entity load + no `resolve()` invoke is issued. Mirrors the static
 * dispatcher's `isBuiltinEntityShortId` in `static_entity_registry.ts`
 * — **keep the two in sync.**
 *
 * `c`- and `u`-nodes are included even though their tags aren't
 * literally a "builtin": they're system-defined provenance forms that
 * carry no loadable content (a `u`-node is the "untracked provenance"
 * wrap of another node; a `c`-node carries metadata only, its contents
 * living on its `context_data` d-node) — both reachable via
 * `allocateGetEntityMetadata`, neither replaceable with a user
 * renderer. Trying to load them hits a content-less node; lumping them
 * under the builtin treatment keeps the dispatcher's "stubs we won't
 * try to load" set in one place.
 */
function isBuiltinEntityShortId(shortId: string): boolean {
	return (
		shortId.startsWith(".") ||
		shortId.startsWith("!") ||
		shortId.startsWith("c") ||
		shortId.startsWith("u")
	);
}

/**
 * Runtime shape-check for a `resolve()` invoke result. The wire
 * codec produces either `null` (the dispatcher's "no match" signal)
 * or a plain object whose `renderer` field is an `EntityRefStub`.
 * Anything else surfaces as an `errored` terminal phase rather than
 * crashing further downstream.
 */
function isResolveResultShape(value: unknown): value is RendererResolution | null {
	if (value === null) return true;
	if (typeof value !== "object") return false;
	const v = value as Record<string, unknown>;
	return typeof v.subtype_key === "string" && v.renderer instanceof EntityRefStub;
}

// --- The dispatcher --------------------------------------------------

/**
 * In-memory cache status for one renderer entity's compiled
 * module. Populated by `ensureCompiled`; consulted by the
 * `loading-renderer-bytes` state-machine arm.
 */
export type CompiledModuleStatus =
	| { readonly status: "loading" }
	| { readonly status: "ready"; readonly module: CompiledRendererModule }
	| { readonly status: "errored"; readonly message: string };

/**
 * The shape of a renderer entity's compiled default export — a
 * two-level lookup `module[subtype_key][dispatch_key_uuid]` →
 * `Component`. The outer key is opaque to the frontend (the
 * dispatcher service decides what subtype labels mean); the inner
 * key is the rendering-slot `DispatchKey.uuid`.
 *
 * Components are not constrained at this layer — the renderer
 * module is responsible for matching the props shape its declared
 * dispatch key expects. Runtime mismatches surface as React-side
 * errors caught by the per-entry error boundary.
 */
export type CompiledRendererModule = Readonly<
	Record<
		string,
		Readonly<Record<string, DispatchedComponent<EntityRefStub | ServiceRefStub, object>>>
	>
>;

/**
 * Loaded shape of a renderer entity. Matches the Python-side
 * `RendererEntity` `DataclassEntity` definition: a single `module`
 * field carrying the compiled ESM bytes. The wire codec decodes
 * the `bytes`-tagged field into a `Uint8Array` on this side.
 */
export interface RendererEntityLoadedShape {
	readonly module: Uint8Array;
	// Other DataclassEntity-injected fields (e.g. `$fxtr.type`,
	// `$fxtr.ctx`) may be present; we ignore them.
}

export interface CreateDynamicDispatcherOptions {
	readonly session: Session;
	/**
	 * The remote dispatcher service. The state machine invokes
	 * `resolve(type_ref)` on this ref to look up a renderer for a
	 * given type. Per the design doc, the consumer obtains this
	 * ref via the workstation connector — typically by looking up
	 * a known external name like `"renderer-dispatcher"`.
	 */
	readonly dispatchServiceRef: ServiceRefStub<unknown>;
	/**
	 * Async out-of-band entity loader. Typically
	 * `connector.loadEntityData.bind(connector)` — the workstation
	 * connector's `LoadEntityRequest` path. The dispatcher uses it
	 * to fetch the bytes of a renderer entity before compiling.
	 * Taken as an option rather than a connector reference so the
	 * dispatch package stays connector-agnostic and so tests can
	 * inject controllable mocks.
	 */
	readonly loadEntityData: (ref: EntityRefStub) => Promise<unknown>;
	/**
	 * Optional override for the bytes-to-module compile step. The
	 * default implementation wraps the bytes in a Blob, creates a
	 * blob URL, `await import(url)`s it, and returns
	 * `module.default`. This requires a real browser ESM loader —
	 * jsdom-based tests can't exercise it. Tests inject a mock
	 * here that returns a hand-built module map directly.
	 *
	 * Production deployments must arrange for the import map (set
	 * up via `installFxtrExternals` from
	 * `@fxtr-ui/react-dispatch-dynamic-shim`) to be in place
	 * before any renderer load runs.
	 */
	readonly compileRendererBytes?: (bytes: Uint8Array) => Promise<CompiledRendererModule>;
	/**
	 * Optional per-key fallback overrides. Each override (built via
	 * `overrideKeyFallbacks(key, partial)`) replaces specific
	 * fallback fields on the matching key with a dispatcher-
	 * specific version. Absent fields fall through to the key's
	 * own `fallbacks` declaration. Unlike the static dispatcher,
	 * the dynamic dispatcher carries no per-key registration — the
	 * key itself supplies everything; `overrides` is purely
	 * additive customisation.
	 */
	readonly overrides?: ReadonlyArray<KeyFallbackOverride>;
}

export function createDynamicDispatcher(opts: CreateDynamicDispatcherOptions): Dispatcher {
	const { session, dispatchServiceRef, loadEntityData } = opts;
	const compileRendererBytes = opts.compileRendererBytes ?? defaultCompileRendererBytes;
	const overrideMap = buildOverrideMap(opts.overrides);

	const rootAtom: FxtrSessionAtom<DynamicDispatcherUserState> = atomWithDefault(
		session.userState as unknown as FxtrSessionAtom<DynamicDispatcherUserState | null>,
		EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
	);

	const cache = new Map<string, AnyDispatchEntry>();
	// Per-entry private staleness atom. Set to `true` during a
	// reload (see chunk 4); cleared by the state machine when it
	// next reaches a terminal phase. Kept in a parallel map so the
	// reload effect can iterate without exposing the atom on the
	// `DispatchEntry` interface.
	const staleAtoms = new Map<string, PrimitiveAtom<boolean>>();
	// Reactive view of the cache's key set, for the debug surface
	// (`debugView()`). The `cache` Map isn't jotai-reactive, so
	// the debug component subscribes to this primitive atom and
	// re-renders when a new entry is added.
	const mountedIdsAtom: PrimitiveAtom<readonly string[]> = atom<readonly string[]>([]);
	const closedAtom: PrimitiveAtom<boolean> = atom(false);
	const status: Atom<DispatcherStatus> = atom((get) =>
		get(closedAtom) ? "closed" : get(session.lifecycle),
	);

	// Sub-atoms for the four sibling session-state cache fields.
	// Captured once so per-entry state machines share them.
	const serviceTypeCacheAtom = subAtom(rootAtom, "service_type_cache");
	const serviceTypeActiveLookupsAtom = subAtom(rootAtom, "service_type_active_lookups");
	const typeRendererCacheAtom = subAtom(rootAtom, "type_renderer_cache");
	const typeRendererActiveLookupsAtom = subAtom(rootAtom, "type_renderer_active_lookups");
	const reloadInboxAtom = subAtom(rootAtom, "reload_inbox");

	// In-memory (not session-backed) cache of compiled renderer
	// modules, keyed by renderer entity short_id. The workstation
	// guarantees entity short_ids are stable for the server's
	// lifetime, so caching across the dispatcher's lifetime is safe.
	// Page reload empties this; reattach to an existing session
	// repopulates on first reference (the per-entry state machine
	// re-kicks `ensureCompiled` from `loading-renderer-bytes` when
	// it sees the map's empty for its renderer ref).
	const compiledModulesAtom: PrimitiveAtom<Map<string, CompiledModuleStatus>> = atom(
		new Map<string, CompiledModuleStatus>(),
	);
	// Dedup map for concurrent in-flight compile loads. Removed
	// after the load settles (regardless of outcome). Tests that
	// want to assert dedup can read its presence on the
	// `inFlightLoadCount` counter we wire via the load wrapper
	// below.
	const compiledLoadInFlight = new Map<string, Promise<void>>();

	function ensureCompiled(rendererRef: EntityRefStub): void {
		const shortId = rendererRef.shortId;
		// Already in flight, or settled (ready / errored)? Don't
		// re-kick. Errored is sticky — the dispatcher doesn't retry
		// on its own. Consumers wanting retry must mint a fresh
		// dispatcher (typical pattern: detach + re-construct).
		if (compiledLoadInFlight.has(shortId)) return;
		const existing = session.store.get(compiledModulesAtom).get(shortId);
		if (existing !== undefined) return;
		// Mark loading synchronously so the calling effect can
		// subscribe to the change on its next pass.
		session.store.set(compiledModulesAtom, (prev) => {
			const next = new Map(prev);
			next.set(shortId, { status: "loading" });
			return next;
		});
		const promise = doLoadAndCompile(rendererRef);
		compiledLoadInFlight.set(shortId, promise);
	}

	async function doLoadAndCompile(rendererRef: EntityRefStub): Promise<void> {
		const shortId = rendererRef.shortId;
		try {
			const data = await loadEntityData(rendererRef);
			if (data === null || typeof data !== "object") {
				throw new Error("Renderer entity load returned a non-object value");
			}
			const moduleBytes = (data as { module?: unknown }).module;
			if (!(moduleBytes instanceof Uint8Array)) {
				throw new Error(
					"Renderer entity is missing a `module: Uint8Array` field (or the field is the wrong type)",
				);
			}
			const compiled = await compileRendererBytes(moduleBytes);
			session.store.set(compiledModulesAtom, (prev) => {
				const next = new Map(prev);
				next.set(shortId, { status: "ready", module: compiled });
				return next;
			});
		} catch (err) {
			// The stored state carries only the message (it renders in the
			// error boundary); surface the full error here so the original
			// stack isn't lost.
			console.error("[dynamic-dispatch] renderer load/compile failed for", shortId, err);
			const message = err instanceof Error ? err.message : String(err);
			session.store.set(compiledModulesAtom, (prev) => {
				const next = new Map(prev);
				next.set(shortId, { status: "errored", message });
				return next;
			});
		} finally {
			compiledLoadInFlight.delete(shortId);
		}
	}

	const dispatcherCtx: DispatcherEffectContext = {
		dispatchServiceRef,
		serviceTypeCacheAtom,
		serviceTypeActiveLookupsAtom,
		typeRendererCacheAtom,
		typeRendererActiveLookupsAtom,
		compiledModulesAtom,
		ensureCompiled,
	};

	// --- Reload helpers (shared by reception / break / manual paths) -----
	//
	// `wipeRootCaches` empties the four session-backed type caches and
	// resets every entry's state machine to `init`, so the next pass
	// re-resolves from scratch. It deliberately does NOT touch
	// `reload_inbox` (each caller decides whether to slice events /
	// resubscribe) nor the in-memory compiled-module cache (renderer
	// entities are content-addressed, so a changed renderer is a new
	// short_id → cache miss → recompile; an unchanged one is reused).
	function wipeRootCaches(prev: DynamicDispatcherUserState): DynamicDispatcherUserState {
		const resetEntries: DynamicDispatcherUserState["entries"] = {};
		for (const [keyUuid, perKey] of Object.entries(prev.entries)) {
			const newPerKey: Record<string, EntryUserStateSlot> = {};
			for (const [shortId, slot] of Object.entries(perKey)) {
				newPerKey[shortId] = {
					...slot,
					dispatcher_state: { phase: "init" } as unknown as StateValue,
				};
			}
			resetEntries[keyUuid] = newPerKey;
		}
		return {
			...prev,
			entries: resetEntries,
			service_type_cache: {},
			service_type_active_lookups: {},
			type_renderer_cache: {},
			type_renderer_active_lookups: {},
		};
	}

	// Flip every mounted entry's staleness atom so the wrap dims the
	// visible component immediately, ahead of the userState-batched wipe.
	function flipAllStale(setter: Setter): void {
		for (const stale of staleAtoms.values()) setter(stale, true);
	}

	// Edge tracker for reload-on-break: only react on a transition INTO
	// `broken`, so an unreachable source doesn't spin resubscribe
	// attempts. Reset whenever the connection is observed not-broken.
	let lastReloadConnState: "idle" | "connected" | "broken" = "idle";

	// --- Reload subscription + reception effect --------------------------
	//
	// On first connect (and on reattach after a workstation restart):
	// allocate a callback inbox + dispatch a callback-target
	// `subscribe_to(dispatchServiceRef)` invoke on its handle. On every event delivered to the
	// inbox: flip every cached entry's `staleAtom` to `true`, wipe the
	// session-backed renderer/type caches, reset every cached entry's
	// `dispatcher_state` to `init`, and consume the events via
	// `inbox.sliced(...)`. The state machine then re-resolves each
	// entry; terminal phases will clear `staleAtom` back to `false`
	// when they commit a new component.
	//
	// The effect also watches the inbox's `connectionState`: if the
	// durable subscription breaks (ws drop / source grant revoked,
	// surfaced as `connectionState: "broken"` while `state` stays
	// "open"), it wipes the caches and resubscribes on the existing
	// handle — the recovery the reception path can't see because no
	// events arrive and the slot never goes "closed".
	//
	// Mounted eagerly via `session.store.sub` at dispatcher
	// construction so the subscribe round-trip happens without
	// waiting on a `<RenderDispatch>` to mount. Unmounted on
	// `detach()`.
	const reloadEffect = atomEffect((get, set) => {
		if (get(closedAtom)) return;
		const lifecycle = get(session.lifecycle);
		if (lifecycle !== "connected") return;

		const inbox = get(reloadInboxAtom);
		const inboxState = inbox === null ? null : get(inbox.state);

		// (Re-)subscribe path: no inbox yet, or stale one inherited
		// from a previous workstation lifetime that dropped the slot.
		// Allocate + subscribe ride the same tick (and the same wire
		// mutation): the subscribe is a callback-target invoke on the
		// fresh inbox's own handle, with the dispatcher service as
		// the event source. The workstation owns the subscription's
		// lifecycle from here — closing the inbox unsubscribes.
		if (inbox === null || inboxState === "closed") {
			if (inbox !== null && inboxState === "closed") {
				console.warn(
					"DynamicDispatcher: reload_inbox is closed on reattach. " +
						"Holding the callback slot in userState should keep it " +
						"open — this usually means the workstation server has " +
						"restarted since the session was created. Re-allocating " +
						"the callback and re-subscribing.",
				);
			}
			set(rootAtom, (prev, updateCtx) => {
				const newInbox = updateCtx.allocateCallback<EntityRefStub>();
				const newInvoke = updateCtx.allocateInvoke<null>(newInbox.handle, "subscribe_to", [
					dispatchServiceRef,
				]);
				return {
					...prev,
					reload_inbox: newInbox,
					reload_subscribe_result: newInvoke,
				};
			});
			lastReloadConnState = "idle";
			return;
		}

		// Reload-on-break path: slot still open, but the durable
		// subscription's connection has broken. Wipe + resubscribe on
		// the existing handle, once per `broken` edge (the guard stops
		// an unreachable source from spinning).
		const connState = get(inbox.connectionState);
		if (connState === "broken") {
			if (lastReloadConnState !== "broken") {
				lastReloadConnState = "broken";
				flipAllStale(set);
				set(rootAtom, (prev, updateCtx) => {
					const handle = prev.reload_inbox?.handle;
					return {
						...wipeRootCaches(prev),
						reload_subscribe_result:
							handle === undefined
								? prev.reload_subscribe_result
								: updateCtx.allocateInvoke<null>(handle, "subscribe_to", [dispatchServiceRef]),
					};
				});
			}
			return;
		}
		lastReloadConnState = connState;

		// Reception path: inbox is alive — react to delivered events.
		const events = get(inbox.events);
		if (events.length === 0) return;

		// (a) Sync in-memory pass: flip every cached entry's
		// staleness atom so the wrap immediately dims the visible
		// component, ahead of the userState-batched changes below.
		flipAllStale(set);

		// (b) UserState batch: wipe the per-type caches, reset every
		// cached entry's dispatcher_state to `init`, and consume the
		// events via `inbox.sliced(events.length)`. Coalesces into
		// one flush; the state machine then re-resolves every entry.
		const consumed = events.length;
		set(rootAtom, (prev) => ({
			...wipeRootCaches(prev),
			reload_inbox: prev.reload_inbox?.sliced(consumed) ?? prev.reload_inbox,
		}));
	});

	// Subscribing to the effect atom keeps it mounted. The unsub
	// handle is stored so `detach()` can tear it down.
	const reloadEffectUnsub = session.store.sub(reloadEffect, () => {});

	function getEntry(key: AnyDispatchKey, stub: EntityRefStub | ServiceRefStub): AnyDispatchEntry {
		if (session.store.get(closedAtom)) throw new DispatcherClosedError();

		const id = `${key.uuid}::${stub.shortId}`;
		const cached = cache.get(id);
		if (cached !== undefined) return cached;

		const stubKind: "entity" | "service" =
			stub instanceof EntityRefStub
				? "entity"
				: stub instanceof ServiceRefStub
					? "service"
					: (() => {
							throw new Error(
								"DynamicDispatcher.getEntry: stub is neither EntityRefStub nor ServiceRefStub. Pass a ref-stub from a connector's entityRef() / serviceRef() / interning path.",
							);
						})();

		// Project userState through entries[keyUuid][shortId] to the
		// per-entry slot. Mirrors the static dispatcher.
		const entriesAtom = subAtom(rootAtom, "entries");
		const keyEntriesNullable = subAtom(entriesAtom, key.uuid);
		const keyEntriesAtom = atomWithDefault(
			keyEntriesNullable,
			{} as Record<string, EntryUserStateSlot>,
		);
		const entrySlotNullable = subAtom(keyEntriesAtom, stub.shortId);
		const initialState: DynamicEntityDispatchState | DynamicServiceDispatchState = {
			phase: "init",
		};
		const defaultSlot: EntryUserStateSlot = {
			dispatcher_state: initialState as unknown as StateValue,
			component_shared_state: null,
		};
		const entrySlotAtom = atomWithDefault(entrySlotNullable, defaultSlot);
		const sharedComponentStateAtom = subAtom(entrySlotAtom, "component_shared_state");

		// Resolve the key's fallbacks against any matching override.
		// Treated uniformly as `AnyKeyFallbacks<object>` at the
		// erased boundary; each effect factory re-narrows.
		const baseFallbacks = (key as unknown as { fallbacks: AnyKeyFallbacks<object> }).fallbacks;
		const fallbacks = resolveKeyFallbacks<AnyKeyFallbacks<object>>(
			baseFallbacks,
			overrideMap,
			key.uuid,
		);

		// Mint the per-entry staleness atom + wrap helper. Both stay
		// inside this dispatcher's closure — the atom is exposed
		// via `staleAtoms.get(id)` to the reload effect (chunk 4)
		// but never reaches the `DispatchEntry` interface; the wrap
		// helper is threaded into the effect-factory builders so
		// every `set(componentAtom, ...)` site can route through
		// it.
		const staleAtom: PrimitiveAtom<boolean> = atom(false);
		staleAtoms.set(id, staleAtom);
		const wrap = makeEntryWrapper(staleAtom);

		let effectFactory: DispatchEffectFactory<EntityRefStub | ServiceRefStub, object, unknown>;
		if (stubKind === "entity") {
			const entityStateAtom = subAtom(
				entrySlotAtom,
				"dispatcher_state",
			) as unknown as FxtrSessionAtom<DynamicEntityDispatchState>;
			effectFactory = buildEntityEffectFactory(
				entityStateAtom,
				key.uuid,
				dispatcherCtx,
				wrap,
				staleAtom,
			) as unknown as DispatchEffectFactory<EntityRefStub | ServiceRefStub, object, unknown>;
		} else {
			const serviceStateAtom = subAtom(
				entrySlotAtom,
				"dispatcher_state",
			) as unknown as FxtrSessionAtom<DynamicServiceDispatchState>;
			effectFactory = buildServiceEffectFactory(
				serviceStateAtom,
				key.uuid,
				dispatcherCtx,
				wrap,
				staleAtom,
			) as unknown as DispatchEffectFactory<EntityRefStub | ServiceRefStub, object, unknown>;
		}

		const stateAtomErased = subAtom(
			entrySlotAtom,
			"dispatcher_state",
		) as unknown as FxtrSessionAtom<unknown>;

		const entry = createBaseEntry({
			id,
			stub,
			stateAtom: stateAtomErased,
			sharedComponentStateAtom,
			// Wrap the initial component too so the wrap div is
			// already in the tree when the entry first mounts; the
			// state machine's later writes just swap the inner
			// component without remounting the wrap.
			initialComponent: wrap(
				fallbacks.Loading as DispatchedComponent<EntityRefStub | ServiceRefStub, object>,
			),
			initialProgressMessage: "",
			effectFactory,
			ErrorFallback: fallbacks.Error as ErrorFallbackComponent<
				EntityRefStub | ServiceRefStub,
				object
			>,
			// biome-ignore lint/suspicious/noExplicitAny: StubFallbacks discriminates by Stub but the dispatcher's getEntry runs at the erased union; the effect factories re-narrow per stubKind branch
			fallbacks: fallbacks as any,
		});

		cache.set(id, entry);
		session.store.set(mountedIdsAtom, (prev) => [...prev, id]);
		return entry;
	}

	let detachPromise: Promise<void> | null = null;
	function detach(): Promise<void> {
		if (detachPromise !== null) return detachPromise;
		detachPromise = (async () => {
			session.store.set(closedAtom, true);
			reloadEffectUnsub();
			cache.clear();
			staleAtoms.clear();
			session.store.set(mountedIdsAtom, []);
			await session.detach();
		})();
		return detachPromise;
	}

	// Manual "reload everything" — the same wipe the reception / break
	// paths run, exposed only through this dispatcher's debug view (NOT
	// on the `Dispatcher` interface). If the reload subscription is
	// currently broken, also resubscribe on the existing handle
	// (best-effort: if the auto break-path already recovered it, the
	// `broken` guard below skips the resubscribe).
	function manualReloadEverything(): void {
		if (session.store.get(closedAtom)) return;
		flipAllStale(session.store.set);
		const inbox = session.store.get(reloadInboxAtom);
		const broken = inbox !== null && session.store.get(inbox.connectionState) === "broken";
		session.store.set(rootAtom, (prev, updateCtx) => {
			const handle = prev.reload_inbox?.handle;
			if (broken && handle !== undefined) {
				return {
					...wipeRootCaches(prev),
					reload_subscribe_result: updateCtx.allocateInvoke<null>(handle, "subscribe_to", [
						dispatchServiceRef,
					]),
				};
			}
			return wipeRootCaches(prev);
		});
	}

	function debugView(): ReactElement {
		// Wrap in a jotai `<Provider store={session.store}>` so the
		// debug view's `useAtomValue` calls read from the same
		// store the dispatcher writes to.
		return createElement(
			JotaiProvider,
			{ store: session.store },
			createElement(DynamicDispatcherDebugView, {
				status,
				sessionId: session.sessionId,
				sessionLifecycle: session.lifecycle,
				mountedIds: mountedIdsAtom,
				getEntryById: (id: string) => cache.get(id),
				getStaleAtomById: (id: string) => staleAtoms.get(id),
				serviceTypeCache: serviceTypeCacheAtom,
				serviceTypeActiveLookups: serviceTypeActiveLookupsAtom,
				typeRendererCache: typeRendererCacheAtom,
				typeRendererActiveLookups: typeRendererActiveLookupsAtom,
				compiledModules: compiledModulesAtom,
				reloadInbox: reloadInboxAtom,
				onReloadEverything: manualReloadEverything,
			}),
		);
	}

	return { status, session, getEntry, detach, debugView };
}

// --- State-machine effect-factory builders ---------------------------

/**
 * Atoms + refs the per-entry state machines reach into to drive
 * their cross-entry caches. Built once at dispatcher construction
 * time and shared across every entry's effect factory.
 */
interface DispatcherEffectContext {
	readonly dispatchServiceRef: ServiceRefStub<unknown>;
	readonly serviceTypeCacheAtom: FxtrSessionAtom<Record<string, EntityRefStub>>;
	readonly serviceTypeActiveLookupsAtom: FxtrSessionAtom<
		Record<string, OpResultStub<EntityRefStub>>
	>;
	readonly typeRendererCacheAtom: FxtrSessionAtom<
		Record<string, Record<string, RendererResolution | null>>
	>;
	readonly typeRendererActiveLookupsAtom: FxtrSessionAtom<
		Record<string, Record<string, OpResultStub<RendererResolution | null>>>
	>;
	/**
	 * In-memory map from renderer short_id to compile status.
	 * State machines `get` it to drive `loading-renderer-bytes`.
	 */
	readonly compiledModulesAtom: PrimitiveAtom<Map<string, CompiledModuleStatus>>;
	/**
	 * Kicks an async load + compile of `rendererRef`'s bytes (if
	 * not already in-flight or settled). Synchronously writes
	 * `{status: "loading"}` to `compiledModulesAtom` so the
	 * calling effect's next pass observes the change.
	 */
	readonly ensureCompiled: (rendererRef: EntityRefStub) => void;
}

/**
 * Default bytes → module pipeline: wrap the bytes in a
 * `application/javascript` Blob, mint a blob URL, dynamic-import
 * it, and return `module.default`. Browser-only (jsdom has no
 * native ESM loader for blob URLs); tests inject a mock through
 * `compileRendererBytes` instead.
 *
 * Production consumers must have a `<script type="importmap">`
 * mapping bare specifiers to shim URLs in the document before
 * the first call here, or the renderer's `import "react"` (etc.)
 * will fail with an unresolved-specifier error. See
 * `@fxtr-ui/react-dispatch-dynamic-shim`'s `installFxtrExternals`.
 */
async function defaultCompileRendererBytes(bytes: Uint8Array): Promise<CompiledRendererModule> {
	// Cast through `BlobPart`: lib.dom.d.ts in newer TS versions
	// narrows `Uint8Array`'s `ArrayBufferLike` parameter in a way
	// that no longer matches `BlobPart`'s structural shape — the
	// constructor still accepts it at runtime, but the type
	// position requires the wider alias.
	const blob = new Blob([bytes as unknown as BlobPart], { type: "application/javascript" });
	const url = URL.createObjectURL(blob);
	try {
		const mod = (await import(/* @vite-ignore */ url)) as {
			default?: CompiledRendererModule;
		};
		if (mod.default === undefined) {
			throw new Error(
				"Compiled renderer module has no default export; expected `export default { [subtype_key]: { [key_uuid]: Component } }`.",
			);
		}
		return mod.default;
	} finally {
		URL.revokeObjectURL(url);
	}
}

/**
 * Synthetic component used to surface terminal `errored` phases.
 * Reads the state atom via `useAtomValue`; throws
 * `Error(state.message)` during render so the per-entry error
 * boundary catches it and renders the configured `Error` fallback.
 *
 * Identity-stable per state-atom pair so the terminal-state
 * re-sync's `componentAtom !== ErrorThrower` check is reliable
 * across effect runs.
 */
function buildErrorThrower<Stub extends EntityRefStub | ServiceRefStub, Props extends object>(
	stateAtom: FxtrSessionAtom<DynamicEntityDispatchState | DynamicServiceDispatchState>,
): DispatchedComponent<Stub, Props> {
	const ErrorThrower: DispatchedComponent<Stub, Props> = () => {
		const state = useAtomValue(stateAtom);
		if (state.phase === "errored") {
			throw new Error(state.message);
		}
		throw new Error(
			`DynamicDispatcher.ErrorThrower rendered with state.phase="${state.phase}" — should only be mounted while state.phase === "errored". This is a dispatcher invariant violation.`,
		);
	};
	ErrorThrower.displayName = "DynamicDispatchErrorThrower";
	return ErrorThrower;
}

/**
 * Phase 2: drive the `resolving-renderer` arm forward by reading
 * the shared `type_renderer_*` caches and (cold path) allocating a
 * `resolve(typeRef)` invoke against the dispatcher service. Used
 * by both entity- and service-side effect factories — the only
 * difference is the surrounding state-machine type, which is
 * threaded through the generic `S`.
 *
 * The function performs its writes (state-atom transition, cache
 * promotion, active_lookups insert/delete, fresh invoke allocation)
 * and returns; the effect's normal subscribe-and-re-run dance
 * propagates them. Cold path explicitly does *not* change the
 * entry's `stateAtom` — only `type_renderer_active_lookups` is
 * written; the next pass (re-triggered by that write) finds the
 * in-flight slot on the active-lookup branch and reads its result.
 */
function driveResolvingRenderer<S extends DynamicEntityDispatchState | DynamicServiceDispatchState>(
	get: <T>(a: Atom<T>) => T,
	set: import("jotai").Setter,
	stateAtom: FxtrSessionAtom<S>,
	typeRef: EntityRefStub,
	keyUuid: string,
	ctx: DispatcherEffectContext,
): void {
	// Cache hit: skip the wire round-trip entirely and go straight
	// to the bytes-load phase (or to `no-match` if the cached
	// answer is the dispatcher's null result). Cache is nested by
	// (typeShortId, keyUuid) — different dispatch keys for the
	// same type may resolve to different renderers.
	const cache = get(ctx.typeRendererCacheAtom);
	const cached = cache[typeRef.shortId]?.[keyUuid];
	if (cached !== undefined) {
		if (cached === null) {
			set(stateAtom, { phase: "no-match" } as unknown as S);
			return;
		}
		set(stateAtom, {
			phase: "loading-renderer-bytes",
			subtype_key: cached.subtype_key,
			renderer: cached.renderer,
		} as unknown as S);
		return;
	}
	// Active-lookup hit: a sibling entry is already invoking
	// `resolve` for this (type, key) pair. Read that invoke's
	// result; if it fills here, promote into cache + drop from
	// active_lookups + transition this entry; if it errors,
	// transition to errored + drop from active_lookups.
	const inFlight = get(ctx.typeRendererActiveLookupsAtom)[typeRef.shortId]?.[keyUuid];
	if (inFlight !== undefined) {
		const result = get(inFlight.result);
		if (result.status === "pending") return;
		if (result.status === "errored") {
			set(stateAtom, { phase: "errored", message: result.message } as unknown as S);
			set(ctx.typeRendererActiveLookupsAtom, (prev) =>
				removeNestedKey(prev, typeRef.shortId, keyUuid),
			);
			return;
		}
		// Filled — validate shape, promote into cache, drop from
		// active_lookups, advance this entry. Several entries
		// reading the same in-flight may race here; each one's
		// writes are idempotent (cache write is the same value,
		// delete is idempotent) so the redundant writes are
		// harmless — at worst they emit no-op patches.
		if (!isResolveResultShape(result.value)) {
			set(stateAtom, {
				phase: "errored",
				message:
					"Dispatcher service `resolve` returned a value that doesn't match `{subtype_key, renderer} | null`.",
			} as unknown as S);
			set(ctx.typeRendererActiveLookupsAtom, (prev) =>
				removeNestedKey(prev, typeRef.shortId, keyUuid),
			);
			return;
		}
		const resolution = result.value;
		set(ctx.typeRendererCacheAtom, (prev) =>
			setNestedKey(prev, typeRef.shortId, keyUuid, resolution),
		);
		set(ctx.typeRendererActiveLookupsAtom, (prev) =>
			removeNestedKey(prev, typeRef.shortId, keyUuid),
		);
		if (resolution === null) {
			set(stateAtom, { phase: "no-match" } as unknown as S);
			return;
		}
		set(stateAtom, {
			phase: "loading-renderer-bytes",
			subtype_key: resolution.subtype_key,
			renderer: resolution.renderer,
		} as unknown as S);
		return;
	}
	// Cold path: allocate a fresh invoke against the dispatcher
	// service and stash the result stub in active_lookups. The
	// per-entry state-atom doesn't change — the next pass (driven
	// by the active_lookups write) finds the in-flight slot and
	// reads its result. The function-form sub-atom write threads
	// the FxtrSessionUpdateCtx through so `ctx.allocateInvoke` is
	// callable inside; the returned stub is referenced inside the
	// returned record, satisfying the
	// "slot must be referenced from userState by end-of-flush"
	// contract. The fresh invoke is keyed by (typeShortId, keyUuid)
	// in the same flush.
	set(ctx.typeRendererActiveLookupsAtom, (prev, updateCtx) => {
		const invoke = updateCtx.allocateInvoke(ctx.dispatchServiceRef, "resolve", [
			typeRef,
			keyUuid,
		]) as OpResultStub<RendererResolution | null>;
		return setNestedKey(prev, typeRef.shortId, keyUuid, invoke);
	});
}

/**
 * Functional update helper for the nested `Record<typeShortId,
 * Record<keyUuid, V>>` shape used by the renderer caches +
 * active_lookups. Returns a new outer record with `outer[key]`
 * replaced by a new inner record that has `inner[innerKey] = value`.
 * Preserves every other entry — both top-level (other types) and
 * within the affected inner record (other dispatch keys for the
 * same type).
 */
function setNestedKey<V>(
	prev: Record<string, Record<string, V>>,
	key: string,
	innerKey: string,
	value: V,
): Record<string, Record<string, V>> {
	const innerPrev = prev[key] ?? {};
	return { ...prev, [key]: { ...innerPrev, [innerKey]: value } };
}

/**
 * Inverse of :func:`setNestedKey`. Drops `outer[key][innerKey]`. If
 * `outer[key]` becomes empty after the inner removal, the outer
 * entry itself is dropped — keeps the record tidy under the steady
 * state where a single (type, key) was the only entry.
 */
function removeNestedKey<V>(
	prev: Record<string, Record<string, V>>,
	key: string,
	innerKey: string,
): Record<string, Record<string, V>> {
	const innerPrev = prev[key];
	if (innerPrev === undefined) return prev;
	if (!(innerKey in innerPrev)) return prev;
	const innerNext = { ...innerPrev };
	delete innerNext[innerKey];
	const next = { ...prev };
	if (Object.keys(innerNext).length === 0) {
		delete next[key];
	} else {
		next[key] = innerNext;
	}
	return next;
}

/**
 * Phase 3: drive the `loading-renderer-bytes` arm forward by
 * reading the in-memory `compiledModulesAtom` for the renderer
 * entity's short_id. Same shared-helper pattern as
 * `driveResolvingRenderer` — used by both entity- and service-
 * side effect factories.
 *
 * Path summary:
 *   - Map entry absent → kick `ensureCompiled` (which writes
 *     `{status: "loading"}` synchronously, then runs the load +
 *     compile asynchronously). The next effect pass observes the
 *     `loading` status and waits.
 *   - `{status: "loading"}` → wait.
 *   - `{status: "errored", message}` → transition entry to errored.
 *   - `{status: "ready", module}` → look up
 *     `module[subtype_key][key.uuid]`:
 *       - hit → publish the resolved component.
 *       - subtype miss / key.uuid miss → `console.warn` (server
 *         and client disagree on what the module should export)
 *         and publish `NoMatchFallback`.
 *
 * The terminal hit / miss render is idempotent: each effect pass
 * re-runs the lookup against the current compiledModulesAtom value
 * and writes through to `componentAtom` only on change. Reattach
 * works naturally — userState carries `(subtype_key, renderer)`,
 * the in-memory map is fresh, the first pass kicks
 * `ensureCompiled`, eventually publishes.
 */
function driveLoadingRendererBytes<
	S extends DynamicEntityDispatchState | DynamicServiceDispatchState,
	Stub extends EntityRefStub | ServiceRefStub,
>(
	get: <T>(a: Atom<T>) => T,
	set: import("jotai").Setter,
	stateAtom: FxtrSessionAtom<S>,
	componentAtom: import("./registry_types").ComponentAtom<Stub, object>,
	keyUuid: string,
	subtypeKey: string,
	renderer: EntityRefStub,
	noMatchFallback: DispatchedComponent<Stub, object>,
	ctx: DispatcherEffectContext,
	wrap: EntryWrapper,
	staleAtom: PrimitiveAtom<boolean>,
): void {
	const map = get(ctx.compiledModulesAtom);
	const status = map.get(renderer.shortId);
	if (status === undefined) {
		ctx.ensureCompiled(renderer);
		return;
	}
	if (status.status === "loading") return;
	if (status.status === "errored") {
		set(stateAtom, { phase: "errored", message: status.message } as unknown as S);
		return;
	}
	// status.status === "ready" — perform the 2-level lookup.
	const subtypeMap = status.module[subtypeKey];
	if (subtypeMap === undefined) {
		console.warn(
			`DynamicDispatcher: renderer module ${renderer.shortId} has no entry for subtype_key "${subtypeKey}"; rendering NoMatch. The backend's resolve() promised a subtype that the compiled module doesn't carry.`,
		);
		const wrapped = wrap(noMatchFallback);
		if (get(componentAtom) !== wrapped) {
			set(componentAtom, wrapped);
		}
		set(staleAtom, false);
		return;
	}
	const component = subtypeMap[keyUuid];
	if (component === undefined) {
		console.warn(
			`DynamicDispatcher: renderer module ${renderer.shortId} (subtype_key "${subtypeKey}") has no entry for dispatch-key uuid "${keyUuid}"; rendering NoMatch. The backend's compiled module didn't include a renderer for this slot.`,
		);
		const wrapped = wrap(noMatchFallback);
		if (get(componentAtom) !== wrapped) {
			set(componentAtom, wrapped);
		}
		set(staleAtom, false);
		return;
	}
	// Cast: the module's components are typed against the union
	// `EntityRefStub | ServiceRefStub` but this entry's
	// `componentAtom` is narrowed to `Stub` (entity or service).
	// The component IS expected to handle whichever kind this
	// entry's stub is — the cast asserts that the renderer module
	// matches the dispatch key's `Allowed`.
	const narrowed = component as DispatchedComponent<Stub, object>;
	const wrapped = wrap(narrowed);
	if (get(componentAtom) !== wrapped) {
		set(componentAtom, wrapped);
	}
	set(staleAtom, false);
}

function buildEntityEffectFactory(
	stateAtom: FxtrSessionAtom<DynamicEntityDispatchState>,
	keyUuid: string,
	ctx: DispatcherEffectContext,
	wrap: EntryWrapper,
	staleAtom: PrimitiveAtom<boolean>,
): DispatchEffectFactory<EntityRefStub, object, DynamicEntityDispatchState> {
	const ErrorThrower = buildErrorThrower<EntityRefStub, object>(
		stateAtom as FxtrSessionAtom<DynamicEntityDispatchState | DynamicServiceDispatchState>,
	);
	return ({ stub, componentAtom, progressMessageAtom, fallbacks }) => {
		const entityFallbacks = fallbacks as EntityKeyFallbacks<object>;
		return (get, set) => {
			const state = get(stateAtom);
			switch (state.phase) {
				case "init": {
					if (isBuiltinEntityShortId(stub.shortId)) {
						set(stateAtom, { phase: "resolved-builtin" });
						return;
					}
					set(stateAtom, (_prev, updateCtx) => ({
						phase: "loading-entity",
						load: updateCtx.allocateLoad(stub),
					}));
					set(progressMessageAtom, "loading entity…");
					return;
				}
				case "loading-entity": {
					const result = get(state.load.result);
					if (result.status === "pending") return;
					if (result.status === "errored") {
						set(stateAtom, { phase: "errored", message: result.message });
						return;
					}
					const value = result.value;
					const typeField =
						value !== null && typeof value === "object" && !Array.isArray(value)
							? (value as Record<string, unknown>)["$fxtr.type"]
							: undefined;
					if (!(typeField instanceof EntityRefStub)) {
						set(stateAtom, {
							phase: "errored",
							message:
								"Entity loaded value has no `$fxtr.type` entity-ref field — dynamic dispatch cannot resolve a renderer without one.",
						});
						return;
					}
					set(stateAtom, {
						phase: "resolving-renderer",
						typeRef: typeField,
					});
					set(progressMessageAtom, "resolving renderer…");
					return;
				}
				case "resolving-renderer": {
					driveResolvingRenderer(get, set, stateAtom, state.typeRef, keyUuid, ctx);
					return;
				}
				case "loading-renderer-bytes": {
					driveLoadingRendererBytes(
						get,
						set,
						stateAtom,
						componentAtom,
						keyUuid,
						state.subtype_key,
						state.renderer,
						entityFallbacks.NoMatch,
						ctx,
						wrap,
						staleAtom,
					);
					return;
				}
				case "resolved-builtin": {
					const Component = wrap(entityFallbacks.Builtin ?? entityFallbacks.NoMatch);
					if (get(componentAtom) !== Component) {
						set(componentAtom, Component);
					}
					set(staleAtom, false);
					return;
				}
				case "no-match": {
					const Component = wrap(entityFallbacks.NoMatch);
					if (get(componentAtom) !== Component) {
						set(componentAtom, Component);
					}
					set(staleAtom, false);
					return;
				}
				case "errored": {
					const Component = wrap(ErrorThrower);
					if (get(componentAtom) !== Component) {
						set(componentAtom, Component);
					}
					set(staleAtom, false);
					return;
				}
			}
		};
	};
}

function buildServiceEffectFactory(
	stateAtom: FxtrSessionAtom<DynamicServiceDispatchState>,
	keyUuid: string,
	ctx: DispatcherEffectContext,
	wrap: EntryWrapper,
	staleAtom: PrimitiveAtom<boolean>,
): DispatchEffectFactory<ServiceRefStub, object, DynamicServiceDispatchState> {
	const ErrorThrower = buildErrorThrower<ServiceRefStub, object>(
		stateAtom as FxtrSessionAtom<DynamicEntityDispatchState | DynamicServiceDispatchState>,
	);
	return ({ stub, componentAtom, progressMessageAtom, fallbacks }) => {
		const serviceFallbacks = fallbacks as ServiceKeyFallbacks<object>;
		return (get, set) => {
			const state = get(stateAtom);
			switch (state.phase) {
				case "init": {
					const cachedTypeRef = get(ctx.serviceTypeCacheAtom)[stub.shortId];
					if (cachedTypeRef !== undefined) {
						set(stateAtom, {
							phase: "resolving-renderer",
							typeRef: cachedTypeRef,
						});
						set(progressMessageAtom, "resolving renderer…");
						return;
					}
					const inFlight = get(ctx.serviceTypeActiveLookupsAtom)[stub.shortId];
					if (inFlight !== undefined) {
						set(stateAtom, { phase: "loading-service-type", typeInvoke: inFlight });
						set(progressMessageAtom, "resolving service type…");
						return;
					}
					// Cold path: allocate a fresh `$fxtr.type` invoke,
					// stash the result stub in active_lookups in the
					// same flush. Captured-from-inside-the-updater so
					// we can also write it into the dispatcher-level
					// active_lookups atom right after. Both writes are
					// sync inside this effect run so they coalesce
					// into one session-state flush, which matters:
					// the slot stub must be referenced from userState
					// by end-of-flush to survive past the pending-slot
					// wipe.
					let allocated: OpResultStub<EntityRefStub> | undefined;
					set(stateAtom, (_prev, updateCtx) => {
						const invoke = updateCtx.allocateInvoke(
							stub,
							"$fxtr.type",
							[],
						) as unknown as OpResultStub<EntityRefStub>;
						allocated = invoke;
						return { phase: "loading-service-type", typeInvoke: invoke };
					});
					if (allocated === undefined) {
						throw new Error(
							"unreachable: function-form FxtrSessionAtom updater should run synchronously",
						);
					}
					const captured = allocated;
					set(ctx.serviceTypeActiveLookupsAtom, (prev) => ({
						...prev,
						[stub.shortId]: captured,
					}));
					set(progressMessageAtom, "resolving service type…");
					return;
				}
				case "loading-service-type": {
					const result = get(state.typeInvoke.result);
					if (result.status === "pending") return;
					if (result.status === "errored") {
						set(stateAtom, { phase: "errored", message: result.message });
						set(ctx.serviceTypeActiveLookupsAtom, (prev) => {
							const next = { ...prev };
							delete next[stub.shortId];
							return next;
						});
						return;
					}
					const typeRef = result.value;
					if (!(typeRef instanceof EntityRefStub)) {
						set(stateAtom, {
							phase: "errored",
							message:
								"Service `$fxtr.type` invoke returned a non-entity value; dynamic dispatch expects an EntityRefStub.",
						});
						set(ctx.serviceTypeActiveLookupsAtom, (prev) => {
							const next = { ...prev };
							delete next[stub.shortId];
							return next;
						});
						return;
					}
					set(ctx.serviceTypeCacheAtom, (prev) => ({
						...prev,
						[stub.shortId]: typeRef,
					}));
					set(ctx.serviceTypeActiveLookupsAtom, (prev) => {
						const next = { ...prev };
						delete next[stub.shortId];
						return next;
					});
					set(stateAtom, { phase: "resolving-renderer", typeRef });
					set(progressMessageAtom, "resolving renderer…");
					return;
				}
				case "resolving-renderer": {
					driveResolvingRenderer(get, set, stateAtom, state.typeRef, keyUuid, ctx);
					return;
				}
				case "loading-renderer-bytes": {
					driveLoadingRendererBytes(
						get,
						set,
						stateAtom,
						componentAtom,
						keyUuid,
						state.subtype_key,
						state.renderer,
						serviceFallbacks.NoMatch,
						ctx,
						wrap,
						staleAtom,
					);
					return;
				}
				case "no-match": {
					const Component = wrap(serviceFallbacks.NoMatch);
					if (get(componentAtom) !== Component) {
						set(componentAtom, Component);
					}
					set(staleAtom, false);
					return;
				}
				case "errored": {
					const Component = wrap(ErrorThrower);
					if (get(componentAtom) !== Component) {
						set(componentAtom, Component);
					}
					set(staleAtom, false);
					return;
				}
			}
		};
	};
}
