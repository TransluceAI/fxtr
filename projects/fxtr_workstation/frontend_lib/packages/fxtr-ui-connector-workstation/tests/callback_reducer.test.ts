/**
 * Tests for the callback-slot reducer (stage 6 chunk 2).
 *
 * Covers `allocateCallback` slot allocation + spec capture,
 * `CreateCallbackOp` emission via the flush walk, `CloseCallbackOp`
 * emission when all inbox stubs for a slot are removed from local
 * user state, no-emit cases (legitimate inbox already in committed,
 * cursor-only changes), and multi-stub-per-slot min-offset behavior.
 *
 * `CallbackEvent` reception, `inboxLogs` population, and the
 * preserve / unpreserve diff for callback events land in chunk 3.
 */

import { createStore } from "jotai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CallbackInboxStub } from "@fxtr-ui/core";
import { atom } from "jotai";
import {
	type CallbackEvent,
	type CallbackSubscriptionEvent,
	type FillRecord,
	type InboxLog,
	type InboxSubscriptionState,
	type MutationOp,
	type SessionEventFrame,
	type SessionSnapshotEvent,
	type StateUpdateEvent,
	WorkstationCallbackInboxStub,
	WorkstationConnector,
	WorkstationServerConnection,
	type WorkstationSession,
	type WorkstationStubSession,
} from "../src";
import { MockWebSocket, currentMockSocket, resetMockWebSockets } from "./_mock_websocket";

// --- Scaffolding --------------------------------------------------

interface SentMessage {
	request_id: number;
	op: string;
	update_id?: number;
	subscription_id?: number;
	patch?: unknown;
	ops?: MutationOp[];
	[k: string]: unknown;
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

function sent(): SentMessage[] {
	return currentMockSocket().sentParsed as SentMessage[];
}
function lastSent(): SentMessage {
	const all = sent();
	const last = all[all.length - 1];
	if (!last) throw new Error("no message has been sent");
	return last;
}
function lastMutate(): SentMessage {
	for (let i = sent().length - 1; i >= 0; i--) {
		const m = sent()[i];
		if (m && m.op === "mutate_session") return m;
	}
	throw new Error("no mutate_session has been sent");
}

let _seq = 0;
function freshSeq(): number {
	_seq += 1;
	return _seq;
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

async function setupConnectedSession(): Promise<{
	connector: WorkstationConnector;
	store: ReturnType<typeof createStore>;
	session: WorkstationSession;
	subscription_id: number;
}> {
	const { connector } = makeConnector();
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
	const subscribe = lastSent();
	const subscription_id = subscribe.request_id;
	currentMockSocket().simulateMessage(JSON.stringify({ type: "ack", request_id: subscription_id }));
	const snapshot: SessionSnapshotEvent = {
		subscription_id,
		type: "session_snapshot",
		session_id: "sess",
		last_state: { ...stateUpdate({}), seq: 0 },
		preserved_events: [],
		subsequent_events: [],
	};
	currentMockSocket().simulateMessage(JSON.stringify(snapshot));
	return { connector, store: session.store, session, subscription_id };
}

function commitLast(subscription_id: number, seq?: number): void {
	const m = lastMutate();
	currentMockSocket().simulateMessage(
		JSON.stringify({
			type: "mutation_committed",
			request_id: m.request_id,
			subscription_id,
			seq: seq ?? freshSeq(),
			update_id: m.update_id ?? 0,
		}),
	);
}

async function drainMicrotask(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
}

/** Deliver a CallbackEvent on the active subscription. Returns the event's seq. */
function deliverCallbackEvent(
	subscription_id: number,
	slot: number,
	payload: unknown,
	seq?: number,
): number {
	const evSeq = seq ?? freshSeq();
	const event: CallbackEvent = {
		kind: "callback",
		seq: evSeq,
		slot,
		payload: payload as never,
	};
	const frame: SessionEventFrame = { subscription_id, type: "session_event", event };
	currentMockSocket().simulateMessage(JSON.stringify(frame));
	return evSeq;
}

/** Deliver a CallbackSubscriptionEvent on the active subscription. */
function deliverSubscriptionEvent(
	subscription_id: number,
	slot: number,
	connected: boolean,
	reason: string | null = null,
	seq?: number,
): number {
	const evSeq = seq ?? freshSeq();
	const event: CallbackSubscriptionEvent = {
		kind: "callback_subscription",
		seq: evSeq,
		slot,
		connected,
		reason,
	};
	const frame: SessionEventFrame = { subscription_id, type: "session_event", event };
	currentMockSocket().simulateMessage(JSON.stringify(frame));
	return evSeq;
}

let warnSpy: ReturnType<typeof vi.spyOn>;
let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
	resetMockWebSockets();
	_seq = 0;
	warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
	errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
	warnSpy.mockRestore();
	errorSpy.mockRestore();
});

// --- Tests --------------------------------------------------------

describe("WorkstationSession.allocateCallback: CreateCallbackOp emission", () => {
	it("allocate + write → CreateCallbackOp emitted with the right slot", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			inbox: ctx.allocateCallback(),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const creates = (mut.ops as MutationOp[]).filter((op) => op.kind === "create_callback");
		expect(creates).toHaveLength(1);
		const op = creates[0] as Extract<MutationOp, { kind: "create_callback" }>;
		// localSlotCounter started at 0; first allocate bumps to 1.
		expect(op.slot).toBe(1);

		// Commit so the session ends in a clean state.
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("allocate without storing the stub → no CreateCallbackOp emitted", async () => {
		const { session, store } = await setupConnectedSession();
		store.set(session.userState, (prev, ctx) => {
			ctx.allocateCallback();
			return prev;
		});
		await drainMicrotask();
		// Same shape as the invoke-allocate-without-store test: a mutation
		// may still ship (e.g. `add /user null`), but no CreateCallbackOp.
		const allOps: MutationOp[] = [];
		for (const m of sent()) {
			if (m.op === "mutate_session" && m.ops) allOps.push(...m.ops);
		}
		expect(allOps.filter((op) => op.kind === "create_callback")).toHaveLength(0);
	});

	it("two allocateCallback calls → two distinct slots, two CreateCallbackOps", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			a: ctx.allocateCallback(),
			b: ctx.allocateCallback(),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const creates = (mut.ops as MutationOp[]).filter(
			(op): op is Extract<MutationOp, { kind: "create_callback" }> => op.kind === "create_callback",
		);
		expect(creates.map((op) => op.slot).sort((a, b) => a - b)).toEqual([1, 2]);
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("cursor advance (sliced) on a stub already in committed → no CreateCallbackOp, no CloseCallbackOp", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		// Allocate + commit so the slot is in committed state.
		store.set(session.userState, (_, ctx) => ({ inbox: ctx.allocateCallback() }));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Now advance the cursor — sliced returns a new stub with offset+1.
		store.set(session.userState, (prev) => {
			const u = prev as { inbox: CallbackInboxStub };
			return { inbox: u.inbox.sliced(1) };
		});
		await drainMicrotask();
		const mut = lastMutate();
		const ops = mut.ops as MutationOp[];
		// No create / close — only a patch updating the offset.
		expect(ops.filter((op) => op.kind === "create_callback")).toHaveLength(0);
		expect(ops.filter((op) => op.kind === "close_callback")).toHaveLength(0);
		commitLast(subscription_id);
		await drainMicrotask();
	});
});

describe("WorkstationSession.allocateCallback: CloseCallbackOp emission", () => {
	it("removing the only inbox stub for a slot → CloseCallbackOp emitted", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		// Allocate + commit.
		store.set(session.userState, (_, ctx) => ({ inbox: ctx.allocateCallback() }));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Remove the stub from state.
		store.set(session.userState, () => ({}));
		await drainMicrotask();
		const mut = lastMutate();
		const closes = (mut.ops as MutationOp[]).filter(
			(op): op is Extract<MutationOp, { kind: "close_callback" }> => op.kind === "close_callback",
		);
		expect(closes.map((op) => op.slot)).toEqual([1]);
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("removing one of two inbox stubs for the same slot → no CloseCallbackOp", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		// Allocate one stub, write it twice (two cursor readers on the same slot).
		store.set(session.userState, (_, ctx) => {
			const inbox = ctx.allocateCallback();
			return { a: inbox, b: inbox };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Remove one reader; the other is still in local state.
		store.set(session.userState, (prev) => {
			const u = prev as { a: CallbackInboxStub; b: CallbackInboxStub };
			return { b: u.b };
		});
		await drainMicrotask();
		const mut = lastMutate();
		const closes = (mut.ops as MutationOp[]).filter((op) => op.kind === "close_callback");
		expect(closes).toHaveLength(0);
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("allocate-and-close in the same flush is a no-op for create/close", async () => {
		// If the user allocates a stub, writes it, then never commits and
		// immediately removes it (in a single tick), the flush walk sees
		// no in-local trace and the pendingSlotSpec gets wiped at end of
		// flush — no CreateCallbackOp, no CloseCallbackOp.
		const { session, store } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			inbox: ctx.allocateCallback(),
		}));
		// Synchronously remove before flushing.
		store.set(session.userState, () => ({}));
		await drainMicrotask();
		const allOps: MutationOp[] = [];
		for (const m of sent()) {
			if (m.op === "mutate_session" && m.ops) allOps.push(...m.ops);
		}
		expect(allOps.filter((op) => op.kind === "create_callback")).toHaveLength(0);
		expect(allOps.filter((op) => op.kind === "close_callback")).toHaveLength(0);
	});

	it("allocate-then-close after commit-and-remove → fresh slot for new allocate", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		// Allocate slot 1, commit.
		store.set(session.userState, (_, ctx) => ({ inbox: ctx.allocateCallback() }));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();
		// Remove (CloseCallbackOp fires).
		store.set(session.userState, () => ({}));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();
		// Allocate again — should get slot 2 (counter is monotonic).
		store.set(session.userState, (_, ctx) => ({ inbox: ctx.allocateCallback() }));
		await drainMicrotask();
		const mut = lastMutate();
		const creates = (mut.ops as MutationOp[]).filter(
			(op): op is Extract<MutationOp, { kind: "create_callback" }> => op.kind === "create_callback",
		);
		expect(creates.map((op) => op.slot)).toEqual([2]);
		commitLast(subscription_id);
		await drainMicrotask();
	});
});

describe("WorkstationSession.allocateCallback: type sanity", () => {
	it("returned stub is an instanceof WorkstationCallbackInboxStub", async () => {
		const { session, store } = await setupConnectedSession();
		let captured: CallbackInboxStub | undefined;
		store.set(session.userState, (_, ctx) => {
			captured = ctx.allocateCallback();
			return { inbox: captured };
		});
		expect(captured).toBeInstanceOf(WorkstationCallbackInboxStub);
		expect((captured as unknown as WorkstationCallbackInboxStub).slot).toBe(1);
		expect((captured as unknown as WorkstationCallbackInboxStub).offset).toBe(0);
	});
});

describe("WorkstationSession: cross-session inbox substitution", () => {
	/** Build a minimal `WorkstationStubSession` standing in for "another session". */
	function makeForeignSession(id: string): WorkstationStubSession {
		return {
			sessionId: id,
			slotFillsAtom: atom<ReadonlyMap<number, FillRecord>>(new Map<number, FillRecord>()),
			committedSlotSetAtom: atom<ReadonlySet<number>>(new Set<number>()),
			brokenSlotsAtom: atom<ReadonlySet<number>>(new Set<number>()),
			inboxLogsAtom: atom<ReadonlyMap<number, InboxLog>>(new Map<number, InboxLog>()),
			committedInboxSlotSetAtom: atom<ReadonlySet<number>>(new Set<number>()),
			inboxSubscriptionStateAtom: atom<ReadonlyMap<number, InboxSubscriptionState>>(
				new Map<number, InboxSubscriptionState>(),
			),
		};
	}

	it("substitutes a cross-session inbox stub with the dead-inbox-sentinel on flush", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		const foreign = makeForeignSession("other-session");
		const foreignStub = new WorkstationCallbackInboxStub(99, 7, foreign);

		// Smuggle the foreign stub into this session's user state.
		store.set(session.userState, () => ({ smuggled: foreignStub }));
		await drainMicrotask();

		// The flush walk substitutes the foreign stub with the
		// dead-inbox-sentinel, which encodes as ["broken_inbox", 0].
		// The wire patch should carry that value at /user/smuggled.
		const mut = lastMutate();
		const patch = mut.patch as Array<{ op: string; path: string; value?: unknown }>;
		const smuggleOp = patch.find((p) => p.path === "/user/smuggled" || p.path === "/user");
		expect(smuggleOp).toBeDefined();
		// Patch shape: the cross-session-substituted value should be
		// `["broken_inbox", 0]` somewhere in the patch's value tree.
		const json = JSON.stringify(patch);
		expect(json).toContain('"broken_inbox"');
		expect(json).not.toContain('"inbox"'); // No live inbox tag should appear.

		// No CreateCallbackOp — the foreign slot doesn't have a
		// pendingSpec in our session.
		const creates = (mut.ops as MutationOp[]).filter((op) => op.kind === "create_callback");
		expect(creates).toHaveLength(0);

		// The local atom has the dead-inbox-sentinel substituted in
		// place of the foreign stub.
		const localUser = store.get(session.userState) as { smuggled: CallbackInboxStub };
		expect(localUser.smuggled).toBeInstanceOf(WorkstationCallbackInboxStub);
		expect((localUser.smuggled as unknown as WorkstationCallbackInboxStub).slot).toBe(0);
		expect((localUser.smuggled as unknown as WorkstationCallbackInboxStub).session).toBe(
			session as unknown as WorkstationStubSession,
		);

		commitLast(subscription_id);
		await drainMicrotask();
	});
});

// ===================================================================
// Chunk 3 — CallbackEvent reception, preserve flow, snapshot rebuild
// ===================================================================

describe("WorkstationSession.handleCallbackEvent", () => {
	it("appends a CallbackEvent's payload to inbox.events", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		let inbox: CallbackInboxStub<number> | undefined;
		store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback<number>();
			return { cb: inbox };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		deliverCallbackEvent(subscription_id, 1, 42);
		expect(store.get((inbox as CallbackInboxStub<number>).events)).toEqual([42]);

		deliverCallbackEvent(subscription_id, 1, 99);
		expect(store.get((inbox as CallbackInboxStub<number>).events)).toEqual([42, 99]);
	});

	it("decodes payloads with entity / service refs", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		let inbox: CallbackInboxStub | undefined;
		store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback();
			return { cb: inbox };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		deliverCallbackEvent(subscription_id, 1, ["entity", "ent-a"]);
		const events = store.get((inbox as CallbackInboxStub).events);
		expect(events).toHaveLength(1);
		// `unescapeWire` decodes ["entity", "ent-a"] via the connector's
		// entity lookup → returns the same interned EntityRefStub.
		const refFromInbox = events[0];
		const refDirect = connector.entityRef("ent-a");
		expect(refFromInbox).toBe(refDirect);
	});

	it("throws on a CallbackEvent for a slot not in committedInboxSlotSet", async () => {
		// Allocate but DON'T commit — slot 1 is in pendingSlotSpecs
		// but not yet in committedInboxSlotSet. A CallbackEvent
		// arriving here would be a protocol bug (the server can't
		// notify before CreateCallback commits). `handleCallbackEvent`
		// throws; the connection's `callOnFrame` catches the throw
		// and logs it to `console.error` (isolating one bad handler
		// from the rest of the connection). The event is not
		// appended to `inboxLogs`.
		const { session, store, subscription_id } = await setupConnectedSession();
		let inbox: CallbackInboxStub | undefined;
		store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback();
			return { cb: inbox };
		});
		await drainMicrotask();
		// Note: we deliberately do NOT call commitLast() here.

		deliverCallbackEvent(subscription_id, 1, "early");

		// The throw was caught by `callOnFrame` and logged. Verify
		// via the error spy + observable side effects (no event in
		// the log).
		expect(errorSpy).toHaveBeenCalledWith(
			expect.stringContaining("subscription handler.onFrame threw"),
			expect.objectContaining({ message: expect.stringContaining("not in committedInboxSlotSet") }),
		);
		expect(store.get((inbox as CallbackInboxStub).events)).toEqual([]);
	});

	it("ignores malformed CallbackEvent payloads (logs, doesn't crash)", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		let inbox: CallbackInboxStub | undefined;
		store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback();
			return { cb: inbox };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// `["nonsense_tag", 5]` will fail decode (no such reserved tag).
		deliverCallbackEvent(subscription_id, 1, ["nonsense_tag", 5]);
		expect(store.get((inbox as CallbackInboxStub).events)).toEqual([]);

		// Subsequent valid event still flows.
		deliverCallbackEvent(subscription_id, 1, "ok");
		expect(store.get((inbox as CallbackInboxStub).events)).toEqual(["ok"]);
	});
});

describe("WorkstationSession: callback preserve / unpreserve diff", () => {
	it("PreserveEventsOp fires for callback seqs <= ack_seq when cursor is at offset 0", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		let inbox: CallbackInboxStub<number> | undefined;
		store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback<number>();
			return { cb: inbox };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Deliver events. seqs are determined by freshSeq() — let's
		// capture them.
		const seq1 = deliverCallbackEvent(subscription_id, 1, "a");
		const seq2 = deliverCallbackEvent(subscription_id, 1, "b");

		// Trigger a flush by writing user state (no-op user change is
		// fine — the flush re-runs the preserve diff). Use sliced(0)
		// to produce a new stub identical to current cursor.
		store.set(session.userState, (prev) => {
			const u = prev as { cb: CallbackInboxStub };
			return { cb: u.cb.sliced(0) };
		});
		await drainMicrotask();
		const mut = lastMutate();
		const ops = mut.ops as MutationOp[];
		const preserves = ops.filter(
			(op): op is Extract<MutationOp, { kind: "preserve_events" }> => op.kind === "preserve_events",
		);
		expect(preserves).toHaveLength(1);
		expect(preserves[0]?.seqs).toEqual([seq1, seq2].sort((a, b) => a - b));
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("UnpreserveEventsOp fires for seqs consumed past by sliced", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		let inbox: CallbackInboxStub<number> | undefined;
		store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback<number>();
			return { cb: inbox };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		const seq1 = deliverCallbackEvent(subscription_id, 1, "a");
		const seq2 = deliverCallbackEvent(subscription_id, 1, "b");
		const seq3 = deliverCallbackEvent(subscription_id, 1, "c");

		// First mutation: preserve all three.
		store.set(session.userState, (prev) => {
			const u = prev as { cb: CallbackInboxStub };
			return { cb: u.cb.sliced(0) };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Now advance past seq1+seq2 by slicing 2.
		store.set(session.userState, (prev) => {
			const u = prev as { cb: CallbackInboxStub };
			return { cb: u.cb.sliced(2) };
		});
		await drainMicrotask();
		const mut = lastMutate();
		const ops = mut.ops as MutationOp[];
		const unpreserves = ops.filter(
			(op): op is Extract<MutationOp, { kind: "unpreserve_events" }> =>
				op.kind === "unpreserve_events",
		);
		expect(unpreserves).toHaveLength(1);
		// seq1 + seq2 dropped; seq3 still preserved.
		expect(unpreserves[0]?.seqs).toEqual([seq1, seq2].sort((a, b) => a - b));
		const preserves = ops.filter((op) => op.kind === "preserve_events");
		expect(preserves).toHaveLength(0);
		commitLast(subscription_id);
		await drainMicrotask();
		void seq3;
	});
});

describe("WorkstationSession: reindex inbox trim-on-commit", () => {
	it("trims front events when committed min_offset advances", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		let inbox: CallbackInboxStub<string> | undefined;
		store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback<string>();
			return { cb: inbox };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		deliverCallbackEvent(subscription_id, 1, "a");
		deliverCallbackEvent(subscription_id, 1, "b");
		deliverCallbackEvent(subscription_id, 1, "c");
		// At offset=0, events visible to the stub = ["a", "b", "c"].
		expect(store.get((inbox as CallbackInboxStub<string>).events)).toEqual(["a", "b", "c"]);

		// Advance cursor by 2 → commit → reindex trims.
		store.set(session.userState, (prev) => {
			const u = prev as { cb: CallbackInboxStub<string> };
			return { cb: u.cb.sliced(2) };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();
		// New stub references slot=1, offset=2. Inbox log firstIndex
		// should now be 2; events list contains only the seq=3 event.
		const u = store.get(session.userState) as { cb: CallbackInboxStub<string> };
		expect(store.get(u.cb.events)).toEqual(["c"]);
		expect(u.cb.offset).toBe(2);
	});

	it("drops the inboxLogs entry entirely when no inbox stub remains in committed", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({ cb: ctx.allocateCallback() }));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		deliverCallbackEvent(subscription_id, 1, "a");

		// Remove the inbox stub from state → CloseCallbackOp fires.
		store.set(session.userState, () => ({}));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// inboxLogs entry for slot 1 should be gone. Construct a
		// stub at slot=1 and verify its events atom returns [].
		const stub = new WorkstationCallbackInboxStub(
			1,
			0,
			session as unknown as WorkstationStubSession,
		);
		expect(store.get(stub.events)).toEqual([]);
	});
});

describe("WorkstationSession: snapshot rebuilds inboxLogs from CallbackEvents", () => {
	it("snapshot with advanced cursor sets firstIndex to committed min_offset", async () => {
		// Drive a new session through the snapshot path with state
		// containing an inbox stub at offset=5; the preserved events
		// represent indices 5..7 (3 events).
		const { connector } = makeConnector();
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
		const subscribe = lastSent();
		const subscription_id = subscribe.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		const snapshot: SessionSnapshotEvent = {
			subscription_id,
			type: "session_snapshot",
			session_id: "sess",
			last_state: {
				kind: "state_update",
				seq: 50,
				ack_seq: 50,
				state: { user: { cb: ["inbox", 7, 5] as unknown } } as StateUpdateEvent["state"],
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 7,
			},
			preserved_events: [
				{ kind: "callback", seq: 1, slot: 7, payload: "e5" as unknown as never },
				{ kind: "callback", seq: 2, slot: 7, payload: "e6" as unknown as never },
				{ kind: "callback", seq: 3, slot: 7, payload: "e7" as unknown as never },
			],
			subsequent_events: [],
		};
		currentMockSocket().simulateMessage(JSON.stringify(snapshot));

		// The stub at offset=5 should see all three events
		// (firstIndex=5 in inboxLogs aligns with the cursor's offset).
		const u = session.store.get(session.userState) as { cb: CallbackInboxStub<string> };
		expect(u.cb.offset).toBe(5);
		expect(session.store.get(u.cb.events)).toEqual(["e5", "e6", "e7"]);
	});
});

// ===================================================================
// Chunk 3.5/5 — callback-target invokes (CallbackInvokeOp emission)
// ===================================================================

describe("WorkstationSession.allocateInvoke with a CallbackHandleStub target", () => {
	it("same-tick allocateCallback + allocateInvoke(handle) → CreateCallbackOp then CallbackInvokeOp", async () => {
		const { connector, session, store, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("s1");
		store.set(session.userState, (_, ctx) => {
			const inbox = ctx.allocateCallback();
			const sub = ctx.allocateInvoke<null>(inbox.handle, "subscribe_to", [svc]);
			return { inbox, sub };
		});
		await drainMicrotask();
		const mut = lastMutate();
		const ops = mut.ops as MutationOp[];
		const create = ops.find((op) => op.kind === "create_callback") as Extract<
			MutationOp,
			{ kind: "create_callback" }
		>;
		const invoke = ops.find((op) => op.kind === "callback_invoke") as Extract<
			MutationOp,
			{ kind: "callback_invoke" }
		>;
		expect(create).toBeDefined();
		expect(invoke).toBeDefined();
		expect(create.slot).toBe(1);
		expect(invoke.slot).toBe(2);
		expect(invoke.callback_slot).toBe(1);
		expect(invoke.method_name).toBe("subscribe_to");
		expect(invoke.args).toEqual([["service", "s1"]]);
		// Slot-monotonic wire order: the create precedes the invoke.
		expect(ops.indexOf(create)).toBeLessThan(ops.indexOf(invoke));

		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("cross-tick: invoking on a committed inbox's handle → CallbackInvokeOp with the committed slot", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		let inbox: CallbackInboxStub<unknown> | undefined;
		store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback();
			return { inbox };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		store.set(session.userState, (prev, ctx) => ({
			...(prev as Record<string, unknown>),
			ping: ctx.allocateInvoke<null>((inbox as CallbackInboxStub<unknown>).handle, "notify", [
				"hello",
			]),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const ops = mut.ops as MutationOp[];
		const invoke = ops.find((op) => op.kind === "callback_invoke") as Extract<
			MutationOp,
			{ kind: "callback_invoke" }
		>;
		expect(invoke).toBeDefined();
		expect(invoke.callback_slot).toBe(1);
		expect(invoke.slot).toBe(2);
		expect(invoke.method_name).toBe("notify");
		expect(invoke.args).toEqual(["hello"]);
		// No fresh CreateCallbackOp this tick.
		expect(ops.filter((op) => op.kind === "create_callback")).toHaveLength(0);

		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("rejects a cross-session handle target without burning a slot", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		const foreign: WorkstationStubSession = {
			sessionId: "other-session",
			slotFillsAtom: atom<ReadonlyMap<number, FillRecord>>(new Map<number, FillRecord>()),
			committedSlotSetAtom: atom<ReadonlySet<number>>(new Set<number>()),
			brokenSlotsAtom: atom<ReadonlySet<number>>(new Set<number>()),
			inboxLogsAtom: atom<ReadonlyMap<number, InboxLog>>(new Map<number, InboxLog>()),
			committedInboxSlotSetAtom: atom<ReadonlySet<number>>(new Set<number>()),
			inboxSubscriptionStateAtom: atom<ReadonlyMap<number, InboxSubscriptionState>>(
				new Map<number, InboxSubscriptionState>(),
			),
		};
		const foreignHandle = new WorkstationCallbackInboxStub(99, 7, foreign).handle;

		expect(() => {
			store.set(session.userState, (prev, ctx) => {
				ctx.allocateInvoke<null>(foreignHandle, "notify", ["x"]);
				return prev;
			});
		}).toThrow(/different session/);

		// The rejection didn't advance the slot counter: the next
		// allocate still gets slot 1.
		store.set(session.userState, (_, ctx) => ({ inbox: ctx.allocateCallback() }));
		await drainMicrotask();
		const mut = lastMutate();
		const create = (mut.ops as MutationOp[]).find((op) => op.kind === "create_callback") as Extract<
			MutationOp,
			{ kind: "create_callback" }
		>;
		expect(create.slot).toBe(1);

		commitLast(subscription_id);
		await drainMicrotask();
	});
});

// ===================================================================
// 6.4post1 — subscribe_to connection-state transitions
// ===================================================================

describe("WorkstationSession: callback inbox connectionState", () => {
	it("connectionState defaults to idle, flips to broken (with reason) then back to connected", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({ inbox: ctx.allocateCallback() }));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		const u = store.get(session.userState) as { inbox: CallbackInboxStub<unknown> };
		const inbox = u.inbox;
		// No subscription yet → idle, no reason.
		expect(store.get(inbox.connectionState)).toBe("idle");
		expect(store.get(inbox.subscriptionBreakReason)).toBeNull();

		// Break transition → broken + reason.
		deliverSubscriptionEvent(subscription_id, 1, false, "rpc connection dropped");
		expect(store.get(inbox.connectionState)).toBe("broken");
		expect(store.get(inbox.subscriptionBreakReason)).toBe("rpc connection dropped");

		// Reconnect transition (resubscribe) → connected, reason cleared.
		deliverSubscriptionEvent(subscription_id, 1, true, null);
		expect(store.get(inbox.connectionState)).toBe("connected");
		expect(store.get(inbox.subscriptionBreakReason)).toBeNull();
	});

	it("snapshot rebuild reduces subscription events into the current connection state", async () => {
		// Drive a fresh session through the snapshot path: the committed
		// state holds an inbox stub at slot 7, and the snapshot's
		// subsequent_events carry a connected→broken transition pair.
		const { connector } = makeConnector();
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
		const subscribe = lastSent();
		const subscription_id = subscribe.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		const snapshot: SessionSnapshotEvent = {
			subscription_id,
			type: "session_snapshot",
			session_id: "sess",
			last_state: {
				kind: "state_update",
				seq: 50,
				ack_seq: 50,
				state: { user: { cb: ["inbox", 7, 0] as unknown } } as StateUpdateEvent["state"],
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 7,
			},
			preserved_events: [],
			// Latest-wins: connected (seq 51) then broken (seq 52) → broken.
			subsequent_events: [
				{ kind: "callback_subscription", seq: 51, slot: 7, connected: true } as never,
				{
					kind: "callback_subscription",
					seq: 52,
					slot: 7,
					connected: false,
					reason: "source revoked",
				} as never,
			],
		};
		currentMockSocket().simulateMessage(JSON.stringify(snapshot));

		const u = session.store.get(session.userState) as { cb: CallbackInboxStub<unknown> };
		expect(session.store.get(u.cb.connectionState)).toBe("broken");
		expect(session.store.get(u.cb.subscriptionBreakReason)).toBe("source revoked");
	});
});
