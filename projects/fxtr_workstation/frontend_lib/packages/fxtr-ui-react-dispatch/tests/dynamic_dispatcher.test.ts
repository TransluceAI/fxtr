/**
 * State-machine tests for `createDynamicDispatcher`. Drives the
 * production dispatcher (not a separately-extracted factory) so
 * the state machine, base-entry wrapping, and userState
 * projection all participate in the test.
 *
 * Coverage:
 *
 *   ENTITY phase 1: init → loading-entity → resolving-renderer.
 *     Plus builtin shortcut + load-failure + missing-$fxtr.type
 *     paths.
 *
 *   SERVICE phase 1: init → loading-service-type → resolving-renderer.
 *     Plus service_type cache-hit shortcut, shared-in-flight dedup
 *     across two entries for the same service, invoke failure,
 *     non-entity-result error.
 *
 *   PHASE 2 (renderer resolution): resolving-renderer →
 *     loading-renderer-bytes. Cache hit shortcut on
 *     type_renderer_cache, dispatcher-level dedup across two entries
 *     for the same type, fill-and-promote semantics, errored invoke
 *     drops active_lookups, malformed-result errors out.
 *
 *   PHASE 3 (bytes + compile + lookup):
 *     loading-renderer-bytes → resolved component published. Happy
 *     path, cross-entry compile dedup, subtype_key miss + key.uuid
 *     miss (both render NoMatch + console.warn), load failure,
 *     compile failure, bad bytes shape.
 *
 * The dynamic dispatcher takes no registry — `DispatchKey`s carry
 * their own fallback bundle (Loading / NoMatch / Error / optional
 * Builtin), and the dispatcher reads from there. Per-key overrides
 * are passed via the optional `overrides?: ReadonlyArray<KeyFallbackOverride>`
 * option; this test file exercises the no-override path (the
 * fallbacks come straight off the key).
 *
 * Bytes-load + compile is plumbed through the `loadEntityData` +
 * `compileRendererBytes` options. Phase-1 / phase-2 tests use the
 * default never-resolving loader (`NEVER_RESOLVES_LOAD`) so the
 * state machine pauses at `loading-renderer-bytes` with
 * `{status: "loading"}` in `compiledModulesAtom`; phase-3 tests
 * supply working mocks.
 */

import type {
	EntityRefStub,
	FxtrSessionAtom,
	OpResultStub,
	ServiceRefStub,
	Session,
	SessionLifecycleState,
	UserState,
} from "@fxtr-ui/core";
import { type PrimitiveAtom, atom, createStore } from "jotai";
import { describe, expect, it, vi } from "vitest";

import {
	type CompiledRendererModule,
	type CreateDynamicDispatcherOptions,
	type DynamicDispatcherUserState,
	type DynamicEntityDispatchState,
	type DynamicServiceDispatchState,
	EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
	createDynamicDispatcher,
} from "../src/dynamic_dispatcher";
import type { EntityKeyFallbacks, ServiceKeyFallbacks } from "../src/fallbacks";
import { declareEntityDispatchKey, declareServiceDispatchKey } from "../src/keys";
import type { DispatchedComponent, ErrorFallbackComponent } from "../src/registry_types";
import { getStaleAwareInner } from "../src/staleness_wrapper";
import {
	type MockInvokeCall,
	TestCallbackInboxStub,
	TestEntityRefStub,
	TestOpResultStub,
	TestServiceRefStub,
} from "./_mocks";

// --- Sentinel components (compared by identity) ----------------------
//
// Note on wrapping: the dynamic dispatcher wraps every component
// it publishes to `componentAtom` with a `StaleAware(inner)` HOC
// (see `staleness_wrapper.tsx`) for the hot-reload staleness
// affordance. The wrap is identity-stable per inner-component
// (WeakMap-memoised) but is *not* the same identity as the bare
// inner. Tests inspect the inner via `getStaleAwareInner` through
// the helper below.

/**
 * Resolve the inner component from a stale-aware wrap and assert
 * the wrap is present. Returns the inner so the caller can compose
 * with `toBe` / `not.toBe` for identity assertions.
 *
 * Two things this asserts:
 *   1. The value pulled from `componentAtom` actually went through
 *      the dispatcher's wrap (catches regressions where a code
 *      path forgets to wrap).
 *   2. The recovered inner is then up to the caller to compare via
 *      `expect(...).toBe(LoadingFallback)` etc.
 */
function staleAwareInnerOf(
	actual: DispatchedComponent<EntityRefStub | ServiceRefStub, object>,
): DispatchedComponent<EntityRefStub | ServiceRefStub, object> {
	const inner = getStaleAwareInner(actual);
	expect(inner, "expected componentAtom value to be a stale-aware-wrapped component").toBeDefined();
	return inner as DispatchedComponent<EntityRefStub | ServiceRefStub, object>;
}

const LoadingFallback = function LoadingFallback(_p: { stub: unknown }) {
	return null;
} as unknown as DispatchedComponent<EntityRefStub | ServiceRefStub, object>;
const NoMatchFallback = function NoMatchFallback(_p: { stub: unknown }) {
	return null;
} as unknown as DispatchedComponent<EntityRefStub | ServiceRefStub, object>;
const Builtin = function Builtin(_p: { stub: unknown }) {
	return null;
} as unknown as DispatchedComponent<EntityRefStub, object>;
const ErrorFallback = function ErrorFallback(_p: { stub: unknown; error: unknown }) {
	return null;
} as unknown as ErrorFallbackComponent<EntityRefStub | ServiceRefStub, object>;

// Two test keys: an entity-only key with a Builtin fallback and a
// service-only key without one. Each key carries its own fallback
// bundle now (declare* takes them at construction time), so the
// dynamic dispatcher needs no per-key registration.
const entityKeyFallbacks: EntityKeyFallbacks<object> = {
	Loading: LoadingFallback as DispatchedComponent<EntityRefStub, object>,
	NoMatch: NoMatchFallback as DispatchedComponent<EntityRefStub, object>,
	Error: ErrorFallback as ErrorFallbackComponent<EntityRefStub, object>,
	Builtin,
};
const serviceKeyFallbacks: ServiceKeyFallbacks<object> = {
	Loading: LoadingFallback as DispatchedComponent<ServiceRefStub, object>,
	NoMatch: NoMatchFallback as DispatchedComponent<ServiceRefStub, object>,
	Error: ErrorFallback as ErrorFallbackComponent<ServiceRefStub, object>,
};

const EntityKey = declareEntityDispatchKey<object>({
	uuid: "00000000-0000-4000-8000-000000000001",
	description: "DynamicDispatcherTestEntityKey",
	fallbacks: entityKeyFallbacks,
});
const ServiceKey = declareServiceDispatchKey<object>({
	uuid: "00000000-0000-4000-8000-000000000002",
	description: "DynamicDispatcherTestServiceKey",
	fallbacks: serviceKeyFallbacks,
});

// A variant entity key that omits Builtin — used by the
// "falls through to NoMatch when no Builtin is set" test.
const EntityKeyNoBuiltin = declareEntityDispatchKey<object>({
	uuid: "00000000-0000-4000-8000-000000000003",
	description: "DynamicDispatcherTestEntityKeyNoBuiltin",
	fallbacks: {
		Loading: entityKeyFallbacks.Loading,
		NoMatch: entityKeyFallbacks.NoMatch,
		Error: entityKeyFallbacks.Error,
		// no Builtin
	},
});

// --- Mock session ----------------------------------------------------

/**
 * Mock Session whose userState atom threads a ctx with
 * controllable allocateLoad / allocateInvoke through caller-
 * supplied queues. The session shares the store with the
 * dispatcher so writes to `_resultPrivate` are visible to the
 * effect-mounted state machine on the next pass.
 */
function makeMockSession(): {
	readonly session: Session;
	readonly userStatePrivate: PrimitiveAtom<UserState | null>;
	readonly lifecyclePrivate: PrimitiveAtom<SessionLifecycleState>;
	readonly loadQueue: Array<TestOpResultStub<unknown>>;
	readonly invokeQueue: Array<TestOpResultStub<unknown>>;
	readonly invokeCalls: Array<MockInvokeCall>;
	/**
	 * Args of every `subscribe_to` invoke the dispatcher's reload
	 * subscription issued (first-connect + each reload-on-break
	 * resubscribe). Recorded separately from `invokeCalls` since the
	 * subscribe_to is a dispatcher-internal detail, not a
	 * user-visible invoke.
	 */
	readonly subscribeToCalls: Array<ReadonlyArray<unknown>>;
	readonly detachCalls: { count: number };
} {
	const store = createStore();
	const userStatePrivate = atom<UserState | null>(null);
	const lifecyclePrivate = atom<SessionLifecycleState>("connected");
	const detachCalls = { count: 0 };
	const loadQueue: Array<TestOpResultStub<unknown>> = [];
	const invokeQueue: Array<TestOpResultStub<unknown>> = [];
	const invokeCalls: Array<MockInvokeCall> = [];
	const subscribeToCalls: Array<ReadonlyArray<unknown>> = [];

	// biome-ignore lint/style/useConst: forward declaration so the ctx getter can close over it before assignment
	let sessionRef: Session | undefined;
	const ctx = {
		allocateLoad: <T>(_ref: EntityRefStub<T>) => {
			const stub = loadQueue.shift();
			if (stub === undefined) {
				throw new Error("mock session: load queue exhausted");
			}
			return stub as unknown as OpResultStub<T>;
		},
		allocateInvoke: ((
			svc: ServiceRefStub<unknown>,
			method: string,
			args: unknown[],
			kwargs?: Record<string, unknown>,
		) => {
			// The dispatcher's reload subscription dispatches a
			// callback-target `subscribe_to(dispatchServiceRef)` on
			// the fresh inbox's handle once on connect. It's a fixed
			// dispatcher-internal detail not under test here, so we
			// silently fulfil it without consuming the user-visible
			// invoke queue or showing up in `invokeCalls`.
			if (method === "subscribe_to") {
				subscribeToCalls.push(args);
				return new TestOpResultStub<null>({
					status: "pending",
				}) as unknown as OpResultStub<unknown>;
			}
			const next = invokeQueue.shift();
			if (next === undefined) {
				throw new Error("mock session: invoke queue exhausted");
			}
			const call: MockInvokeCall =
				kwargs === undefined ? { stub: svc, method, args } : { stub: svc, method, args, kwargs };
			invokeCalls.push(call);
			return next as unknown as OpResultStub<unknown>;
			// biome-ignore lint/suspicious/noExplicitAny: matches FxtrSessionUpdateCtx.allocateInvoke
		}) as any,
		allocateGetEntityMetadata: () => {
			throw new Error("mock session: allocateGetEntityMetadata not wired");
		},
		allocateGetServiceMetadata: () => {
			throw new Error("mock session: allocateGetServiceMetadata not wired");
		},
		allocateCallback: <R>() => {
			// The dispatcher's reload subscription allocates one
			// callback inbox on connect. Hand back a default test
			// stub; tests that exercise the reload-reception path
			// directly can override by reaching for it via
			// `m.session.store.get(m.userStatePrivate)?.reload_inbox`.
			return new TestCallbackInboxStub<R>();
		},
		get session() {
			if (sessionRef === undefined) {
				throw new Error("mock session: session ref not set yet");
			}
			return sessionRef;
		},
	};

	const userState: FxtrSessionAtom<UserState | null> = atom(
		(get) => get(userStatePrivate),
		(get, set, update, _options) => {
			const prev = get(userStatePrivate);
			const next =
				typeof update === "function"
					? (update as (p: UserState | null, c: typeof ctx) => UserState | null)(prev, ctx)
					: update;
			set(userStatePrivate, next as UserState | null);
		},
	) as FxtrSessionAtom<UserState | null>;

	sessionRef = {
		sessionId: "test-session",
		store,
		lifecycle: lifecyclePrivate,
		userState,
		detach: async () => {
			detachCalls.count += 1;
		},
		terminate: async () => {},
	};

	return {
		session: sessionRef,
		userStatePrivate,
		lifecyclePrivate,
		loadQueue,
		invokeQueue,
		invokeCalls,
		subscribeToCalls,
		detachCalls,
	};
}

/**
 * Subscribe to an entry's componentAtom to mount the underlying
 * jotai-effect (which is what drives the state machine forward).
 * The notify callback is a no-op — tests read the latest value via
 * `store.get(entry.componentAtom)` after awaiting microtasks.
 */
function subscribeEntry(
	store: ReturnType<typeof createStore>,
	componentAtom: import("jotai").Atom<unknown>,
): () => void {
	return store.sub(componentAtom, () => {
		/* no-op; tests read with store.get */
	});
}

/** Helper: drain pending microtasks so jotai-effect runs. */
async function flushMicrotasks(): Promise<void> {
	// jotai-effect schedules effects via microtasks; a couple of
	// awaits clears chains of state-driven re-runs.
	for (let i = 0; i < 5; i++) {
		await Promise.resolve();
	}
}

/**
 * A `loadEntityData` mock that returns a never-resolving promise.
 * Phase-3 kicks `ensureCompiled` as soon as the state machine
 * reaches `loading-renderer-bytes`, which writes
 * `{status: "loading"}` to `compiledModulesAtom` synchronously
 * and then awaits this loader. With a never-resolving loader, the
 * compile stays "loading" indefinitely — perfect for phase-1 /
 * phase-2 tests that want the state machine paused before the
 * bytes-load mechanic runs.
 */
const NEVER_RESOLVES_LOAD: CreateDynamicDispatcherOptions["loadEntityData"] = () =>
	new Promise(() => {
		/* never resolves; pending forever */
	});

/**
 * Build a dispatcher with default mocks. Overrides let phase-3
 * tests supply working `loadEntityData` + `compileRendererBytes`
 * implementations.
 */
function makeDispatcher(
	m: ReturnType<typeof makeMockSession>,
	opts: Partial<Omit<CreateDynamicDispatcherOptions, "session">> = {},
): ReturnType<typeof createDynamicDispatcher> {
	return createDynamicDispatcher({
		session: m.session,
		dispatchServiceRef: opts.dispatchServiceRef ?? new TestServiceRefStub("dispatch-svc"),
		loadEntityData: opts.loadEntityData ?? NEVER_RESOLVES_LOAD,
		...(opts.compileRendererBytes !== undefined
			? { compileRendererBytes: opts.compileRendererBytes }
			: {}),
		...(opts.overrides !== undefined ? { overrides: opts.overrides } : {}),
	});
}

function readUserState(m: ReturnType<typeof makeMockSession>): DynamicDispatcherUserState {
	const raw = m.session.store.get(m.userStatePrivate) as DynamicDispatcherUserState | null;
	return raw ?? EMPTY_DYNAMIC_DISPATCHER_USER_STATE;
}

// =====================================================================
// Entity-side phase 1
// =====================================================================

describe("DynamicDispatcher entity phase 1", () => {
	it("transitions init → loading-entity → resolving-renderer + allocates the resolve invoke", async () => {
		const m = makeMockSession();
		const typeRef = new TestEntityRefStub("type-aaa");
		// Pre-filled load result: state machine cascades through to
		// resolving-renderer in one flush. The resolving-renderer arm
		// then allocates a pending `resolve` invoke on the dispatcher
		// service (phase 2's cold path), which the test queues here.
		const loadStub = new TestOpResultStub<unknown>({
			status: "filled",
			value: { "$fxtr.type": typeRef },
		});
		m.loadQueue.push(loadStub);
		const resolveStub = new TestOpResultStub<unknown>({ status: "pending" });
		m.invokeQueue.push(resolveStub);

		const dispatchServiceRef = new TestServiceRefStub("dispatch-svc");
		const dispatcher = makeDispatcher(m, { dispatchServiceRef });
		const stub = new TestEntityRefStub("e-1");
		const entry = dispatcher.getEntry(EntityKey, stub);
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		// Entry sits at resolving-renderer holding the type ref, with
		// the resolve invoke parked in type_renderer_active_lookups.
		const state = readUserState(m).entries[EntityKey.uuid]?.["e-1"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("resolving-renderer");
		expect((state as { typeRef: EntityRefStub }).typeRef.shortId).toBe("type-aaa");
		expect(readUserState(m).type_renderer_active_lookups["type-aaa"]?.[EntityKey.uuid]).toBe(
			resolveStub,
		);
		expect(m.invokeCalls.length).toBe(1);
		expect(m.invokeCalls[0]?.method).toBe("resolve");
		expect(m.invokeCalls[0]?.stub).toBe(dispatchServiceRef);
		expect(m.invokeCalls[0]?.args).toEqual([typeRef, EntityKey.uuid]);
		// The componentAtom is still at the seed Loading value — no
		// renderer has been published yet.
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(LoadingFallback);
	});

	it("short-circuits a `.`-prefixed builtin stub to resolved-builtin without a load", async () => {
		const m = makeMockSession();
		const dispatcher = makeDispatcher(m);
		const stub = new TestEntityRefStub(".anonymous-thing");
		const entry = dispatcher.getEntry(EntityKey, stub);
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		expect(m.loadQueue.length).toBe(0);
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(Builtin);
		expect(
			readUserState(m).entries[EntityKey.uuid]?.[".anonymous-thing"]
				?.dispatcher_state as DynamicEntityDispatchState,
		).toEqual({ phase: "resolved-builtin" });
	});

	it("short-circuits a `u`-prefixed (content-less provenance wrap) stub without a load", async () => {
		const m = makeMockSession();
		const dispatcher = makeDispatcher(m);
		const stub = new TestEntityRefStub("u!demo_type");
		const entry = dispatcher.getEntry(EntityKey, stub);
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		expect(m.loadQueue.length).toBe(0);
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(Builtin);
		expect(
			readUserState(m).entries[EntityKey.uuid]?.["u!demo_type"]
				?.dispatcher_state as DynamicEntityDispatchState,
		).toEqual({ phase: "resolved-builtin" });
	});

	it("falls through to NoMatchFallback for builtin when the key has no Builtin component", async () => {
		const m = makeMockSession();
		const dispatcher = makeDispatcher(m);
		const stub = new TestEntityRefStub("!builtin-thing");
		const entry = dispatcher.getEntry(EntityKeyNoBuiltin, stub);
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(NoMatchFallback);
	});

	it("transitions to errored when the load resolves to errored", async () => {
		const m = makeMockSession();
		const loadStub = new TestOpResultStub<unknown>({
			status: "errored",
			message: "boom",
		});
		m.loadQueue.push(loadStub);

		const dispatcher = makeDispatcher(m);
		const entry = dispatcher.getEntry(EntityKey, new TestEntityRefStub("e-err"));
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		const state = readUserState(m).entries[EntityKey.uuid]?.["e-err"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("errored");
		expect((state as { message: string }).message).toBe("boom");
	});

	it("transitions to errored when the loaded value is missing $fxtr.type", async () => {
		const m = makeMockSession();
		const loadStub = new TestOpResultStub<unknown>({
			status: "filled",
			value: { foo: 1 },
		});
		m.loadQueue.push(loadStub);

		const dispatcher = makeDispatcher(m);
		const entry = dispatcher.getEntry(EntityKey, new TestEntityRefStub("e-missing"));
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		const state = readUserState(m).entries[EntityKey.uuid]?.["e-missing"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("errored");
		expect((state as { message: string }).message).toMatch(/\$fxtr\.type/);
	});
});

// =====================================================================
// Service-side phase 1
// =====================================================================

describe("DynamicDispatcher service phase 1", () => {
	it("allocates a $fxtr.type invoke on a cold service and promotes into service_type_cache when it fills", async () => {
		const m = makeMockSession();
		const typeRef = new TestEntityRefStub("type-aaa");
		// Phase 1's $fxtr.type invoke — pre-filled so the service
		// cascades through to resolving-renderer.
		const typeInvoke = new TestOpResultStub<unknown>({
			status: "filled",
			value: typeRef,
		});
		// Phase 2's resolve invoke — left pending so the test
		// asserts on the resolving-renderer terminal.
		const resolveInvoke = new TestOpResultStub<unknown>({ status: "pending" });
		m.invokeQueue.push(typeInvoke, resolveInvoke);

		const dispatcher = makeDispatcher(m);
		const svcStub = new TestServiceRefStub("s-1");
		const entry = dispatcher.getEntry(ServiceKey, svcStub);
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		// Two invokes happened: $fxtr.type on the service stub,
		// then resolve(typeRef) on the dispatcher service.
		expect(m.invokeCalls.length).toBe(2);
		expect(m.invokeCalls[0]?.method).toBe("$fxtr.type");
		expect(m.invokeCalls[0]?.stub).toBe(svcStub);
		expect(m.invokeCalls[0]?.args).toEqual([]);
		expect(m.invokeCalls[1]?.method).toBe("resolve");
		expect(m.invokeCalls[1]?.args).toEqual([typeRef, ServiceKey.uuid]);

		const root = readUserState(m);
		// Phase 1 cache populated, active_lookup dropped.
		expect(root.service_type_cache["s-1"]).toBe(typeRef);
		expect(root.service_type_active_lookups["s-1"]).toBeUndefined();
		// Phase 2 active_lookup holds the in-flight resolve, nested by
		// (typeShortId, keyUuid).
		expect(root.type_renderer_active_lookups["type-aaa"]?.[ServiceKey.uuid]).toBe(resolveInvoke);
		// Still in resolving-renderer; component hasn't advanced.
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(LoadingFallback);
	});

	it("hits the service_type_cache on a second entry for the same service — no $fxtr.type invoke", async () => {
		const m = makeMockSession();
		const typeRef = new TestEntityRefStub("type-aaa");
		// Pre-seed the cache as if a prior resolution had populated
		// it (simulates the survives-reattach behaviour). The
		// resolving-renderer phase still kicks a `resolve` invoke
		// (phase-2 cache is empty), so queue one pending stub.
		m.session.store.set(m.userStatePrivate, {
			...EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
			service_type_cache: { "s-1": typeRef },
		} as unknown as UserState);
		m.invokeQueue.push(new TestOpResultStub<unknown>({ status: "pending" }));

		const dispatcher = makeDispatcher(m);
		const entry = dispatcher.getEntry(ServiceKey, new TestServiceRefStub("s-1"));
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		// Only one invoke — the phase-2 `resolve`. The $fxtr.type
		// call was short-circuited by the cache hit.
		expect(m.invokeCalls.length).toBe(1);
		expect(m.invokeCalls[0]?.method).toBe("resolve");
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(LoadingFallback);
	});

	it("dedups concurrent in-flight $fxtr.type invokes across two entries for the same service", async () => {
		const m = makeMockSession();
		const typeInvoke = new TestOpResultStub<unknown>({ status: "pending" });
		m.invokeQueue.push(typeInvoke);

		const dispatcher = makeDispatcher(m);
		const entryA = dispatcher.getEntry(ServiceKey, new TestServiceRefStub("s-shared"));
		subscribeEntry(m.session.store, entryA.componentAtom);
		await flushMicrotasks();

		expect(m.invokeCalls.length).toBe(1);
		expect(readUserState(m).service_type_active_lookups["s-shared"]).toBe(typeInvoke);

		// Second entry — should observe the in-flight $fxtr.type
		// invoke from active_lookups, not allocate a fresh one.
		const entryB = dispatcher.getEntry(ServiceKey, new TestServiceRefStub("s-shared"));
		subscribeEntry(m.session.store, entryB.componentAtom);
		await flushMicrotasks();
		expect(m.invokeCalls.length).toBe(1);

		// Fill the $fxtr.type invoke; both entries advance into
		// resolving-renderer and both observe the shared phase-2
		// `resolve` invoke through type_renderer_active_lookups.
		// Phase-2 dedup means only one resolve call is allocated
		// across both entries.
		const typeRef = new TestEntityRefStub("type-shared");
		const resolveInvoke = new TestOpResultStub<unknown>({ status: "pending" });
		m.invokeQueue.push(resolveInvoke);
		m.session.store.set(typeInvoke._resultPrivate, {
			status: "filled",
			value: typeRef,
		});
		await flushMicrotasks();

		expect(m.invokeCalls.length).toBe(2);
		expect(m.invokeCalls[1]?.method).toBe("resolve");
		const root = readUserState(m);
		expect(root.service_type_cache["s-shared"]).toBe(typeRef);
		expect(root.service_type_active_lookups["s-shared"]).toBeUndefined();
		expect(root.type_renderer_active_lookups["type-shared"]?.[ServiceKey.uuid]).toBe(resolveInvoke);
		expect(staleAwareInnerOf(m.session.store.get(entryA.componentAtom))).toBe(LoadingFallback);
		expect(staleAwareInnerOf(m.session.store.get(entryB.componentAtom))).toBe(LoadingFallback);
	});

	it("transitions to errored and drops active_lookups when the invoke errors", async () => {
		const m = makeMockSession();
		const invokeStub = new TestOpResultStub<unknown>({ status: "pending" });
		m.invokeQueue.push(invokeStub);

		const dispatcher = makeDispatcher(m);
		const entry = dispatcher.getEntry(ServiceKey, new TestServiceRefStub("s-err"));
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();
		expect(readUserState(m).service_type_active_lookups["s-err"]).toBe(invokeStub);

		m.session.store.set(invokeStub._resultPrivate, {
			status: "errored",
			message: "service exploded",
		});
		await flushMicrotasks();

		const state = readUserState(m).entries[ServiceKey.uuid]?.["s-err"]
			?.dispatcher_state as DynamicServiceDispatchState;
		expect(state.phase).toBe("errored");
		expect((state as { message: string }).message).toBe("service exploded");
		expect(readUserState(m).service_type_active_lookups["s-err"]).toBeUndefined();
	});

	it("transitions to errored if the invoke result is not an EntityRefStub", async () => {
		const m = makeMockSession();
		const invokeStub = new TestOpResultStub<unknown>({ status: "pending" });
		m.invokeQueue.push(invokeStub);

		const dispatcher = makeDispatcher(m);
		const entry = dispatcher.getEntry(ServiceKey, new TestServiceRefStub("s-wrongtype"));
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		m.session.store.set(invokeStub._resultPrivate, {
			status: "filled",
			// biome-ignore lint/suspicious/noExplicitAny: deliberately wrong-typed
			value: "not-an-entity-ref" as any,
		});
		await flushMicrotasks();

		const state = readUserState(m).entries[ServiceKey.uuid]?.["s-wrongtype"]
			?.dispatcher_state as DynamicServiceDispatchState;
		expect(state.phase).toBe("errored");
		expect(readUserState(m).service_type_active_lookups["s-wrongtype"]).toBeUndefined();
	});
});

// =====================================================================
// Phase 2 — renderer resolution via the dispatcher service
// =====================================================================

describe("DynamicDispatcher phase 2 (renderer resolution)", () => {
	/**
	 * Helper for these tests: build a dispatcher + an entity entry
	 * whose load result already carries `$fxtr.type: typeRef`, so
	 * the entity-side state machine reaches `resolving-renderer`
	 * in one effect pass.
	 */
	async function setupEntityAtResolvingRenderer(opts: {
		readonly entityShortId: string;
		readonly typeRef: EntityRefStub;
		readonly resolveInvoke: TestOpResultStub<unknown>;
	}) {
		const m = makeMockSession();
		const loadStub = new TestOpResultStub<unknown>({
			status: "filled",
			value: { "$fxtr.type": opts.typeRef },
		});
		m.loadQueue.push(loadStub);
		m.invokeQueue.push(opts.resolveInvoke);
		const dispatchServiceRef = new TestServiceRefStub("dispatch-svc");
		const dispatcher = makeDispatcher(m, { dispatchServiceRef });
		const entry = dispatcher.getEntry(EntityKey, new TestEntityRefStub(opts.entityShortId));
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();
		return { m, dispatcher, entry, dispatchServiceRef };
	}

	it("hits type_renderer_cache on a second entry for the same type — no second resolve invoke", async () => {
		const m = makeMockSession();
		const typeRef = new TestEntityRefStub("type-aaa");
		const rendererRef = new TestEntityRefStub("renderer-rrr");
		// Pre-seed both phase-1 (an entry's $fxtr.type resolves to
		// typeRef) AND phase-2 caches: the entry's resolving-renderer
		// pass should find the cache, transition straight to
		// loading-renderer-bytes, never call resolve.
		m.session.store.set(m.userStatePrivate, {
			...EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
			type_renderer_cache: {
				"type-aaa": {
					[EntityKey.uuid]: { subtype_key: "default", renderer: rendererRef },
				},
			},
		} as unknown as UserState);
		const loadStub = new TestOpResultStub<unknown>({
			status: "filled",
			value: { "$fxtr.type": typeRef },
		});
		m.loadQueue.push(loadStub);
		const dispatcher = makeDispatcher(m);
		const entry = dispatcher.getEntry(EntityKey, new TestEntityRefStub("e-1"));
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		// No resolve call happened; the state machine took the cache
		// shortcut and landed at loading-renderer-bytes. With the
		// default never-resolving loader, the bytes load stays in
		// `{status: "loading"}` and the component stays at the seed
		// LoadingFallback.
		expect(m.invokeCalls.length).toBe(0);
		const state = readUserState(m).entries[EntityKey.uuid]?.["e-1"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("loading-renderer-bytes");
		expect((state as { subtype_key: string }).subtype_key).toBe("default");
		expect((state as { renderer: EntityRefStub }).renderer).toBe(rendererRef);
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(LoadingFallback);
	});

	it("dedups concurrent in-flight resolves across two entries for the same type", async () => {
		// Two entities of the same type. Both should kick the entity
		// load (independent), both transition into resolving-renderer,
		// but only ONE allocates the `resolve` invoke — the second
		// observes the in-flight slot in type_renderer_active_lookups.
		const m = makeMockSession();
		const typeRef = new TestEntityRefStub("type-shared");
		const loadA = new TestOpResultStub<unknown>({
			status: "filled",
			value: { "$fxtr.type": typeRef },
		});
		const loadB = new TestOpResultStub<unknown>({
			status: "filled",
			value: { "$fxtr.type": typeRef },
		});
		m.loadQueue.push(loadA, loadB);
		const resolveInvoke = new TestOpResultStub<unknown>({ status: "pending" });
		m.invokeQueue.push(resolveInvoke);

		const dispatcher = makeDispatcher(m);
		const entryA = dispatcher.getEntry(EntityKey, new TestEntityRefStub("e-a"));
		const entryB = dispatcher.getEntry(EntityKey, new TestEntityRefStub("e-b"));
		subscribeEntry(m.session.store, entryA.componentAtom);
		subscribeEntry(m.session.store, entryB.componentAtom);
		await flushMicrotasks();

		// One resolve call total, shared via active_lookups.
		expect(m.invokeCalls.length).toBe(1);
		expect(m.invokeCalls[0]?.method).toBe("resolve");
		expect(readUserState(m).type_renderer_active_lookups["type-shared"]?.[EntityKey.uuid]).toBe(
			resolveInvoke,
		);

		// Fill the resolve. Both entries advance through to
		// loading-renderer-bytes; the cache is populated; the
		// active_lookups entry is dropped.
		const rendererRef = new TestEntityRefStub("renderer-shared");
		m.session.store.set(resolveInvoke._resultPrivate, {
			status: "filled",
			value: { subtype_key: "default", renderer: rendererRef },
		});
		await flushMicrotasks();

		const root = readUserState(m);
		expect(root.type_renderer_cache["type-shared"]?.[EntityKey.uuid]).toEqual({
			subtype_key: "default",
			renderer: rendererRef,
		});
		expect(root.type_renderer_active_lookups["type-shared"]).toBeUndefined();
		const stateA = root.entries[EntityKey.uuid]?.["e-a"]
			?.dispatcher_state as DynamicEntityDispatchState;
		const stateB = root.entries[EntityKey.uuid]?.["e-b"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(stateA.phase).toBe("loading-renderer-bytes");
		expect(stateB.phase).toBe("loading-renderer-bytes");
		// With the default never-resolving loader, the bytes load
		// stays in `{status: "loading"}` and both entries' components
		// remain at the seed LoadingFallback.
		expect(staleAwareInnerOf(m.session.store.get(entryA.componentAtom))).toBe(LoadingFallback);
		expect(staleAwareInnerOf(m.session.store.get(entryB.componentAtom))).toBe(LoadingFallback);
	});

	it("transitions to errored and drops type_renderer_active_lookups when the resolve invoke errors", async () => {
		const typeRef = new TestEntityRefStub("type-err");
		const resolveInvoke = new TestOpResultStub<unknown>({ status: "pending" });
		const { m, entry } = await setupEntityAtResolvingRenderer({
			entityShortId: "e-err",
			typeRef,
			resolveInvoke,
		});
		expect(readUserState(m).type_renderer_active_lookups["type-err"]?.[EntityKey.uuid]).toBe(
			resolveInvoke,
		);

		m.session.store.set(resolveInvoke._resultPrivate, {
			status: "errored",
			message: "dispatcher service unhappy",
		});
		await flushMicrotasks();

		const state = readUserState(m).entries[EntityKey.uuid]?.["e-err"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("errored");
		expect((state as { message: string }).message).toBe("dispatcher service unhappy");
		// `removeNestedKey` drops the outer key entirely once its inner
		// record empties out — both equivalent forms (outer-missing and
		// inner-key-missing) read as `undefined`.
		expect(readUserState(m).type_renderer_active_lookups["type-err"]).toBeUndefined();
		// Read entry so the unused-binding lint doesn't fire — it
		// also confirms the entry surface is still valid post-error.
		expect(entry.id).toBeDefined();
	});

	it("transitions to errored if the resolve result has the wrong shape", async () => {
		const typeRef = new TestEntityRefStub("type-bad-shape");
		const resolveInvoke = new TestOpResultStub<unknown>({ status: "pending" });
		const { m } = await setupEntityAtResolvingRenderer({
			entityShortId: "e-bad-shape",
			typeRef,
			resolveInvoke,
		});

		// Result is missing `renderer`, or has it as a string instead
		// of an EntityRefStub.
		m.session.store.set(resolveInvoke._resultPrivate, {
			status: "filled",
			value: { subtype_key: "default", renderer: "not-an-entity-ref" },
		});
		await flushMicrotasks();

		const state = readUserState(m).entries[EntityKey.uuid]?.["e-bad-shape"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("errored");
		expect((state as { message: string }).message).toMatch(/subtype_key|renderer|shape/);
		expect(readUserState(m).type_renderer_active_lookups["type-bad-shape"]).toBeUndefined();
		// nb: the outer key drops when its inner record empties (see
		// `removeNestedKey` in dynamic_dispatcher.ts).
	});

	it("transitions to no-match (publishing NoMatchFallback) when the resolve invoke returns null", async () => {
		const typeRef = new TestEntityRefStub("type-no-match");
		const resolveInvoke = new TestOpResultStub<unknown>({ status: "pending" });
		const { m, entry } = await setupEntityAtResolvingRenderer({
			entityShortId: "e-no-match",
			typeRef,
			resolveInvoke,
		});
		expect(readUserState(m).type_renderer_active_lookups["type-no-match"]?.[EntityKey.uuid]).toBe(
			resolveInvoke,
		);

		m.session.store.set(resolveInvoke._resultPrivate, {
			status: "filled",
			value: null,
		});
		await flushMicrotasks();

		const root = readUserState(m);
		const state = root.entries[EntityKey.uuid]?.["e-no-match"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("no-match");
		// The null result is cached (nested by keyUuid) so subsequent
		// dispatches for the same (type, key) don't re-invoke `resolve`.
		expect(Object.prototype.hasOwnProperty.call(root.type_renderer_cache, "type-no-match")).toBe(
			true,
		);
		expect(root.type_renderer_cache["type-no-match"]?.[EntityKey.uuid]).toBe(null);
		expect(root.type_renderer_active_lookups["type-no-match"]).toBeUndefined();
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(NoMatchFallback);
	});

	it("hits the type_renderer_cache with a cached null → no-match without re-invoking resolve", async () => {
		const m = makeMockSession();
		const typeRef = new TestEntityRefStub("type-cached-null");
		// Pre-seed phase-2 cache with a null entry — the second
		// entry should observe it and transition straight to
		// no-match without calling resolve.
		m.session.store.set(m.userStatePrivate, {
			...EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
			type_renderer_cache: {
				"type-cached-null": { [EntityKey.uuid]: null },
			},
		} as unknown as UserState);
		const loadStub = new TestOpResultStub<unknown>({
			status: "filled",
			value: { "$fxtr.type": typeRef },
		});
		m.loadQueue.push(loadStub);
		const dispatcher = makeDispatcher(m);
		const entry = dispatcher.getEntry(EntityKey, new TestEntityRefStub("e-cached"));
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();

		expect(m.invokeCalls.length).toBe(0);
		const state = readUserState(m).entries[EntityKey.uuid]?.["e-cached"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("no-match");
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(NoMatchFallback);
	});

	it("fill-and-promote drops the resolve slot from active_lookups in the same flush as the cache population", async () => {
		// Smoke check for the wire-footprint claim in the design
		// doc: after the result fills, the slot is no longer in
		// active_lookups (so the next flush has no reason to keep
		// the slot alive) and the answer is in cache.
		const typeRef = new TestEntityRefStub("type-promote");
		const resolveInvoke = new TestOpResultStub<unknown>({ status: "pending" });
		const { m } = await setupEntityAtResolvingRenderer({
			entityShortId: "e-promote",
			typeRef,
			resolveInvoke,
		});
		const rendererRef = new TestEntityRefStub("renderer-promote");
		m.session.store.set(resolveInvoke._resultPrivate, {
			status: "filled",
			value: { subtype_key: "default", renderer: rendererRef },
		});
		await flushMicrotasks();

		const root = readUserState(m);
		expect(root.type_renderer_cache["type-promote"]?.[EntityKey.uuid]).toEqual({
			subtype_key: "default",
			renderer: rendererRef,
		});
		expect(root.type_renderer_active_lookups["type-promote"]).toBeUndefined();
	});
});

// =====================================================================
// getEntry + detach (independent of state-machine specifics)
// =====================================================================

// =====================================================================
// Phase 3 — bytes load + ESM compile + module-map lookup
// =====================================================================

describe("DynamicDispatcher phase 3 (bytes + compile + lookup)", () => {
	/**
	 * Helper: build a dispatcher whose phase-2 cache is pre-seeded
	 * with a resolution `{subtype_key, renderer}`, so the state
	 * machine reaches `loading-renderer-bytes` after one pass.
	 * Returns the test scaffolding plus the renderer ref.
	 */
	async function setupEntryAtLoadingRendererBytes(opts: {
		readonly entityShortId: string;
		readonly typeRef: EntityRefStub;
		readonly subtypeKey: string;
		readonly rendererShortId: string;
		readonly loadEntityData: CreateDynamicDispatcherOptions["loadEntityData"];
		readonly compileRendererBytes?: CreateDynamicDispatcherOptions["compileRendererBytes"];
	}) {
		const m = makeMockSession();
		const rendererRef = new TestEntityRefStub(opts.rendererShortId);
		// Pre-seed phase-2 cache so resolving-renderer short-circuits
		// straight to loading-renderer-bytes.
		m.session.store.set(m.userStatePrivate, {
			...EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
			type_renderer_cache: {
				[opts.typeRef.shortId]: {
					[EntityKey.uuid]: {
						subtype_key: opts.subtypeKey,
						renderer: rendererRef,
					},
				},
			},
		} as unknown as UserState);
		const loadStub = new TestOpResultStub<unknown>({
			status: "filled",
			value: { "$fxtr.type": opts.typeRef },
		});
		m.loadQueue.push(loadStub);
		const dispatcher = makeDispatcher(m, {
			loadEntityData: opts.loadEntityData,
			...(opts.compileRendererBytes !== undefined
				? { compileRendererBytes: opts.compileRendererBytes }
				: {}),
		});
		const entry = dispatcher.getEntry(EntityKey, new TestEntityRefStub(opts.entityShortId));
		subscribeEntry(m.session.store, entry.componentAtom);
		await flushMicrotasks();
		return { m, dispatcher, entry, rendererRef };
	}

	it("publishes the resolved component from `module[subtype_key][key.uuid]` on a happy bytes-load", async () => {
		const TestComponent = function TestComponent() {
			return null;
		} as unknown as DispatchedComponent<EntityRefStub | ServiceRefStub, object>;
		const compiledModule: CompiledRendererModule = {
			default: { [EntityKey.uuid]: TestComponent },
		};

		const { m, entry } = await setupEntryAtLoadingRendererBytes({
			entityShortId: "e-happy",
			typeRef: new TestEntityRefStub("type-happy"),
			subtypeKey: "default",
			rendererShortId: "renderer-happy",
			loadEntityData: async () => ({ module: new Uint8Array([1, 2, 3]) }),
			compileRendererBytes: async () => compiledModule,
		});

		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(TestComponent);
	});

	it("dedups concurrent bytes-loads of the same renderer (loadEntityData called once)", async () => {
		// Two entities resolved to the same renderer entity — only
		// one loadEntityData call should happen.
		const m = makeMockSession();
		const typeRef = new TestEntityRefStub("type-dedup");
		const rendererRef = new TestEntityRefStub("renderer-dedup");
		m.session.store.set(m.userStatePrivate, {
			...EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
			type_renderer_cache: {
				"type-dedup": {
					[EntityKey.uuid]: { subtype_key: "default", renderer: rendererRef },
				},
			},
		} as unknown as UserState);
		const loadA = new TestOpResultStub<unknown>({
			status: "filled",
			value: { "$fxtr.type": typeRef },
		});
		const loadB = new TestOpResultStub<unknown>({
			status: "filled",
			value: { "$fxtr.type": typeRef },
		});
		m.loadQueue.push(loadA, loadB);
		let loadCallCount = 0;
		const TestComponent = function TestComponent() {
			return null;
		} as unknown as DispatchedComponent<EntityRefStub | ServiceRefStub, object>;
		const dispatcher = makeDispatcher(m, {
			loadEntityData: async () => {
				loadCallCount += 1;
				return { module: new Uint8Array([1, 2, 3]) };
			},
			compileRendererBytes: async () => ({
				default: { [EntityKey.uuid]: TestComponent },
			}),
		});
		const entryA = dispatcher.getEntry(EntityKey, new TestEntityRefStub("e-a"));
		const entryB = dispatcher.getEntry(EntityKey, new TestEntityRefStub("e-b"));
		subscribeEntry(m.session.store, entryA.componentAtom);
		subscribeEntry(m.session.store, entryB.componentAtom);
		await flushMicrotasks();

		expect(loadCallCount).toBe(1);
		expect(staleAwareInnerOf(m.session.store.get(entryA.componentAtom))).toBe(TestComponent);
		expect(staleAwareInnerOf(m.session.store.get(entryB.componentAtom))).toBe(TestComponent);
	});

	it("renders NoMatch + warns when the compiled module has no entry for subtype_key", async () => {
		const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
		try {
			const { m, entry } = await setupEntryAtLoadingRendererBytes({
				entityShortId: "e-no-subtype",
				typeRef: new TestEntityRefStub("type-no-subtype"),
				subtypeKey: "card",
				rendererShortId: "renderer-no-subtype",
				loadEntityData: async () => ({ module: new Uint8Array([1]) }),
				// Module only has "detail", not "card".
				compileRendererBytes: async () => ({
					detail: {
						[EntityKey.uuid]: function Detail() {
							return null;
						} as unknown as DispatchedComponent<EntityRefStub | ServiceRefStub, object>,
					},
				}),
			});
			expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(NoMatchFallback);
			expect(warnSpy).toHaveBeenCalled();
			const warning = warnSpy.mock.calls[0]?.[0] as string;
			expect(warning).toMatch(/subtype_key.*card|card.*subtype/i);
		} finally {
			warnSpy.mockRestore();
		}
	});

	it("renders NoMatch + warns when the module's subtype map has no entry for key.uuid", async () => {
		const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
		try {
			// Module has the right subtype but no entry for this
			// dispatch key's uuid.
			const OtherComponent = function OtherComponent() {
				return null;
			} as unknown as DispatchedComponent<EntityRefStub | ServiceRefStub, object>;
			const { m, entry } = await setupEntryAtLoadingRendererBytes({
				entityShortId: "e-no-key",
				typeRef: new TestEntityRefStub("type-no-key"),
				subtypeKey: "default",
				rendererShortId: "renderer-no-key",
				loadEntityData: async () => ({ module: new Uint8Array([1]) }),
				compileRendererBytes: async () => ({
					default: { "some-other-uuid": OtherComponent },
				}),
			});
			expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).toBe(NoMatchFallback);
			expect(warnSpy).toHaveBeenCalled();
			const warning = warnSpy.mock.calls[0]?.[0] as string;
			expect(warning).toMatch(/dispatch-key uuid/);
		} finally {
			warnSpy.mockRestore();
		}
	});

	it("transitions to errored when loadEntityData rejects", async () => {
		const { m, entry } = await setupEntryAtLoadingRendererBytes({
			entityShortId: "e-load-fail",
			typeRef: new TestEntityRefStub("type-load-fail"),
			subtypeKey: "default",
			rendererShortId: "renderer-load-fail",
			loadEntityData: async () => {
				throw new Error("load failed");
			},
		});
		const state = readUserState(m).entries[EntityKey.uuid]?.["e-load-fail"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("errored");
		expect((state as { message: string }).message).toBe("load failed");
		// Since the state machine writes ErrorThrower into
		// componentAtom and the test render path isn't active here,
		// just confirm componentAtom isn't NoMatch/Loading.
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).not.toBe(NoMatchFallback);
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).not.toBe(LoadingFallback);
	});

	it("transitions to errored when compileRendererBytes rejects", async () => {
		const { m, entry } = await setupEntryAtLoadingRendererBytes({
			entityShortId: "e-compile-fail",
			typeRef: new TestEntityRefStub("type-compile-fail"),
			subtypeKey: "default",
			rendererShortId: "renderer-compile-fail",
			loadEntityData: async () => ({ module: new Uint8Array([1]) }),
			compileRendererBytes: async () => {
				throw new Error("syntax error in compiled module");
			},
		});
		const state = readUserState(m).entries[EntityKey.uuid]?.["e-compile-fail"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("errored");
		expect((state as { message: string }).message).toBe("syntax error in compiled module");
		// ErrorThrower published (see above).
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).not.toBe(NoMatchFallback);
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).not.toBe(LoadingFallback);
	});

	it("transitions to errored when loadEntityData returns a value without a Uint8Array module field", async () => {
		const { m, entry } = await setupEntryAtLoadingRendererBytes({
			entityShortId: "e-bad-shape",
			typeRef: new TestEntityRefStub("type-bad-shape"),
			subtypeKey: "default",
			rendererShortId: "renderer-bad-shape",
			// Returns a string instead of an object with `module`.
			loadEntityData: async () => "not-an-object",
		});
		const state = readUserState(m).entries[EntityKey.uuid]?.["e-bad-shape"]
			?.dispatcher_state as DynamicEntityDispatchState;
		expect(state.phase).toBe("errored");
		expect((state as { message: string }).message).toMatch(/non-object|module/i);
		expect(staleAwareInnerOf(m.session.store.get(entry.componentAtom))).not.toBe(LoadingFallback);
	});
});

// =====================================================================
// getEntry + detach (independent of state-machine specifics)
// =====================================================================

describe("DynamicDispatcher getEntry + detach", () => {
	it("returns the same entry for repeat (key, stub) lookups", () => {
		const m = makeMockSession();
		const dispatcher = makeDispatcher(m);
		const stub = new TestEntityRefStub("e-1");
		const a = dispatcher.getEntry(EntityKey, stub);
		const b = dispatcher.getEntry(EntityKey, stub);
		expect(a).toBe(b);
		expect(a.id).toBe(`${EntityKey.uuid}::e-1`);
	});

	it("throws DispatcherClosedError on getEntry after detach()", async () => {
		const m = makeMockSession();
		const dispatcher = makeDispatcher(m);
		await dispatcher.detach();
		expect(m.detachCalls.count).toBe(1);
		expect(m.session.store.get(dispatcher.status)).toBe("closed");
		expect(() => dispatcher.getEntry(EntityKey, new TestEntityRefStub("e-1"))).toThrow();
	});
});

describe("DynamicDispatcher reload subscription (closed-inbox recovery)", () => {
	it("logs a warning and re-allocates when reattaching to a session whose reload_inbox is closed", async () => {
		// Pre-seed the userState with a `reload_inbox` whose `state`
		// atom reports `"closed"`. This simulates a reattach where
		// the workstation has dropped the original callback slot —
		// e.g. server restart since the session was created. The
		// dispatcher should log a warning and re-subscribe.
		const m = makeMockSession();
		const closedInbox = new TestCallbackInboxStub<EntityRefStub>({
			state: "closed",
		});
		m.session.store.set(m.userStatePrivate, {
			...EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
			reload_inbox: closedInbox,
			// biome-ignore lint/suspicious/noExplicitAny: pre-seeded userState narrowed to the dispatcher's shape
		} as any);

		const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
		try {
			makeDispatcher(m);
			// Allow the reload effect's microtask to run.
			await flushMicrotasks();

			expect(warnSpy).toHaveBeenCalled();
			const warning = warnSpy.mock.calls[0]?.join(" ") ?? "";
			expect(warning).toContain("reload_inbox is closed");

			// The dispatcher should have allocated a new inbox via
			// the mock session's default `allocateCallback`. Read
			// userState back to confirm.
			const after = m.session.store.get(m.userStatePrivate) as {
				reload_inbox: TestCallbackInboxStub<EntityRefStub> | null;
			} | null;
			expect(after?.reload_inbox).not.toBeNull();
			expect(after?.reload_inbox).not.toBe(closedInbox);
		} finally {
			warnSpy.mockRestore();
		}
	});
});

describe("DynamicDispatcher reload-on-break", () => {
	it("wipes the type caches and resubscribes (on the same handle) when connectionState flips to broken", async () => {
		const m = makeMockSession();
		const dispatcher = makeDispatcher(m);
		try {
			await flushMicrotasks();
			// First-connect subscribe fired exactly once.
			expect(m.subscribeToCalls.length).toBe(1);
			const inbox = readUserState(m).reload_inbox as TestCallbackInboxStub<EntityRefStub> | null;
			expect(inbox).not.toBeNull();
			if (inbox === null) throw new Error("unreachable");

			// Seed a non-empty type cache so the wipe is observable.
			m.session.store.set(
				m.userStatePrivate,
				(prev) =>
					({
						...(prev as unknown as DynamicDispatcherUserState),
						service_type_cache: { svc1: new TestEntityRefStub("type-1") },
						// biome-ignore lint/suspicious/noExplicitAny: pre-seeded userState narrowed to the dispatcher's shape
					}) as any,
			);
			expect(Object.keys(readUserState(m).service_type_cache)).toHaveLength(1);

			// Break the durable subscription.
			m.session.store.set(inbox._connectionStatePrivate, "broken");
			await flushMicrotasks();

			// Caches wiped, resubscribe issued on the SAME inbox handle
			// (no new callback allocated).
			expect(readUserState(m).service_type_cache).toEqual({});
			expect(m.subscribeToCalls.length).toBe(2);
			expect(readUserState(m).reload_inbox).toBe(inbox);
		} finally {
			await dispatcher.detach();
		}
	});

	it("reacts once per broken edge, and again only after recovery", async () => {
		const m = makeMockSession();
		const dispatcher = makeDispatcher(m);
		try {
			await flushMicrotasks();
			expect(m.subscribeToCalls.length).toBe(1);
			const inbox = readUserState(m).reload_inbox as TestCallbackInboxStub<EntityRefStub> | null;
			if (inbox === null) throw new Error("unreachable");

			// First broken edge → one resubscribe.
			m.session.store.set(inbox._connectionStatePrivate, "broken");
			await flushMicrotasks();
			expect(m.subscribeToCalls.length).toBe(2);

			// Still broken: no further effect work, no resubscribe storm.
			await flushMicrotasks();
			expect(m.subscribeToCalls.length).toBe(2);

			// Recover, then break again → fires once more.
			m.session.store.set(inbox._connectionStatePrivate, "connected");
			await flushMicrotasks();
			expect(m.subscribeToCalls.length).toBe(2);
			m.session.store.set(inbox._connectionStatePrivate, "broken");
			await flushMicrotasks();
			expect(m.subscribeToCalls.length).toBe(3);
		} finally {
			await dispatcher.detach();
		}
	});
});
