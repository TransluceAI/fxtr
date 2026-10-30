/**
 * Unit tests for chunk 6's mutation reducer: writable userState,
 * microtask coalescing, optimistic + rollback, terminate, drain
 * detach, connector-level stub interning. `MockWebSocket`-driven;
 * live-workstation coverage lands in chunk 7.
 */

import { createStore } from "jotai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RollbackInfo, SessionLifecycleState } from "@fxtr-ui/core";
import {
	type MutateSessionRequest,
	type MutationOp,
	type MutationRejectReason,
	type SessionEventFrame,
	type SessionSnapshotEvent,
	type StateUpdateEvent,
	WorkstationConnector,
	type WorkstationEntityRefStub,
	WorkstationServerConnection,
	type WorkstationSession,
} from "../src";
import { MockWebSocket, currentMockSocket, resetMockWebSockets } from "./_mock_websocket";

// --- Test scaffolding ---------------------------------------------

let nextSeq = 0;
function freshSeq(): number {
	nextSeq += 1;
	return nextSeq;
}

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

function findLastMutation(): MutateSessionRequest & { request_id: number } {
	const mutations = sentMessages().filter((m) => m.op === "mutate_session");
	const last = mutations[mutations.length - 1];
	if (!last) throw new Error("no mutation has been sent");
	return last as unknown as MutateSessionRequest & { request_id: number };
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

function ackAndSnapshot(
	state: Readonly<Record<string, unknown>> = {},
	sessionId = "sess",
): { subscription_id: number } {
	const subscribe = lastSent();
	expect(subscribe.op).toBe("subscribe_session");
	const subscription_id = subscribe.request_id;
	currentMockSocket().simulateMessage(JSON.stringify({ type: "ack", request_id: subscription_id }));
	const snapshot: SessionSnapshotEvent = {
		subscription_id,
		type: "session_snapshot",
		session_id: sessionId,
		// Fresh-session convention: seq=0.
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

function commitMutation(
	requestId: number,
	subscriptionId: number,
	updateId: number,
	seq?: number,
): void {
	currentMockSocket().simulateMessage(
		JSON.stringify({
			type: "mutation_committed",
			request_id: requestId,
			subscription_id: subscriptionId,
			seq: seq ?? freshSeq(),
			update_id: updateId,
		}),
	);
}

function rejectMutation(requestId: number, reason: MutationRejectReason, message = "rej"): void {
	currentMockSocket().simulateMessage(
		JSON.stringify({
			type: "mutation_rejected",
			request_id: requestId,
			reason,
			message,
		}),
	);
}

async function setupConnectedSession(initial: Readonly<Record<string, unknown>> = {}): Promise<{
	conn: WorkstationServerConnection;
	connector: WorkstationConnector;
	store: ReturnType<typeof createStore>;
	session: WorkstationSession;
	subscription_id: number;
}> {
	const { conn, connector } = makeConnector();
	currentMockSocket().simulateOpen();

	const createPromise = connector.createSession({});
	const createReq = lastSent();
	currentMockSocket().simulateMessage(
		JSON.stringify({
			type: "session_created",
			request_id: createReq.request_id,
			session_id: "sess",
		}),
	);
	const session = await createPromise;
	const { subscription_id } = ackAndSnapshot(initial, session.sessionId);
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

// --- Happy-path mutation --------------------------------------------

describe("WorkstationSession: happy-path mutation", () => {
	it("writes flow through the microtask flush and ship a MutateSessionRequest", async () => {
		const { store, session, subscription_id } = await setupConnectedSession();
		store.set(session.userState, { v: 1 });

		// Optimistic read: visible before the microtask flushes.
		expect(store.get(session.userState)).toEqual({ v: 1 });

		// Microtask flush.
		await Promise.resolve();
		const mutation = findLastMutation();
		expect(mutation).toMatchObject({
			op: "mutate_session",
			subscription_id,
			ack_seq: 0,
			update_id: 1,
		});
		expect(mutation.patch).toEqual([{ op: "add", path: "/user", value: { v: 1 } }]);
		expect(mutation.ops).toEqual([]);
	});

	it("on commit, promotes inflight to committed and clears inflight bookkeeping", async () => {
		const { store, session, subscription_id } = await setupConnectedSession();
		store.set(session.userState, { v: 1 });
		await Promise.resolve();

		const mutation = findLastMutation();
		commitMutation(mutation.request_id, subscription_id, mutation.update_id);

		// userState still reads as { v: 1 } (now committed).
		expect(store.get(session.userState)).toEqual({ v: 1 });

		// A subsequent write with no further changes should produce
		// no mutation (empty diff short-circuit).
		store.set(session.userState, { v: 1 });
		await Promise.resolve();
		const mutations = sentMessages().filter((m) => m.op === "mutate_session");
		expect(mutations).toHaveLength(1);
	});
});

// --- Microtask coalescing ------------------------------------------

describe("WorkstationSession: microtask coalescing", () => {
	it("multiple writes in one tick fold into a single mutation", async () => {
		const { store, session } = await setupConnectedSession();
		store.set(session.userState, { a: 1 });
		store.set(session.userState, { a: 1, b: 2 });
		store.set(session.userState, { a: 1, b: 2, c: 3 });
		await Promise.resolve();

		const mutations = sentMessages().filter((m) => m.op === "mutate_session");
		expect(mutations).toHaveLength(1);
		const mutation = mutations[0] as unknown as MutateSessionRequest;
		expect(mutation.patch).toEqual([{ op: "add", path: "/user", value: { a: 1, b: 2, c: 3 } }]);
	});

	it("updater form receives prev value and produces fold-correctly", async () => {
		const { store, session, subscription_id } = await setupConnectedSession({ user: { n: 1 } });
		store.set(session.userState, (prev) => ({ ...(prev as { n: number }), n: 2 }));
		await Promise.resolve();
		const mutation = findLastMutation();
		expect(mutation.patch).toEqual([{ op: "replace", path: "/user/n", value: 2 }]);
		commitMutation(mutation.request_id, subscription_id, mutation.update_id);
		expect(store.get(session.userState)).toEqual({ n: 2 });
	});
});

// --- Pending while inflight ----------------------------------------

describe("WorkstationSession: pending writes while in flight", () => {
	it("queues additional writes until the in-flight commits, then sends a new mutation", async () => {
		const { store, session, subscription_id } = await setupConnectedSession();
		store.set(session.userState, { v: 1 });
		await Promise.resolve();

		// Second write while in flight.
		store.set(session.userState, { v: 1, more: true });
		await Promise.resolve();
		// Only one mutation so far — the second write is queued.
		let mutations = sentMessages().filter((m) => m.op === "mutate_session");
		expect(mutations).toHaveLength(1);

		// First mutation commits; second flush kicks off (after the
		// commit handler microtask runs, then the rescheduled flush
		// microtask runs).
		const first = mutations[0] as unknown as MutateSessionRequest & { request_id: number };
		commitMutation(first.request_id, subscription_id, first.update_id);
		// Two microtask yields: one for the response handler, one for
		// the rescheduled flush.
		await Promise.resolve();
		await Promise.resolve();

		mutations = sentMessages().filter((m) => m.op === "mutate_session");
		expect(mutations).toHaveLength(2);
		const second = mutations[1] as unknown as MutateSessionRequest;
		expect(second.patch).toContainEqual({ op: "add", path: "/user/more", value: true });
	});
});

// --- Reject + rollback ---------------------------------------------

describe("WorkstationSession: reject and rollback", () => {
	it("rejection reverts localAtom to committed and fires onRollback with RollbackInfo", async () => {
		const { store, session, subscription_id } = await setupConnectedSession({ user: { v: 1 } });
		const rollbackInfos: RollbackInfo[] = [];
		store.set(session.userState, { v: 2 }, { onRollback: (info) => rollbackInfos.push(info) });
		await Promise.resolve();
		expect(store.get(session.userState)).toEqual({ v: 2 }); // optimistic

		const mutation = findLastMutation();
		rejectMutation(mutation.request_id, "conflict", "merge conflict");
		await Promise.resolve(); // response handler microtask

		expect(store.get(session.userState)).toEqual({ v: 1 }); // reverted
		expect(rollbackInfos).toHaveLength(1);
		expect(rollbackInfos[0]).toEqual({
			kind: "retryable",
			message: "merge conflict",
		});
		expect(subscription_id).toBeGreaterThan(0); // sanity
	});

	it("rollbacks fire in forward (oldest-first) registration order across coalesced writes", async () => {
		const { store, session } = await setupConnectedSession({ user: { x: 1 } });
		const order: string[] = [];
		store.set(session.userState, { x: 2 }, { onRollback: () => order.push("a") });
		store.set(session.userState, { x: 3 }, { onRollback: () => order.push("b") });
		store.set(session.userState, { x: 4 }, { onRollback: () => order.push("c") });
		await Promise.resolve();
		const mutation = findLastMutation();
		rejectMutation(mutation.request_id, "stale_ack");
		await Promise.resolve();
		expect(order).toEqual(["a", "b", "c"]);
	});

	it("session_gone rejection also marks lifecycle closed", async () => {
		const { store, session } = await setupConnectedSession({ user: { v: 1 } });
		store.set(session.userState, { v: 2 });
		await Promise.resolve();
		const mutation = findLastMutation();
		rejectMutation(mutation.request_id, "session_gone");
		await Promise.resolve();
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("closed");
	});
});

// --- Connection-lost rollback --------------------------------------

describe("WorkstationSession: connection-lost rollback", () => {
	it("socket drop while inflight rolls back the optimistic view and fires onRollback", async () => {
		vi.useFakeTimers();
		const { store, session } = await setupConnectedSession({ user: { v: 1 } });
		const infos: RollbackInfo[] = [];
		store.set(session.userState, { v: 99 }, { onRollback: (i) => infos.push(i) });
		await Promise.resolve();
		// In flight. Drop the socket.
		currentMockSocket().simulateClose();
		// Pending sendRequest rejects with ConnectionLostError; our
		// .catch handler runs the rollback.
		await vi.advanceTimersByTimeAsync(0);

		expect(store.get(session.userState)).toEqual({ v: 1 });
		expect(infos).toHaveLength(1);
		expect(infos[0]?.kind).toBe("retryable");
	});
});

// --- Foreign update integration ------------------------------------

describe("WorkstationSession: foreign update integration", () => {
	it("foreign state_update with no local edits snaps committed and local", async () => {
		const { store, session, subscription_id } = await setupConnectedSession({ user: { v: 1 } });
		deliverStateUpdate(subscription_id, { user: { v: 2 } });
		expect(store.get(session.userState)).toEqual({ v: 2 });
	});

	it("foreign state_update while local-dirty leaves localAtom alone (user keeps their edits)", async () => {
		const { store, session, subscription_id } = await setupConnectedSession({ user: { v: 1 } });
		store.set(session.userState, { v: 100 });
		await Promise.resolve();
		// In flight. A foreign update arrives.
		deliverStateUpdate(subscription_id, { user: { v: 2, foreign: true } });
		// userState still shows the optimistic local edit.
		expect(store.get(session.userState)).toEqual({ v: 100 });
		// When our mutation is rejected with stale_ack, the rollback
		// reverts to the new committed (which has the foreign edit).
		const mutation = findLastMutation();
		rejectMutation(mutation.request_id, "stale_ack");
		await Promise.resolve();
		expect(store.get(session.userState)).toEqual({ v: 2, foreign: true });
	});
});

// --- Track/Untrack ops --------------------------------------------

describe("WorkstationSession: Track/Untrack ops", () => {
	it("adding an entity ref to state emits TrackEntityOp", async () => {
		const { store, connector, session } = await setupConnectedSession();
		const ref = connector.entityRef("a");
		store.set(session.userState, { thing: ref });
		await Promise.resolve();
		const mutation = findLastMutation();
		expect(mutation.ops).toContainEqual<MutationOp>({ kind: "track_entity", short_id: "a" });
	});

	it("removing an entity ref emits UntrackEntityOp", async () => {
		const { store, connector, session, subscription_id } = await setupConnectedSession();
		const ref = connector.entityRef("a");
		store.set(session.userState, { thing: ref });
		await Promise.resolve();
		let mutation = findLastMutation();
		commitMutation(mutation.request_id, subscription_id, mutation.update_id);

		store.set(session.userState, {});
		await Promise.resolve();
		mutation = findLastMutation();
		expect(mutation.ops).toContainEqual<MutationOp>({ kind: "untrack_entity", short_id: "a" });
	});

	it("services use track_service / untrack_service", async () => {
		const { store, connector, session } = await setupConnectedSession();
		store.set(session.userState, { svc: connector.serviceRef("s") });
		await Promise.resolve();
		const mutation = findLastMutation();
		expect(mutation.ops).toContainEqual<MutationOp>({ kind: "track_service", short_id: "s" });
	});

	it("adding and removing the same ref in one coalesced batch nets to no op", async () => {
		// Initial state already has a non-empty `user` envelope so
		// the patch path doesn't need to materialize `/user`. With
		// committed.user = {} and a round-tripped ref write, the
		// diff is empty and ops cancel — no mutation should be
		// shipped.
		const { store, connector, session } = await setupConnectedSession({ user: {} });
		const ref = connector.entityRef("a");
		store.set(session.userState, { thing: ref });
		store.set(session.userState, {});
		await Promise.resolve();
		const mutations = sentMessages().filter((m) => m.op === "mutate_session");
		expect(mutations).toHaveLength(0);
	});
});

// --- Connector-level stub interning -------------------------------

describe("WorkstationConnector: entityRef / serviceRef interning", () => {
	it("entityRef returns the same instance for the same shortId", () => {
		const { connector } = makeConnector();
		expect(connector.entityRef("a")).toBe(connector.entityRef("a"));
		expect(connector.entityRef("a")).not.toBe(connector.entityRef("b"));
	});

	it("serviceRef is interned independently of entityRef", () => {
		const { connector } = makeConnector();
		expect(connector.serviceRef("a")).toBe(connector.serviceRef("a"));
		expect(connector.entityRef("a") as unknown).not.toBe(connector.serviceRef("a"));
	});

	it("wire-decoded refs and programmatically-minted refs for the same shortId collapse", async () => {
		const { connector, session, store, subscription_id } = await setupConnectedSession();
		// Foreign update embeds an entity ref.
		deliverStateUpdate(subscription_id, { user: { thing: ["entity", "wire-id"] } });
		const fromState = (store.get(session.userState) as { thing: WorkstationEntityRefStub }).thing;
		expect(fromState).toBe(connector.entityRef("wire-id"));
	});
});

// --- terminate() ---------------------------------------------------

describe("WorkstationSession: terminate", () => {
	it("terminate() ships a CloseOp; commit transitions lifecycle to closed and resolves the promise", async () => {
		const { store, session, subscription_id } = await setupConnectedSession({ user: { v: 1 } });
		const termPromise = session.terminate();
		await Promise.resolve();
		const mutation = findLastMutation();
		expect(mutation.ops).toContainEqual<MutationOp>({ kind: "close" });
		// Patch should be empty (no user state change, just terminate).
		expect(mutation.patch).toEqual([]);
		commitMutation(mutation.request_id, subscription_id, mutation.update_id);
		await termPromise;
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("closed");
	});

	it("terminate(finalState) writes the parting state alongside the CloseOp", async () => {
		const { session, subscription_id } = await setupConnectedSession({ user: { v: 1 } });
		const termPromise = session.terminate({ v: 2, note: "bye" });
		await Promise.resolve();
		const mutation = findLastMutation();
		expect(mutation.ops).toContainEqual<MutationOp>({ kind: "close" });
		expect(mutation.patch).toContainEqual({ op: "replace", path: "/user/v", value: 2 });
		expect(mutation.patch).toContainEqual({ op: "add", path: "/user/note", value: "bye" });
		commitMutation(mutation.request_id, subscription_id, mutation.update_id);
		await termPromise;
	});

	it("writes after terminate() throw", async () => {
		const { store, session } = await setupConnectedSession({ user: { v: 1 } });
		const termPromise = session.terminate();
		// Attach a catch so the eventual rejection isn't unhandled
		// (we'll let this test leave the terminate hanging without
		// committing; the connector-close in afterEach will reject it).
		termPromise.catch(() => {});
		expect(() => store.set(session.userState, { v: 999 })).toThrow();
	});

	it("terminate() rejection keeps session alive, lifecycle stays connected, future writes work", async () => {
		const { store, session } = await setupConnectedSession({ user: { v: 1 } });
		const infos: RollbackInfo[] = [];
		const termPromise = session.terminate(undefined, { onRollback: (i) => infos.push(i) });
		await Promise.resolve();
		const mutation = findLastMutation();
		rejectMutation(mutation.request_id, "validation_failed", "no can do");
		// Promise rejects with MutationRolledBackError carrying the info.
		await expect(termPromise).rejects.toMatchObject({
			name: "MutationRolledBackError",
			info: { kind: "permanent", message: "no can do" },
		});
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("connected");
		expect(infos).toHaveLength(1);
		expect(infos[0]).toEqual({ kind: "permanent", message: "no can do" });
		// Local terminated flag cleared (since rollback reverts to committed).
		// User can write again.
		store.set(session.userState, { v: 2 });
	});

	it("calling terminate() while a terminate is in progress rejects the second call", async () => {
		const { session } = await setupConnectedSession();
		const first = session.terminate();
		first.catch(() => {});
		await expect(session.terminate()).rejects.toThrow(/terminating session/);
	});
});

// --- detach() drain ------------------------------------------------

describe("WorkstationSession: detach with drain", () => {
	it("detach() waits for in-flight mutation to commit before unsubscribing", async () => {
		const { conn, store, session, subscription_id } = await setupConnectedSession();
		store.set(session.userState, { v: 1 });
		await Promise.resolve();
		const mutation = findLastMutation();

		const detachPromise = session.detach();
		// Detach is still pending — mutation hasn't committed.
		let resolved = false;
		detachPromise.then(() => {
			resolved = true;
		});
		await Promise.resolve();
		expect(resolved).toBe(false);

		// Mutation commits. Detach should now complete.
		commitMutation(mutation.request_id, subscription_id, mutation.update_id);
		await detachPromise;

		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("closed");
		// Unsubscribe was sent.
		const unsubs = sentMessages().filter((m) => m.op === "unsubscribe");
		expect(unsubs).toHaveLength(1);

		await conn.close();
	});

	it("detach() with no in-flight or pending writes resolves immediately", async () => {
		const { conn, store, session } = await setupConnectedSession();
		await session.detach();
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("closed");
		await conn.close();
	});

	it("writes after detach() throw", async () => {
		const { conn, store, session } = await setupConnectedSession();
		const detachPromise = session.detach();
		expect(() => store.set(session.userState, { v: 1 })).toThrow();
		await detachPromise;
		await conn.close();
	});

	it("detach() drains both inflight and post-commit re-flush", async () => {
		const { conn, store, session, subscription_id } = await setupConnectedSession();
		// First write -> flush -> inflight.
		store.set(session.userState, { v: 1 });
		await Promise.resolve();
		const m1 = findLastMutation();

		// Second write while inflight; will be re-flushed after first commits.
		store.set(session.userState, { v: 1, more: true });

		// Start detach. It should drain both. We need to interleave
		// commit responses with the detach's await loop. Promise.race
		// against a "did detach resolve?" probe lets us advance.

		// Commit first mutation. Detach is awaiting it.
		commitMutation(m1.request_id, subscription_id, m1.update_id);

		// Now start detach — at this point the response handler has
		// queued, but hasn't run. We need to yield enough microtasks
		// for: response handler → re-flush schedule → re-flush runs
		// → second mutation sent.
		const detachPromise = session.detach();
		await Promise.resolve();
		await Promise.resolve();
		await Promise.resolve();
		const allMutations = sentMessages().filter((m) => m.op === "mutate_session");
		expect(allMutations.length).toBeGreaterThanOrEqual(2);
		const m2 = allMutations[1] as unknown as MutateSessionRequest & { request_id: number };
		commitMutation(m2.request_id, subscription_id, m2.update_id);

		await detachPromise;
		expect(store.get(session.lifecycle)).toBe<SessionLifecycleState>("closed");
		await conn.close();
	});

	it("detach() is idempotent", async () => {
		const { conn, session } = await setupConnectedSession();
		await session.detach();
		await session.detach();
		await conn.close();
	});
});

// --- Stale event handling ------------------------------------------

describe("WorkstationSession: stale event handling", () => {
	it("stale-duplicate state_update is dropped with warn", async () => {
		const { session, store, subscription_id } = await setupConnectedSession({ user: { v: 1 } });
		// Send an update with seq=10.
		deliverStateUpdate(subscription_id, { user: { v: 2 } }, { seq: 10 });
		expect(store.get(session.userState)).toEqual({ v: 2 });

		// Now send a duplicate with seq=10 (stale).
		deliverStateUpdate(subscription_id, { user: { v: 999 } }, { seq: 10 });
		expect(store.get(session.userState)).toEqual({ v: 2 });
		expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("stale state_update"));
	});

	it("non-state event seq is acked even though payload is dropped (chunk 6 no-slot semantics)", async () => {
		const { connector, store, session, subscription_id } = await setupConnectedSession();
		store.set(session.userState, { x: 1 });
		await Promise.resolve();

		// Foreign load_result event arrives with seq=5. We have no
		// slot machinery, so it gets dropped — but lastProcessedSeq
		// bumps to 5. The next mutation's ack_seq reflects this.
		const frame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: { kind: "load_result", seq: 5, slot: 1, payload: null },
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));

		// Commit the in-flight to clear it. Then make another write
		// and confirm ack_seq advanced.
		const m1 = findLastMutation();
		commitMutation(m1.request_id, subscription_id, m1.update_id, 6);
		store.set(session.userState, { x: 2 });
		await Promise.resolve();
		const m2 = findLastMutation();
		// ack_seq should reflect lastProcessedSeq (max of 5 and 6 = 6).
		expect(m2.ack_seq).toBe(6);
		void connector; // sanity
	});
});
