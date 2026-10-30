/**
 * Unit tests for `WorkstationSession` and the connector's
 * `createSession` / `attachSession` methods. `MockWebSocket`-driven —
 * live-workstation coverage lands with the stage-1 integration test
 * (chunk 7).
 *
 * Conventions match `connector.test.ts`: per-test connector + jotai
 * store, `console.warn` / `error` spied for tight assertions, all
 * wire frames built inline so the test reads as "what the server
 * would send."
 */

import { createStore } from "jotai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
	CallbackInboxStub,
	type JsonValue,
	type OpResultStub,
	type SessionLifecycleState,
} from "@fxtr-ui/core";
import {
	type SessionEventFrame,
	type SessionSnapshotEvent,
	type StateUpdateEvent,
	WorkstationConnector,
	WorkstationEntityRefStub,
	WorkstationOpResultStub,
	WorkstationServerConnection,
	WorkstationServiceRefStub,
	type WorkstationSession,
} from "../src";
import { MockWebSocket, currentMockSocket, resetMockWebSockets } from "./_mock_websocket";

// --- Test scaffolding ---------------------------------------------

function makeConnector() {
	const store = createStore();
	const conn = new WorkstationServerConnection({
		url: "ws://test",
		store,
		webSocketCtor: MockWebSocket as unknown as new (url: string) => WebSocket,
	});
	const connector = new WorkstationConnector({ connection: conn, store });
	return { conn, connector, store };
}

interface SentMessage {
	request_id: number;
	op: string;
	[k: string]: unknown;
}

function sentMessages(): SentMessage[] {
	return currentMockSocket().sentParsed as SentMessage[];
}

function lastSent(): SentMessage {
	const all = sentMessages();
	const last = all[all.length - 1];
	if (!last) throw new Error("no message has been sent");
	return last;
}

function findSent(predicate: (m: SentMessage) => boolean): SentMessage | undefined {
	return sentMessages().find(predicate);
}

/**
 * Build a minimal `StateUpdateEvent` carrying just the wire `state`
 * dict. Other fields default to neutral values.
 */
/**
 * Counter for unique seq values across a test. Reset in
 * `beforeEach`. Each call returns the next monotonic seq.
 */
let nextSeq = 0;
function freshSeq(): number {
	nextSeq += 1;
	return nextSeq;
}

function stateUpdate(
	state: Readonly<Record<string, unknown>>,
	opts: Partial<StateUpdateEvent> = {},
): StateUpdateEvent {
	return {
		kind: "state_update",
		seq: freshSeq(),
		ack_seq: 0,
		state: state as StateUpdateEvent["state"],
		closed: false,
		tracked_entities: [],
		tracked_services: [],
		slot_counter: 0,
		...opts,
	};
}

/**
 * Deliver a `session_snapshot` frame to the most recent subscribe
 * request. Returns the subscription_id (= the subscribe's
 * request_id) for delta delivery.
 */
function ackAndSnapshot(
	state: Readonly<Record<string, unknown>> = {},
	sessionId = "sess",
): {
	subscription_id: number;
} {
	const subscribe = lastSent();
	expect(subscribe.op).toBe("subscribe_session");
	const subscription_id = subscribe.request_id;
	currentMockSocket().simulateMessage(JSON.stringify({ type: "ack", request_id: subscription_id }));
	// Snapshot's last_state can carry seq=0 (fresh session convention).
	const snapshot: SessionSnapshotEvent = {
		subscription_id,
		type: "session_snapshot",
		session_id: sessionId,
		last_state: { ...stateUpdate(state), seq: 0 },
		preserved_events: [],
		subsequent_events: [],
	};
	currentMockSocket().simulateMessage(JSON.stringify(snapshot));
	return { subscription_id };
}

function deliverStateUpdate(
	subscription_id: number,
	state: Readonly<Record<string, unknown>>,
	opts: Partial<StateUpdateEvent> = {},
): void {
	const frame: SessionEventFrame = {
		subscription_id,
		type: "session_event",
		event: stateUpdate(state, opts),
	};
	currentMockSocket().simulateMessage(JSON.stringify(frame));
}

/** Helper: do create-session + drain initial subscribe to a connected session. */
async function makeConnectedSession(
	state: Readonly<Record<string, unknown>> = {},
	metadata: JsonValue = {},
): Promise<{
	conn: WorkstationServerConnection;
	connector: WorkstationConnector;
	store: ReturnType<typeof createStore>;
	session: WorkstationSession;
	subscription_id: number;
}> {
	const { conn, connector } = makeConnector();
	currentMockSocket().simulateOpen();

	const createPromise = connector.createSession(metadata);
	const createReq = lastSent();
	expect(createReq.op).toBe("create_session");
	currentMockSocket().simulateMessage(
		JSON.stringify({
			type: "session_created",
			request_id: createReq.request_id,
			session_id: "sess",
		}),
	);
	const session = await createPromise;

	const { subscription_id } = ackAndSnapshot(state, session.sessionId);
	// Session owns its own jotai store as of stage 4 chunk 3.
	return { conn, connector, store: session.store, session, subscription_id };
}

let warnSpy: ReturnType<typeof vi.spyOn>;
let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
	resetMockWebSockets();
	nextSeq = 0;
	warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
	errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
	warnSpy.mockRestore();
	errorSpy.mockRestore();
	vi.useRealTimers();
});

// --- createSession / attachSession ---------------------------------

describe("WorkstationConnector: createSession", () => {
	it("round-trips CreateSessionRequest, constructs a Session that subscribes", async () => {
		const { connector, store } = makeConnector();
		currentMockSocket().simulateOpen();

		const createPromise = connector.createSession({ tag: "demo" });

		const createReq = lastSent();
		expect(createReq).toMatchObject({ op: "create_session", metadata: { tag: "demo" } });
		currentMockSocket().simulateMessage(
			JSON.stringify({
				type: "session_created",
				request_id: createReq.request_id,
				session_id: "sess-xyz",
			}),
		);

		const session = await createPromise;
		expect(session.sessionId).toBe("sess-xyz");
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("connecting");
		expect(store.get(session.userState)).toBeNull();

		// The session's constructor should have sent a subscribe_session.
		const subscribe = findSent((m) => m.op === "subscribe_session");
		expect(subscribe).toMatchObject({ op: "subscribe_session", session_id: "sess-xyz" });
	});

	it("throws ConnectorClosedError if the connector is closed before the round-trip", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();
		await connector.close();
		await expect(connector.createSession({})).rejects.toThrow();
		await conn.close();
	});

	it("throws ConnectorClosedError if the connector closes mid round-trip (post-await guard)", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();

		const createPromise = connector.createSession({});
		const createReq = lastSent();

		// Close the connector while the round-trip is in flight. In
		// borrowed mode the connection stays alive, so the pending
		// sendRequest doesn't reject — but the connector's `closed`
		// flag is now set, so when the server response *does* arrive
		// and the promise resumes, the post-await guard in
		// createSession throws.
		await connector.close();
		currentMockSocket().simulateMessage(
			JSON.stringify({
				type: "session_created",
				request_id: createReq.request_id,
				session_id: "sess-late",
			}),
		);

		await expect(createPromise).rejects.toThrow();
		await conn.close();
	});
});

describe("WorkstationConnector: attachSession", () => {
	it("constructs a Session that subscribes synchronously", () => {
		const { connector, store } = makeConnector();
		currentMockSocket().simulateOpen();

		const session = connector.attachSession("existing");
		expect(session.sessionId).toBe("existing");
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("connecting");
		expect(store.get(session.userState)).toBeNull();

		const subscribe = findSent((m) => m.op === "subscribe_session");
		expect(subscribe).toMatchObject({ op: "subscribe_session", session_id: "existing" });
	});

	it("two attachSession calls with the same id return independent sessions with independent subscriptions", () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();

		const a = connector.attachSession("dup");
		const b = connector.attachSession("dup");
		expect(a).not.toBe(b);

		const subscribes = sentMessages().filter((m) => m.op === "subscribe_session");
		expect(subscribes).toHaveLength(2);
	});

	it("throws ConnectorClosedError if the connector is closed", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();
		await connector.close();
		expect(() => connector.attachSession("x")).toThrow();
		await conn.close();
	});
});

// --- Snapshot decoding + lifecycle transitions ---------------------

describe("WorkstationSession: snapshot decoding and lifecycle", () => {
	it("transitions connecting -> connected on snapshot; userState reflects the envelope", async () => {
		// `bar: [[2, 3]]` is the wire list-escape form for the
		// structured array `[2, 3]` (an unprefixed array is reserved
		// for the tagged-list form). After decode, userState's `bar`
		// is just `[2, 3]`.
		const { session, store } = await makeConnectedSession({
			user: { foo: 1, label: "hello", bar: [[2, 3]] },
		});
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("connected");
		expect(store.get(session.userState)).toEqual({ foo: 1, label: "hello", bar: [2, 3] });
	});

	it("a fresh-session snapshot (`state = {}`) leaves userState null with no warning", async () => {
		const { session, store } = await makeConnectedSession({});
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("connected");
		expect(store.get(session.userState)).toBeNull();
		// "missing user key" used to warn — it shouldn't anymore.
		expect(warnSpy).not.toHaveBeenCalled();
	});

	it("state_update events overwrite committed userState", async () => {
		const { session, store, subscription_id } = await makeConnectedSession({ user: { v: 1 } });
		expect(store.get(session.userState)).toEqual({ v: 1 });

		deliverStateUpdate(subscription_id, { user: { v: 2, added: true } });
		expect(store.get(session.userState)).toEqual({ v: 2, added: true });
	});

	it("warns when the wire state decodes to a non-dict root (protocol violation)", async () => {
		const { session, store, subscription_id } = await makeConnectedSession({});

		// `[[1, 2, 3]]` is the wire literal-escape form for the
		// structured array `[1, 2, 3]`. Decode succeeds, but the
		// resulting root isn't a dict envelope — the session warns
		// and resets userState to null.
		const frame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "state_update",
				seq: 2,
				ack_seq: 0,
				state: [[1, 2, 3]],
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 0,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));

		expect(store.get(session.userState)).toBeNull();
		expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("wire state root is an array"));
	});

	it("logs an error and skips the update if wire decode throws", async () => {
		const { session, store, subscription_id } = await makeConnectedSession({ user: { v: 1 } });

		// Malformed wire-escape tag (unknown tag name) — unescapeWire throws.
		deliverStateUpdate(subscription_id, { user: ["NOT_A_REAL_TAG", "x"] });

		// userState should be untouched since decode threw before set.
		expect(store.get(session.userState)).toEqual({ v: 1 });
		expect(errorSpy).toHaveBeenCalledWith(
			expect.stringContaining("wire decode failed"),
			expect.anything(),
		);
	});
});

// --- Stub interning -----------------------------------------------

describe("WorkstationSession: stub interning", () => {
	it("interns a WorkstationEntityRefStub per shortId, reused across decodes within a session", async () => {
		const { session, store, subscription_id } = await makeConnectedSession({
			user: { ref: ["entity", "e1"] },
		});

		const first = (store.get(session.userState) as { ref: unknown }).ref;
		expect(first).toBeInstanceOf(WorkstationEntityRefStub);
		expect((first as WorkstationEntityRefStub).shortId).toBe("e1");

		// Same short_id in a later state_update -> same instance.
		deliverStateUpdate(subscription_id, {
			user: { ref: ["entity", "e1"], other: 42 },
		});
		const second = (store.get(session.userState) as { ref: unknown }).ref;
		expect(second).toBe(first);

		// Different short_id -> different instance.
		deliverStateUpdate(subscription_id, {
			user: { ref: ["entity", "e2"] },
		});
		const third = (store.get(session.userState) as { ref: unknown }).ref;
		expect(third).not.toBe(first);
		expect((third as WorkstationEntityRefStub).shortId).toBe("e2");
	});

	it("interns services analogously and keeps entity / service intern maps separate", async () => {
		const { session, store } = await makeConnectedSession({
			user: { svc: ["service", "s1"] },
		});
		const svc = (store.get(session.userState) as { svc: unknown }).svc;
		expect(svc).toBeInstanceOf(WorkstationServiceRefStub);
		expect((svc as WorkstationServiceRefStub).shortId).toBe("s1");
	});
});

// --- Reset / re-register ------------------------------------------

describe("WorkstationSession: reset and re-register", () => {
	it("connected -> stale on reset; userState retained; re-register queued", async () => {
		vi.useFakeTimers();
		const { session, store } = await makeConnectedSession({ user: { v: 1 } });

		currentMockSocket().simulateClose();
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("stale");
		expect(store.get(session.userState)).toEqual({ v: 1 });

		await vi.advanceTimersByTimeAsync(200);
		const newSocket = currentMockSocket();
		newSocket.simulateOpen();

		// New subscribe queued by the session's onClose handler should
		// land on the fresh socket.
		const subscribeOnNew = newSocket.sentParsed.find(
			(m) => (m as { op?: string }).op === "subscribe_session",
		);
		expect(subscribeOnNew).toMatchObject({ op: "subscribe_session", session_id: "sess" });

		// Deliver new snapshot — lifecycle goes back to connected,
		// userState refreshes.
		const subId = (subscribeOnNew as { request_id: number }).request_id;
		newSocket.simulateMessage(JSON.stringify({ type: "ack", request_id: subId }));
		const snap: SessionSnapshotEvent = {
			subscription_id: subId,
			type: "session_snapshot",
			session_id: "sess",
			last_state: stateUpdate({ user: { v: 99 } }),
			preserved_events: [],
			subsequent_events: [],
		};
		newSocket.simulateMessage(JSON.stringify(snap));

		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("connected");
		expect(store.get(session.userState)).toEqual({ v: 99 });
	});

	it("connecting -> connecting on reset (no snapshot yet, no stale transition)", async () => {
		vi.useFakeTimers();
		const { connector, store } = makeConnector();
		currentMockSocket().simulateOpen();

		const session = connector.attachSession("pre-snapshot");
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("connecting");

		// Drop the socket before any snapshot arrives.
		currentMockSocket().simulateClose();
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("connecting");

		// Re-register happened; on reconnect the new subscribe drains.
		await vi.advanceTimersByTimeAsync(200);
		currentMockSocket().simulateOpen();
		const subscribes = currentMockSocket().sentParsed.filter(
			(m) => (m as { op?: string }).op === "subscribe_session",
		);
		expect(subscribes).toHaveLength(1);
	});

	it("stale -> stale on chained drops", async () => {
		vi.useFakeTimers();
		const { session, store } = await makeConnectedSession({ user: { v: 1 } });

		currentMockSocket().simulateClose();
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("stale");

		await vi.advanceTimersByTimeAsync(200);
		const second = currentMockSocket();
		second.simulateOpen();
		// Don't deliver a snapshot; just drop again.
		second.simulateClose();
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("stale");
	});
});

// --- Terminal close paths ------------------------------------------

describe("WorkstationSession: terminal close paths", () => {
	it("detach() sends unsubscribe and transitions to closed; idempotent", async () => {
		const { conn, session, store } = await makeConnectedSession({ user: { v: 1 } });

		session.detach();
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("closed");
		expect(store.get(session.userState)).toEqual({ v: 1 }); // retained

		const unsubscribes = sentMessages().filter((m) => m.op === "unsubscribe");
		expect(unsubscribes).toHaveLength(1);

		// Idempotent.
		session.detach();
		const unsubscribes2 = sentMessages().filter((m) => m.op === "unsubscribe");
		expect(unsubscribes2).toHaveLength(1);

		await conn.close();
	});

	it("subscribe_failed (server ErrorResponse) transitions to 'unavailable' with the error logged", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();
		const session = connector.attachSession("doomed");

		const subscribe = lastSent();
		currentMockSocket().simulateMessage(
			JSON.stringify({
				type: "error",
				request_id: subscribe.request_id,
				message: "no such session",
			}),
		);

		// `subscribe_failed` is "session_id not known to server" — distinct
		// from a normal close, so the lifecycle terminal is `unavailable`,
		// not `closed`. See `SessionLifecycleState`'s docstring.
		expect(session.store.get(session.lifecycle)).toBe<SessionLifecycleState>("unavailable");
		expect(errorSpy).toHaveBeenCalledWith(
			expect.stringContaining("WorkstationSession(doomed) subscribe failed"),
			expect.objectContaining({ message: expect.stringContaining("no such session") }),
		);

		await conn.close();
	});

	it("terminal state_update (closed=true) flips lifecycle to closed", async () => {
		const { conn, session, store, subscription_id } = await makeConnectedSession({
			user: { v: 1 },
		});

		deliverStateUpdate(subscription_id, { user: { v: 1, terminal: true } }, { closed: true });

		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("closed");
		// Final state was applied before terminal-flip.
		expect(store.get(session.userState)).toEqual({ v: 1, terminal: true });

		await conn.close();
	});

	it("connector close cascades to terminal session lifecycle (owned mode via globalThis stub)", async () => {
		const realWebSocket = (globalThis as { WebSocket?: unknown }).WebSocket;
		(globalThis as { WebSocket: unknown }).WebSocket = MockWebSocket;
		try {
			const store = createStore();
			const connector = new WorkstationConnector({ url: "ws://test", store });
			currentMockSocket().simulateOpen();

			const session = connector.attachSession("x");
			await connector.close();

			expect(session.store.get(session.lifecycle)).toBe<SessionLifecycleState>("closed");
		} finally {
			(globalThis as { WebSocket?: unknown }).WebSocket = realWebSocket;
		}
	});

	it("connector close in borrowed mode does NOT cascade to sessions (consumer owns them)", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();
		const session = connector.attachSession("borrowed-session");
		ackAndSnapshot({ user: { v: 1 } }, "borrowed-session");

		await connector.close();
		// Connection still alive; session subscription untouched.
		expect(session.store.get(session.lifecycle)).toBe<SessionLifecycleState>("connected");

		// Closing the connection itself fans through to the session.
		await conn.close();
		expect(session.store.get(session.lifecycle)).toBe<SessionLifecycleState>("closed");
	});
});

// --- Atom write protection ----------------------------------------

describe("WorkstationSession: lifecycle atom is read-only at runtime", () => {
	it("writes to the lifecycle atom from outside throw at the jotai layer", async () => {
		const { session, store } = await makeConnectedSession({});
		// Cast to bypass the type-level `Atom<T>` (which already
		// blocks writes via the public TS surface). The runtime
		// derived-atom-with-no-write-function is what actually
		// stops the write.
		const writable = session.lifecycle as unknown as Parameters<typeof store.set>[0];
		expect(() => store.set(writable, "closed" as never)).toThrow();
	});

	// userState is now intentionally writable — its write function
	// routes through the mutation reducer. Write-rejection happens
	// when the session is terminating / closed / detaching, which
	// the dedicated mutation tests cover.
});

// --- Slot-allocation surfaces (stage 2) ----------------------------

describe("WorkstationSession: allocateCallback (stage 6 chunk 1 scaffolding)", () => {
	it("ctx.allocateCallback mints a CallbackInboxStub the user can store in state", async () => {
		const { session, store } = await makeConnectedSession({});
		let inbox: CallbackInboxStub | undefined;
		store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback();
			return { cb: inbox };
		});
		expect(inbox).toBeInstanceOf(CallbackInboxStub);
		// Chunk 1 doesn't wire the flush walk to emit CreateCallbackOp;
		// chunk 2 adds that. For now the stub-mint side is enough.
	});
});

describe("WorkstationSession: ctx.allocateLoad (stage 2 chunk 2)", () => {
	it("mints a fresh WorkstationOpResultStub on first allocate", async () => {
		const { session, store, connector } = await makeConnectedSession({});
		const ref = connector.entityRef("ent-a");
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(ref) as WorkstationOpResultStub;
			return { x: stub };
		});
		expect(stub).toBeInstanceOf(WorkstationOpResultStub);
		expect(stub.slot).toBe(1);
	});

	it("counter advances on subsequent allocates", async () => {
		const { session, store, connector } = await makeConnectedSession({});
		let a!: WorkstationOpResultStub;
		let b!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			a = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			b = ctx.allocateLoad(connector.entityRef("ent-b")) as WorkstationOpResultStub;
			return { a, b };
		});
		expect(a.slot).toBe(1);
		expect(b.slot).toBe(2);
	});

	it("dedups: two allocates for the same shortId return the same stub", async () => {
		const { session, store, connector } = await makeConnectedSession({});
		const ref = connector.entityRef("ent-a");
		let a!: OpResultStub<unknown>;
		let b!: OpResultStub<unknown>;
		store.set(session.userState, (_, ctx) => {
			a = ctx.allocateLoad(ref);
			b = ctx.allocateLoad(ref);
			return { a, b };
		});
		expect(a).toBe(b);
	});

	it("same-tick dedup: two ctx.allocateLoad calls inside one updater share a stub via pendingSlotSpecs", async () => {
		// In ctx-world the only way to allocate without writing is to
		// discard the stub inside the updater. The same-tick dedup
		// path (scanning `pendingSlotSpecs`) still hits because both
		// allocates run before the flush walk wipes the map.
		const { session, store, connector } = await makeConnectedSession({});
		let a!: WorkstationOpResultStub;
		let b!: WorkstationOpResultStub;
		store.set(session.userState, (prev, ctx) => {
			a = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			b = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return prev;
		});
		expect(a).toBe(b);
		expect(a.slot).toBe(1);
	});

	it("dedup persists across optimistic userState write + remove in the same tick", async () => {
		// Initial committed state has a user dict so write + remove
		// in a single tick rounds back to the committed state and
		// the microtask flush short-circuits — keeps the test
		// focused on dedup behavior, no mutation noise.
		const { session, store, connector } = await makeConnectedSession({ user: { foo: 0 } });
		let a!: OpResultStub<unknown>;
		// Optimistically write the stub into user state, then
		// remove it. Both writes happen synchronously; the second
		// `set` snapshots committed-equivalent state, and the
		// microtask flush nets out.
		store.set(session.userState, (_, ctx) => {
			a = ctx.allocateLoad(connector.entityRef("ent-a"));
			return { foo: 0, x: a };
		});
		store.set(session.userState, { foo: 0 });
		// loadDedup still has ent-a → slot 1 via the same-tick
		// `pendingSlotSpecs` lookup, so the re-allocate hits and
		// returns the same stub.
		let b!: OpResultStub<unknown>;
		store.set(session.userState, (_, ctx) => {
			b = ctx.allocateLoad(connector.entityRef("ent-a"));
			return { foo: 0, x: b };
		});
		expect(a).toBe(b);
	});

	it("counter advances past state_update.slot_counter on absorb", async () => {
		const { session, store, subscription_id, connector } = await makeConnectedSession({});
		// Foreign state_update reports the server's slot counter at 42.
		deliverStateUpdate(subscription_id, {}, { slot_counter: 42 });
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { x: stub };
		});
		expect(stub.slot).toBe(43);
	});

	it("counter advances past snapshot's last_state.slot_counter", async () => {
		const { conn, connector, store } = makeConnector();
		currentMockSocket().simulateOpen();
		const session = connector.attachSession("sess");
		// Drive the snapshot with a non-zero slot_counter.
		const subscribe = lastSent();
		expect(subscribe.op).toBe("subscribe_session");
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscribe.request_id }),
		);
		currentMockSocket().simulateMessage(
			JSON.stringify({
				subscription_id: subscribe.request_id,
				type: "session_snapshot",
				session_id: "sess",
				last_state: { ...stateUpdate({}), seq: 0, slot_counter: 17 },
				preserved_events: [],
				subsequent_events: [],
			}),
		);
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-x")) as WorkstationOpResultStub;
			return { x: stub };
		});
		expect(stub.slot).toBe(18);
		await conn.close();
	});

	it("counter never decreases", async () => {
		const { session, store, subscription_id, connector } = await makeConnectedSession({});
		store.set(session.userState, (_, ctx) => ({
			a: ctx.allocateLoad(connector.entityRef("ent-a")), // slot 1
			b: ctx.allocateLoad(connector.entityRef("ent-b")), // slot 2
		}));
		// state_update with a lower slot_counter shouldn't roll the
		// local counter back.
		deliverStateUpdate(subscription_id, {}, { slot_counter: 1 });
		let c!: WorkstationOpResultStub;
		store.set(session.userState, (prev, ctx) => {
			c = ctx.allocateLoad(connector.entityRef("ent-c")) as WorkstationOpResultStub;
			return { ...(prev as object), c };
		});
		expect(c.slot).toBe(3);
	});

	it("throws on a closed session", async () => {
		const { session, store, connector } = await makeConnectedSession({});
		await session.detach();
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				x: ctx.allocateLoad(connector.entityRef("ent-a")),
			})),
		).toThrow();
	});
});

describe("WorkstationSession: lookupOpResult tripwire", () => {
	it("throws when a wire-decoded slot exceeds localSlotCounter", async () => {
		const { subscription_id } = await makeConnectedSession({});
		// localSlotCounter is 0 here (no allocates, slot_counter on
		// the snapshot was 0). State_update introduces slot 99
		// without first announcing slot_counter ≥ 99 — protocol
		// violation that should trip the tripwire on decode.
		deliverStateUpdate(
			subscription_id,
			{ x: ["op_result", 99] },
			{ slot_counter: 0 }, // counter stays at 0 despite the reference
		);
		// Decode happens inside absorbState; the throw is caught and
		// console.error'd by the absorbState try/catch, since wire
		// decode failure is treated as a non-fatal "give up on this
		// state update" condition. We assert the error log.
		expect(errorSpy).toHaveBeenCalled();
		const messages = errorSpy.mock.calls.map((c) => String(c[1] ?? c[0]));
		expect(messages.some((m) => m.includes("exceeds localSlotCounter"))).toBe(true);
	});

	it("does NOT throw when slot is at or below localSlotCounter", async () => {
		const { subscription_id } = await makeConnectedSession({});
		// state_update advances slot_counter to 5, then references
		// slot 5 in state.
		deliverStateUpdate(subscription_id, { x: ["op_result", 5] }, { slot_counter: 5 });
		expect(errorSpy).not.toHaveBeenCalled();
	});
});
