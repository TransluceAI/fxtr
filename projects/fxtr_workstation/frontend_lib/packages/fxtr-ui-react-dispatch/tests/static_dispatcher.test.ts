/**
 * Smoke tests for `createStaticDispatcher` — the lifecycle
 * surface (`status` atom, `detach()`) and `getEntry` caching /
 * post-detach rejection.
 *
 * State-machine behavior is covered by the static-helper tests
 * (`static_entity_registry.test.ts` /
 * `static_service_registry.test.ts`); React-render-side
 * behavior is in
 * `v2/ts/packages/fxtr-ui-react-dispatch-tests/tests/render_dispatch.test.tsx`.
 * This file's job is the dispatcher's own machinery.
 */

import {
	type EntityRefStub,
	type FxtrSessionAtom,
	type FxtrSessionUpdateCtx,
	type FxtrUpdate,
	NotImplementedError,
	type Session,
	type SessionLifecycleState,
	type UserState,
	type UserStateWriteOptions,
} from "@fxtr-ui/core";
import { type PrimitiveAtom, atom, createStore } from "jotai";
import { describe, expect, it } from "vitest";

import { DispatcherClosedError } from "../src/dispatcher";
import type { EntityKeyFallbacks } from "../src/fallbacks";
import { declareEntityDispatchKey } from "../src/keys";
import type { DispatchKeyRegistryEntry } from "../src/registry_types";
import { createStaticDispatcher } from "../src/static_dispatcher";
import { createDispatchRegistry, entityEntry } from "../src/static_registry";
import { TestEntityRefStub } from "./_mocks";

interface MockSessionHandle {
	readonly session: Session;
	readonly lifecyclePrivate: PrimitiveAtom<SessionLifecycleState>;
	/** Counter for `session.detach()` calls — for spy assertions. */
	readonly detachCalls: { count: number };
}

/**
 * Minimal `Session` mock with a writable `lifecycle` and a
 * call-counting `detach()`. Sufficient for the dispatcher's
 * dependencies — the dispatcher itself doesn't allocate slots,
 * so the userState's update ctx isn't exercised here.
 */
function makeMockSession(): MockSessionHandle {
	const store = createStore();
	const lifecyclePrivate = atom<SessionLifecycleState>("connecting");
	const userStatePrivate = atom<UserState | null>(null);
	const detachCalls = { count: 0 };

	// biome-ignore lint/style/useConst: closure-capture forward declaration
	let sessionRef: Session;
	const ctx: FxtrSessionUpdateCtx = {
		allocateLoad: () => {
			throw new NotImplementedError("mock session: allocateLoad not wired");
		},
		allocateInvoke: () => {
			throw new NotImplementedError("mock session: allocateInvoke not wired");
		},
		allocateGetEntityMetadata: () => {
			throw new NotImplementedError("mock session: allocateGetEntityMetadata not wired");
		},
		allocateGetServiceMetadata: () => {
			throw new NotImplementedError("mock session: allocateGetServiceMetadata not wired");
		},
		allocateCallback: () => {
			throw new NotImplementedError("mock session: allocateCallback not wired");
		},
		get session() {
			return sessionRef;
		},
	};

	const userState: FxtrSessionAtom<UserState | null> = atom(
		(get) => get(userStatePrivate),
		(get, set, update: FxtrUpdate<UserState | null>, _options?: UserStateWriteOptions) => {
			const prev = get(userStatePrivate);
			const next =
				typeof update === "function"
					? (update as (p: UserState | null, c: FxtrSessionUpdateCtx) => UserState | null)(
							prev,
							ctx,
						)
					: update;
			set(userStatePrivate, next);
		},
	);

	const session: Session = {
		sessionId: "mock-session-id",
		store,
		lifecycle: atom((get) => get(lifecyclePrivate)),
		userState,
		detach: async () => {
			detachCalls.count += 1;
		},
		terminate: async () => {},
	};
	sessionRef = session;

	return { session, lifecyclePrivate, detachCalls };
}

// Trivial registry entry — an empty effect-fn body, no state
// transitions. Sufficient for testing the dispatcher's
// machinery without driving a state machine. Fallbacks live on
// the key now, so the entry shape is minimal.
function makeEmptyEntry(): DispatchKeyRegistryEntry<EntityRefStub, object, null> {
	return {
		initialState: null,
		effectFn: () => () => {},
	};
}

// Trivial fallback bundle for the test key. Identity doesn't
// matter to these tests — they exercise getEntry caching and
// detach() behaviour, not the fallback resolution path.
const testKeyFallbacks: EntityKeyFallbacks<object> = {
	Loading: () => null,
	NoMatch: () => null,
	Error: () => null,
};

const TestKey = declareEntityDispatchKey({
	uuid: "static-dispatcher-test-key",
	description: "StaticDispatcherTestKey",
	fallbacks: testKeyFallbacks,
});

describe("createStaticDispatcher — getEntry caching", () => {
	it("returns the same entry for repeat (key, stub) lookups", () => {
		const { session } = makeMockSession();
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(TestKey, makeEmptyEntry())],
			serviceEntries: [],
		});
		const dispatcher = createStaticDispatcher({ session, registry });
		const stub = new TestEntityRefStub("e-1");

		const e1 = dispatcher.getEntry(TestKey, stub);
		const e2 = dispatcher.getEntry(TestKey, stub);
		expect(e1).toBe(e2);
		expect(e1.id).toBe(`${TestKey.uuid}::e-1`);
	});

	it("returns distinct entries for distinct stubs", () => {
		const { session } = makeMockSession();
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(TestKey, makeEmptyEntry())],
			serviceEntries: [],
		});
		const dispatcher = createStaticDispatcher({ session, registry });

		const a = dispatcher.getEntry(TestKey, new TestEntityRefStub("e-a"));
		const b = dispatcher.getEntry(TestKey, new TestEntityRefStub("e-b"));
		expect(a).not.toBe(b);
		expect(a.id).toBe(`${TestKey.uuid}::e-a`);
		expect(b.id).toBe(`${TestKey.uuid}::e-b`);
	});
});

describe("createStaticDispatcher — status atom", () => {
	it("mirrors session.lifecycle until detach()", () => {
		const { session, lifecyclePrivate } = makeMockSession();
		const registry = createDispatchRegistry({ entityEntries: [], serviceEntries: [] });
		const dispatcher = createStaticDispatcher({ session, registry });

		expect(session.store.get(dispatcher.status)).toBe("connecting");
		session.store.set(lifecyclePrivate, "connected");
		expect(session.store.get(dispatcher.status)).toBe("connected");
		session.store.set(lifecyclePrivate, "stale");
		expect(session.store.get(dispatcher.status)).toBe("stale");
		session.store.set(lifecyclePrivate, "unavailable");
		expect(session.store.get(dispatcher.status)).toBe("unavailable");
	});

	it('reports "closed" after detach() regardless of underlying lifecycle', async () => {
		const { session, lifecyclePrivate } = makeMockSession();
		const registry = createDispatchRegistry({ entityEntries: [], serviceEntries: [] });
		const dispatcher = createStaticDispatcher({ session, registry });

		session.store.set(lifecyclePrivate, "connected");
		expect(session.store.get(dispatcher.status)).toBe("connected");

		await dispatcher.detach();
		expect(session.store.get(dispatcher.status)).toBe("closed");

		// Even if the session's lifecycle flips back, dispatcher
		// stays closed.
		session.store.set(lifecyclePrivate, "connected");
		expect(session.store.get(dispatcher.status)).toBe("closed");
	});
});

describe("createStaticDispatcher — detach", () => {
	it("calls session.detach() and flips status to closed", async () => {
		const { session, detachCalls } = makeMockSession();
		const registry = createDispatchRegistry({ entityEntries: [], serviceEntries: [] });
		const dispatcher = createStaticDispatcher({ session, registry });

		expect(detachCalls.count).toBe(0);
		await dispatcher.detach();
		expect(detachCalls.count).toBe(1);
		expect(session.store.get(dispatcher.status)).toBe("closed");
	});

	it("is idempotent — repeated calls share the in-flight promise", async () => {
		const { session, detachCalls } = makeMockSession();
		const registry = createDispatchRegistry({ entityEntries: [], serviceEntries: [] });
		const dispatcher = createStaticDispatcher({ session, registry });

		const p1 = dispatcher.detach();
		const p2 = dispatcher.detach();
		expect(p1).toBe(p2);
		await Promise.all([p1, p2]);
		expect(detachCalls.count).toBe(1);

		// Subsequent call after the first resolves: still
		// returns the same resolved promise, still no second
		// session.detach() call.
		await dispatcher.detach();
		expect(detachCalls.count).toBe(1);
	});

	it("getEntry throws DispatcherClosedError after detach()", async () => {
		const { session } = makeMockSession();
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(TestKey, makeEmptyEntry())],
			serviceEntries: [],
		});
		const dispatcher = createStaticDispatcher({ session, registry });

		await dispatcher.detach();
		expect(() => dispatcher.getEntry(TestKey, new TestEntityRefStub("e-post-detach"))).toThrow(
			DispatcherClosedError,
		);
	});

	it("drops the entry cache so post-detach getEntry would re-mint (verified via DispatcherClosedError gate)", async () => {
		// We can't directly inspect the internal cache, but the
		// "throws on post-detach getEntry" assertion above proves
		// the closed flag gates lookups. The cache-clear is a GC
		// nicety we don't observe directly.
		const { session } = makeMockSession();
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(TestKey, makeEmptyEntry())],
			serviceEntries: [],
		});
		const dispatcher = createStaticDispatcher({ session, registry });

		const stub = new TestEntityRefStub("e-pre-detach");
		const before = dispatcher.getEntry(TestKey, stub);
		expect(before).toBeDefined();
		await dispatcher.detach();
		expect(() => dispatcher.getEntry(TestKey, stub)).toThrow(DispatcherClosedError);
	});
});

describe("createStaticDispatcher — unregistered keys", () => {
	it("renders NoMatchFallback for a key with no registry entry (no throw)", () => {
		const { session } = makeMockSession();
		// Registry has no entry for TestKey — under the old API
		// this threw `UnregisteredDispatchKeyError`. Under the new
		// API the dispatcher renders the key's NoMatchFallback.
		const NoMatch = (() => null) as unknown as EntityKeyFallbacks<object>["NoMatch"];
		const KeyWithFallback = declareEntityDispatchKey({
			uuid: "key-with-fallback",
			description: "KeyWithFallback",
			fallbacks: {
				Loading: () => null,
				NoMatch,
				Error: () => null,
			},
		});
		const registry = createDispatchRegistry({ entityEntries: [], serviceEntries: [] });
		const dispatcher = createStaticDispatcher({ session, registry });
		const stub = new TestEntityRefStub("e-unknown");
		const entry = dispatcher.getEntry(KeyWithFallback, stub);
		// Subscribe to mount the no-op effect, then read the
		// component. After microtask drain the effect has run and
		// componentAtom holds NoMatch.
		const unsub = session.store.sub(entry.componentAtom, () => {});
		try {
			// jotai-effect schedules via microtask; drain.
			return Promise.resolve()
				.then(() => Promise.resolve())
				.then(() => {
					expect(session.store.get(entry.componentAtom)).toBe(NoMatch);
				});
		} finally {
			unsub();
		}
	});
});
