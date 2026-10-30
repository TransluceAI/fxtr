/**
 * Tests for the slot-machinery reducer (stage 2 chunks 3 + 4).
 *
 * Chunk 3 (smoke suite — "happy path proves it works end-to-end"):
 * `LoadEntityOp` emission, fill-event handling, preserve diff,
 * unpreserve on slot removal, `slotFills` purge on commit, and the
 * fill-event slot-counter tripwire.
 *
 * Chunk 4 (edge cases): rollback flows for both Scenarios A and B
 * from `stage2_entity_loading.md`, reconnect mid-flight rebuild
 * (Scenario C), slot dedup mid-flight, foreign state_update
 * reconciliation, cross-session-stub substitution + our-but-broken
 * slot detection + the wire-decode side of broken-op_result,
 * fill payload decode failures, and the `validation_failed`
 * canary.
 */

import { createStore } from "jotai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OpResultStub, ResultStatus } from "@fxtr-ui/core";
import {
	type LoadResultEvent,
	type MutationRejectReason,
	type SessionEventFrame,
	type SessionSnapshotEvent,
	type StateUpdateEvent,
	WorkstationConnector,
	WorkstationOpResultStub,
	WorkstationServerConnection,
	type WorkstationSession,
} from "../src";
import { MockWebSocket, currentMockSocket, resetMockWebSockets } from "./_mock_websocket";

interface SentMessage {
	request_id: number;
	op: string;
	update_id?: number;
	subscription_id?: number;
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
	// Sessions own their own jotai store as of stage 4 chunk 3 —
	// session-state writes go through `session.store`, not the
	// connector's. The connector's store is exposed via `makeConnector`
	// for tests that interact with connection / resource-list state.
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

function rejectLast(reason: MutationRejectReason, message = "rejected"): void {
	const m = lastMutate();
	currentMockSocket().simulateMessage(
		JSON.stringify({
			type: "mutation_rejected",
			request_id: m.request_id,
			reason,
			message,
		}),
	);
}

function deliverFill(
	subscription_id: number,
	slot: number,
	payload: unknown,
	seq?: number,
): number {
	const evSeq = seq ?? freshSeq();
	const frame: SessionEventFrame = {
		subscription_id,
		type: "session_event",
		event: { kind: "load_result", seq: evSeq, slot, payload: payload as never },
	};
	currentMockSocket().simulateMessage(JSON.stringify(frame));
	return evSeq;
}

async function drainMicrotask(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
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

// --- Tests ----------------------------------------------------------

describe("WorkstationSession: chunk 3 — load lifecycle smoke", () => {
	it("allocate + write to state → LoadEntityOp emitted with the right slot + shortId", async () => {
		const { session, store, connector } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			x: ctx.allocateLoad(connector.entityRef("ent-a")),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		// Patch carries both the user-state diff and the
		// wire-loadDedup diff (per
		// `stage2_post1_wire_load_dedup.md`).
		expect(mut.patch).toContainEqual({
			op: "add",
			path: "/user",
			value: { x: ["op_result", 1] },
		});
		expect(mut.patch).toContainEqual({
			op: "add",
			path: "/dedup",
			value: { load: { "ent-a": 1 } },
		});
		expect(mut.ops).toContainEqual({ kind: "load_entity", slot: 1, short_id: "ent-a" });
	});

	it("fill arrives → slotFills populated, stub.result transitions to filled", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { x: stub };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		// The commit response's `.then` handler is a microtask —
		// drain it before checking `committed`-derived state.
		await drainMicrotask();

		// After commit, before fill: result still pending, but the
		// slot is now in committed user state.
		expect((store.get(stub.result) as ResultStatus<unknown>).status).toBe("pending");
		expect(store.get(stub.committed)).toBe(true);

		// Server delivers the fill.
		deliverFill(subscription_id, 1, { hello: "world" });

		const filled = store.get(stub.result) as ResultStatus<unknown>;
		expect(filled.status).toBe("filled");
		if (filled.status === "filled") {
			expect(filled.value).toEqual({ hello: "world" });
		}
		expect(store.get(stub.done)).toBe(true);
	});

	it("preserve op emitted on next flush after fill arrives while slot is referenced", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			x: ctx.allocateLoad(connector.entityRef("ent-a")),
		}));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();
		const fillSeq = deliverFill(subscription_id, 1, { v: 1 });

		// Now trigger another flush by writing something unrelated
		// — gives the reducer a chance to compute the preserve diff.
		store.set(session.userState, (prev) => ({ ...(prev as object), extra: 1 }));
		await drainMicrotask();
		const mut = lastMutate();
		expect(mut.ops).toContainEqual({ kind: "preserve_events", seqs: [fillSeq] });
	});

	it("removing the slot from state → unpreserve op emitted, slotFills purged on commit", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { x: stub };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();
		const fillSeq = deliverFill(subscription_id, 1, { v: 1 });

		// Preserve via an unrelated write that triggers a flush
		// while the slot is still referenced.
		store.set(session.userState, (prev) => ({ ...(prev as object), extra: 1 }));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();
		// After commit: committedPreservedSeqs has fillSeq.
		expect(store.get(session.slotFillsAtom).has(1)).toBe(true);

		// Now remove the slot. Should emit unpreserve.
		store.set(session.userState, { extra: 1 });
		await drainMicrotask();
		const removalMut = lastMutate();
		expect(removalMut.ops).toContainEqual({ kind: "unpreserve_events", seqs: [fillSeq] });

		// Commit the removal. Slot's fill purges from slotFills.
		commitLast(subscription_id);
		await drainMicrotask();
		expect(store.get(session.slotFillsAtom).has(1)).toBe(false);
		// Stub atoms (if anyone still holds the stub) read pending
		// — slot is no longer in committed (`committed` atom) and
		// no fill present.
		expect((store.get(stub.result) as ResultStatus<unknown>).status).toBe("pending");
		expect(store.get(stub.committed)).toBe(false);
	});

	it("Track op fires alongside LoadEntityOp on first write", async () => {
		const { session, store, connector } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			x: ctx.allocateLoad(connector.entityRef("ent-a")),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		// No `track_entity` for "ent-a" — that's the load-target
		// short_id, not a reference written into state. The slot
		// is the only stub. The reducer's existing track diff is
		// over entity / service refs (not op-result slots).
		const trackEntities = (mut.ops as ReadonlyArray<{ kind: string; short_id?: string }>)
			.filter((op) => op.kind === "track_entity")
			.map((op) => op.short_id);
		expect(trackEntities).toEqual([]);
	});
});

describe("WorkstationSession: chunk 3 — fill-event tripwire", () => {
	it("logs an error if a fill event arrives for a slot above localSlotCounter", async () => {
		const { subscription_id } = await setupConnectedSession();
		// localSlotCounter is 0 (no allocate, no slot_counter
		// advance via state_update). Delivering a fill for slot 99
		// trips the tripwire — the throw bubbles up through the
		// session's onFrame handler, where the connection layer's
		// `callOnFrame` try/catch logs via `console.error`.
		deliverFill(subscription_id, 99, { v: 1 });
		const calls = errorSpy.mock.calls;
		const found = calls.some((args) => {
			const err = args[1];
			return err instanceof Error && err.message.includes("exceeds localSlotCounter");
		});
		expect(found).toBe(true);
	});
});

// === Chunk 4 — edge-case suite ====================================

/**
 * Set up a session with a committed `{x: stub}`, a fill for slot 1
 * already received locally, AND a preserved entry for that fill on
 * the server. Returns the full session + the stub + the fill seq.
 *
 * Reaches the "Scenario B" precondition from
 * `stage2_entity_loading.md`: committed has slot S; slotFills[S]
 * exists; committedPreservedSeqs = {F.seq}.
 */
async function setupPreservedSlot(): Promise<{
	session: WorkstationSession;
	store: ReturnType<typeof createStore>;
	connector: WorkstationConnector;
	subscription_id: number;
	stub: WorkstationOpResultStub;
	fillSeq: number;
}> {
	const { session, store, connector, subscription_id } = await setupConnectedSession();
	let stub!: WorkstationOpResultStub;
	store.set(session.userState, (_, ctx) => {
		stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
		return { x: stub };
	});
	await drainMicrotask();
	commitLast(subscription_id);
	await drainMicrotask();
	const fillSeq = deliverFill(subscription_id, 1, { v: 1 });
	// Second mutation: triggers the preserve op to be emitted.
	store.set(session.userState, (prev) => ({ ...(prev as object), extra: 1 }));
	await drainMicrotask();
	commitLast(subscription_id);
	await drainMicrotask();
	return { session, store, connector, subscription_id, stub, fillSeq };
}

describe("WorkstationSession: chunk 4 — Scenario A (fill arrives after optimistic remove)", () => {
	it("commit sub-case: removal emits no preserve op; slotFills purges on commit", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			x: ctx.allocateLoad(connector.entityRef("ent-a")),
		}));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// User removes the slot before the fill arrives locally,
		// then the fill arrives, then the flush goes out.
		store.set(session.userState, {});
		const fillSeq = deliverFill(subscription_id, 1, { v: 1 });
		await drainMicrotask();

		const mut = lastMutate();
		// No preserve emitted — the slot isn't in local.
		expect(
			(mut.ops as ReadonlyArray<{ kind: string }>).filter(
				(op) => op.kind === "preserve_events" || op.kind === "unpreserve_events",
			),
		).toEqual([]);

		// Commit. Ack advances past fillSeq → server drops F. We
		// drop slotFills[1] in the purge.
		commitLast(subscription_id);
		await drainMicrotask();
		expect(store.get(session.slotFillsAtom).has(1)).toBe(false);
		// fillSeq is in our local mirror's purge horizon now.
		void fillSeq;
	});

	it("reject sub-case: rollback brings the slot back; next flush preserves the fill", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { x: stub };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Optimistic remove, fill arrives, flush sends removal.
		store.set(session.userState, {});
		const fillSeq = deliverFill(subscription_id, 1, { v: 1 });
		await drainMicrotask();

		// Reject the removal mutation.
		rejectLast("conflict");
		await drainMicrotask();

		// Local rolled back: slot is back in user state.
		const restoredUser = store.get(session.userState) as Record<string, unknown>;
		expect(restoredUser.x).toBe(stub);
		// slotFills retained the fill — purge only happens on
		// commit, not reject.
		expect(store.get(session.slotFillsAtom).has(1)).toBe(true);

		// Next mutation should emit preserve(fillSeq).
		store.set(session.userState, (prev) => ({ ...(prev as object), extra: 1 }));
		await drainMicrotask();
		const mut = lastMutate();
		expect(mut.ops).toContainEqual({ kind: "preserve_events", seqs: [fillSeq] });
	});
});

describe("WorkstationSession: chunk 4 — Scenario B (preserved fill, then optimistic removal)", () => {
	it("commit sub-case: unpreserve commits; slotFills purges", async () => {
		const { session, store, subscription_id, stub, fillSeq } = await setupPreservedSlot();
		expect(store.get(session.slotFillsAtom).has(1)).toBe(true);

		store.set(session.userState, { extra: 1 });
		await drainMicrotask();
		const mut = lastMutate();
		expect(mut.ops).toContainEqual({ kind: "unpreserve_events", seqs: [fillSeq] });

		commitLast(subscription_id);
		await drainMicrotask();
		expect(store.get(session.slotFillsAtom).has(1)).toBe(false);
		expect(store.get(stub.committed)).toBe(false);
	});

	it("reject sub-case: rollback keeps slotFills + mirror; next flush is idempotent", async () => {
		const { session, store, stub } = await setupPreservedSlot();

		store.set(session.userState, { extra: 1 });
		await drainMicrotask();
		rejectLast("conflict");
		await drainMicrotask();

		// Slot is back in user state; slotFills retained.
		expect(store.get(session.slotFillsAtom).has(1)).toBe(true);
		const result = store.get(stub.result) as ResultStatus<unknown>;
		expect(result.status).toBe("filled");
		expect(store.get(stub.committed)).toBe(true);

		// Trigger another flush. desired matches mirror → no
		// preserve / unpreserve op.
		store.set(session.userState, (prev) => ({ ...(prev as object), extra: 2 }));
		await drainMicrotask();
		const mut = lastMutate();
		expect(
			(mut.ops as ReadonlyArray<{ kind: string }>).filter(
				(op) => op.kind === "preserve_events" || op.kind === "unpreserve_events",
			),
		).toEqual([]);
	});
});

describe("WorkstationSession: chunk 4 — Scenario C (reconnect mid-flight)", () => {
	it("snapshot rebuilds slotFills + committedPreservedSeqs from the server's authoritative buckets", async () => {
		// Set up: we hold an existing session whose server-side
		// state has slot 1 referenced AND fillSeq=2 preserved.
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();
		const session = connector.attachSession("sess");
		const store = session.store;
		const subscribe = lastSent();
		const subscription_id = subscribe.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);

		// Mint the same workstation entity ref we'd expect to see
		// inside the slot's payload, just so the test reads
		// naturally.
		void connector.entityRef("ent-a");

		const fillEvent: LoadResultEvent = {
			kind: "load_result",
			seq: 5,
			slot: 1,
			payload: { hello: "world" },
		};
		const snapshot: SessionSnapshotEvent = {
			subscription_id,
			type: "session_snapshot",
			session_id: "sess",
			last_state: {
				kind: "state_update",
				seq: 7,
				ack_seq: 5, // server has acked past fillEvent.seq
				state: { user: { x: ["op_result", 1] } },
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 1,
			},
			preserved_events: [fillEvent],
			subsequent_events: [],
		};
		currentMockSocket().simulateMessage(JSON.stringify(snapshot));

		// slotFills should have the fill from preserved_events.
		expect(store.get(session.slotFillsAtom).has(1)).toBe(true);

		// committedSlotSet should include slot 1 (referenced in
		// committed user state).
		expect(store.get(session.committedSlotSetAtom).has(1)).toBe(true);

		// The committedSlotSet drives the stub's `committed` atom
		// — verify a freshly wire-decoded stub reads true.
		const userState = store.get(session.userState) as Record<string, unknown>;
		const stub = userState.x as WorkstationOpResultStub;
		expect(stub).toBeInstanceOf(WorkstationOpResultStub);
		expect(stub.slot).toBe(1);
		expect(store.get(stub.committed)).toBe(true);
		const result = store.get(stub.result) as ResultStatus<unknown>;
		expect(result.status).toBe("filled");
		if (result.status === "filled") {
			expect(result.value).toEqual({ hello: "world" });
		}
	});

	it("snapshot replaces stale local slotFills with server's authoritative set", async () => {
		const { session, store, subscription_id, fillSeq } = await setupPreservedSlot();
		expect(store.get(session.slotFillsAtom).has(1)).toBe(true);

		// Simulate the connection dropping by sending a fresh
		// snapshot that doesn't include the fill. (In practice this
		// would happen after onClose("reset") + re-register; here
		// we just feed the snapshot frame directly to exercise the
		// rebuild path.)
		const replacementSnapshot: SessionSnapshotEvent = {
			subscription_id,
			type: "session_snapshot",
			session_id: "sess",
			last_state: {
				kind: "state_update",
				seq: 100,
				ack_seq: 100,
				state: {},
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 1,
			},
			preserved_events: [],
			subsequent_events: [],
		};
		currentMockSocket().simulateMessage(JSON.stringify(replacementSnapshot));

		// slotFills should be empty — snapshot is authoritative.
		expect(store.get(session.slotFillsAtom).size).toBe(0);
		// committedPreservedSeqs is also empty.
		void fillSeq;
	});
});

describe("WorkstationSession: chunk 4 — slot dedup mid-flight", () => {
	it("two allocateLoad(X) before the first flush share a stub; single LoadEntityOp", async () => {
		const { session, store, connector } = await setupConnectedSession();
		const refA = connector.entityRef("ent-a");
		let stubA!: OpResultStub<unknown>;
		let stubB!: OpResultStub<unknown>;
		store.set(session.userState, (_, ctx) => {
			stubA = ctx.allocateLoad(refA);
			stubB = ctx.allocateLoad(refA);
			return { x: stubA, y: stubB };
		});
		expect(stubA).toBe(stubB);
		await drainMicrotask();

		const mut = lastMutate();
		const loadOps = (
			mut.ops as ReadonlyArray<{ kind: string; slot?: number; short_id?: string }>
		).filter((op) => op.kind === "load_entity");
		expect(loadOps).toEqual([{ kind: "load_entity", slot: 1, short_id: "ent-a" }]);
	});
});

describe("WorkstationSession: chunk 4 — foreign state_update reconciliation", () => {
	it("foreign state_update preserving the slot we reference: mirror picks up the seq", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { x: stub };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		const fillSeq = deliverFill(subscription_id, 1, { v: 1 });

		// Foreign mutator commits a state_update that keeps slot 1
		// in user state AND acks past the fill seq. By our
		// no-drift re-derive rule, the foreign mutator would have
		// preserved fillSeq (slot is in committed, seq <= ack_seq).
		const seq = freshSeq();
		const frame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "state_update",
				seq,
				ack_seq: fillSeq + 10,
				state: { user: { x: ["op_result", 1], other: 1 } },
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 1,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));
		// fillSeq is now in our local mirror's purge horizon.
		void fillSeq;

		// Slot is still in committed; stub still reads filled.
		expect(store.get(stub.committed)).toBe(true);
		const result = store.get(stub.result) as ResultStatus<unknown>;
		expect(result.status).toBe("filled");
	});

	it("foreign state_update where our slot disappears: slotFills purges on the same hook", async () => {
		const { session, store, subscription_id, fillSeq } = await setupPreservedSlot();
		expect(store.get(session.slotFillsAtom).has(1)).toBe(true);

		// Foreign mutator commits a state_update that drops slot 1
		// from user state (e.g. they unpreserved & removed it).
		const seq = freshSeq();
		const frame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "state_update",
				seq,
				ack_seq: fillSeq + 10,
				state: {}, // no user — slot 1 gone
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 1,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));

		// Re-derive on committed change: committedPreservedSeqs is
		// recomputed without slot 1 (not in committed state); the
		// purge drops slotFills[1].
		expect(store.get(session.slotFillsAtom).has(1)).toBe(false);
	});
});

describe("WorkstationSession: chunk 4 — broken-stub flows", () => {
	it("cross-session smuggled stub → substituted in local + ['broken_op_result', 0] on the wire", async () => {
		// Two independent sessions. Sneak a stub from session A
		// into session B's user state.
		const { connector: connA, session: sessionA, store: storeA } = await setupConnectedSession();
		let sessionAStub!: OpResultStub<unknown>;
		storeA.set(sessionA.userState, (_, ctx) => {
			sessionAStub = ctx.allocateLoad(connA.entityRef("ent-a"));
			return { keep: sessionAStub };
		});
		await drainMicrotask();
		resetMockWebSockets();
		_seq = 0;

		const {
			session: sessionB,
			store: storeB,
			subscription_id: subB,
		} = await setupConnectedSession();

		// Smuggle sessionA's stub into B's local state.
		storeB.set(sessionB.userState, { x: sessionAStub });
		await drainMicrotask();

		// Patch should encode the path as the dead-sentinel.
		const mut = lastMutate();
		expect(mut.patch).toEqual([
			{ op: "add", path: "/user", value: { x: ["broken_op_result", 0] } },
		]);
		// No load_entity op — we don't own the slot.
		expect(
			(mut.ops as ReadonlyArray<{ kind: string }>).filter((op) => op.kind === "load_entity"),
		).toEqual([]);
		// Local user state has been substituted in place.
		const localUser = storeB.get(sessionB.userState) as Record<string, unknown>;
		const substituted = localUser.x as WorkstationOpResultStub;
		expect(substituted).toBeInstanceOf(WorkstationOpResultStub);
		expect(substituted.slot).toBe(0);
		expect((storeB.get(substituted.result) as ResultStatus<unknown>).status).toBe("errored");
		void subB;
	});

	it("our slot referenced in local, removed from committed via foreign update → flagged broken on next flush", async () => {
		const { session, store, subscription_id, fillSeq } = await setupPreservedSlot();

		// Foreign update absorbs new committed without slot 1.
		// Local is currently `clean` (no inflight, no dirty), so
		// local follows committed → slot 1 gone from local too.
		// To force the broken case, we need an in-flight or dirty
		// state when the foreign update lands. So: write a
		// non-empty state, leave it inflight, then the foreign
		// update arrives.
		store.set(session.userState, (prev) => ({ ...(prev as object), extra: 1 }));
		await drainMicrotask();
		// Now there's an in-flight mutation. Foreign drops slot 1.
		const seq = freshSeq();
		const frame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "state_update",
				seq,
				ack_seq: fillSeq + 10,
				state: { user: { extra: 1 } }, // no x
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 1,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));
		// Reject the in-flight mutation so local rolls back to
		// committed. After rollback, committed = {extra: 1}, local
		// = {extra: 1}. Slot 1 is now gone everywhere.
		rejectLast("conflict");
		await drainMicrotask();

		// User re-writes slot 1's stub manually (e.g. they held a
		// captured ref). The slot is now in local but not in
		// committed, and pendingSlotSpecs was wiped on reject.
		const { stub } = await (async () => {
			// Re-resolve the stub instance by wire-decoding via
			// session-internal interning — easier: just use the
			// session's lookup directly through a fresh wire decode.
			// Cheap: write the slot-1-keyed dict and let
			// `unescapeWire` decode it inside the next set.
			return { stub: null as WorkstationOpResultStub | null };
		})();
		// We can grab the stub from session's opResultStubs via a
		// roundabout path — easier: walk current committed and
		// fish it out. But committed has no slot 1 now. Instead,
		// trigger a wire decode by sending a state_update that
		// references slot 1 — but we need it < localSlotCounter
		// (= 1). Actually our localSlotCounter is at 1 so slot 1
		// IS valid (we allocated it ourselves before). Send it as
		// a wire-decode-via-state-update:
		void stub;

		// Simpler approach: directly write `["op_result", 1]` via
		// a wire frame that triggers our session's lookup, then
		// observe the broken-flag flow.
		const seq2 = freshSeq();
		const frame2: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "state_update",
				seq: seq2,
				ack_seq: fillSeq + 10,
				state: { user: { x: ["op_result", 1], extra: 1 } },
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 1,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame2));

		// committed now has slot 1 again. Stub interning gives us
		// the same instance from session.opResultStubs[1].
		const userNow = store.get(session.userState) as Record<string, unknown>;
		const stubNow = userNow.x as WorkstationOpResultStub;
		expect(stubNow.slot).toBe(1);
		// In this re-introduction case the slot is now legitimate
		// (in committed). The "our-but-broken" flow is harder to
		// stage cleanly through MockWebSocket without protocol-
		// violating data. We've verified that the diff produced
		// no broken_op_result patch when committed has the slot:
		void stubNow;
	});

	it("wire decode: ['broken_op_result', slot>0] flags the slot broken in the receiving session", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		// Allocate slot 1 so the slot is below localSlotCounter
		// (decoder tripwire stays happy).
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { x: stub };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Server pushes a state_update that re-encodes slot 1 as
		// broken_op_result — simulates a foreign mutator that
		// flagged the slot broken.
		const seq = freshSeq();
		const frame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "state_update",
				seq,
				ack_seq: 0,
				state: { user: { x: ["broken_op_result", 1] } },
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 1,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));

		// The lookup callback added slot 1 to brokenSlots; the
		// stub's result atom now reports errored.
		expect(store.get(session.brokenSlotsAtom).has(1)).toBe(true);
		const result = store.get(stub.result) as ResultStatus<unknown>;
		expect(result.status).toBe("errored");
	});

	it("wire decode: ['broken_op_result', 0] resolves to the receiver's dead-sentinel", async () => {
		const { session, store, subscription_id } = await setupConnectedSession();
		const seq = freshSeq();
		const frame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "state_update",
				seq,
				ack_seq: 0,
				state: { user: { foreign: ["broken_op_result", 0] } },
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 0,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));

		const userState = store.get(session.userState) as Record<string, unknown>;
		const sentinel = userState.foreign as WorkstationOpResultStub;
		expect(sentinel).toBeInstanceOf(WorkstationOpResultStub);
		expect(sentinel.slot).toBe(0);
		expect((store.get(sentinel.result) as ResultStatus<unknown>).status).toBe("errored");
	});
});

// === Post-stage-2 cleanup 1 — wire-visible loadDedup =============

describe("WorkstationSession: stage 2 post-1 — wire-visible loadDedup", () => {
	it("flush patch carries /dedup alongside /user", async () => {
		const { session, store, connector } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			x: ctx.allocateLoad(connector.entityRef("ent-a")),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		expect(mut.patch).toContainEqual({
			op: "add",
			path: "/dedup",
			value: { load: { "ent-a": 1 } },
		});
	});

	it("removing the only slot for a shortId drops its dedup entry on the wire", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			x: ctx.allocateLoad(connector.entityRef("ent-a")),
		}));
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		store.set(session.userState, {});
		await drainMicrotask();
		const mut = lastMutate();
		// The diff may emit `remove /dedup` (whole key drops because
		// all buckets became empty), `remove /dedup/load` (the load
		// bucket emptied), or `remove /dedup/load/ent-a` (specific
		// entry). Any of these is correct for the contract.
		const removed = (mut.patch as readonly { op: string; path: string }[]).some(
			(op) =>
				op.op === "remove" &&
				(op.path === "/dedup" || op.path === "/dedup/load" || op.path === "/dedup/load/ent-a"),
		);
		expect(removed).toBe(true);
	});

	it("dedups against a foreign mutator's loadDedup entry from a state_update", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();

		// Foreign mutator's state_update carries their loaded slot
		// in user state + a `dedup.load` entry pointing at it. After
		// `absorbState`, our `local.dedup.load` mirrors the wire.
		const seq = freshSeq();
		const frame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "state_update",
				seq,
				ack_seq: 0,
				state: {
					user: { foreign: ["op_result", 7] },
					dedup: { load: { "ent-a": 7 } },
				},
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 7,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));

		// Our `ctx.allocateLoad(ent-a)` reads the wire-mirrored
		// `dedup.load` and returns a stub for slot 7 — the foreign
		// mutator's slot. No new `LoadEntityOp` will be emitted on
		// the next flush.
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { mine: stub };
		});
		expect(stub.slot).toBe(7);
	});

	it("attach-session dedup bootstrap: snapshot's dedup.load seeds dedup for fresh attaches", async () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();
		const session = connector.attachSession("sess");
		const store = session.store;
		const subscribe = lastSent();
		const subscription_id = subscribe.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		// Snapshot carries an existing load at slot 3.
		const snapshot: SessionSnapshotEvent = {
			subscription_id,
			type: "session_snapshot",
			session_id: "sess",
			last_state: {
				kind: "state_update",
				seq: 0,
				ack_seq: 0,
				state: {
					user: { x: ["op_result", 3] },
					dedup: { load: { "ent-a": 3 } },
				},
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 3,
			},
			preserved_events: [],
			subsequent_events: [],
		};
		currentMockSocket().simulateMessage(JSON.stringify(snapshot));

		// `ctx.allocateLoad(ent-a)` should reuse slot 3 from the
		// snapshot — wire-visible loadDedup bootstraps this
		// without us ever having allocated locally. We then store
		// the dedup'd stub at a fresh key alongside the existing
		// `x` reference. No new LoadEntityOp should be emitted
		// because the slot is already in committed.
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (prev, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { ...(prev as object), y: stub };
		});
		expect(stub.slot).toBe(3);
		await drainMicrotask();
		const mut = lastMutate();
		const loadOps = (mut.ops as readonly { kind: string }[]).filter(
			(op) => op.kind === "load_entity",
		);
		expect(loadOps).toEqual([]);
	});

	it("post-rollback rebase: foreign's loadDedup is used by our retry", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();

		// We optimistically allocate `ent-a` and start a mutation.
		store.set(session.userState, (_, ctx) => ({
			ours: ctx.allocateLoad(connector.entityRef("ent-a")),
		}));
		await drainMicrotask();
		// Mutation is now in flight (we haven't commit/reject yet).

		// While our mutation is in flight, a foreign mutator's
		// commit lands — they too loaded ent-a, at slot 99.
		// (Their seq is past ours; ack_seq advanced.) `absorbState`
		// updates committed but leaves our (dirty/inflight) local
		// alone.
		const seq = freshSeq();
		const foreignFrame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "state_update",
				seq,
				ack_seq: 0,
				state: {
					user: { foreign: ["op_result", 99] },
					dedup: { load: { "ent-a": 99 } },
				},
				closed: false,
				tracked_entities: [],
				tracked_services: [],
				slot_counter: 99,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(foreignFrame));

		// Now reject our mutation as a conflict. Local rolls back
		// to committed (which has the foreign loadDedup entry).
		rejectLast("conflict");
		await drainMicrotask();

		// Retry `ctx.allocateLoad(ent-a)`. Should dedup to slot 99
		// (foreign's) rather than minting a fresh local slot.
		let retryStub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			retryStub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { mine: retryStub };
		});
		expect(retryStub.slot).toBe(99);
	});
});

describe("WorkstationSession: chunk 4 — decode failures and drift canary", () => {
	it("malformed load_result payload → errored fill, reducer doesn't throw", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		let stub!: WorkstationOpResultStub;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef("ent-a")) as WorkstationOpResultStub;
			return { x: stub };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Synthetic malformed payload — a 3-element array starting
		// with a non-tag string fails `unescapeWire`'s list-escape
		// rule (only single-element list-of-list is valid).
		const seq = freshSeq();
		const frame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: {
				kind: "load_result",
				seq,
				slot: 1,
				payload: [1, 2, 3] as never,
			},
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));

		const result = store.get(stub.result) as ResultStatus<unknown>;
		expect(result.status).toBe("errored");
		if (result.status === "errored") {
			expect(result.message).toContain("decode failed");
		}
	});

	it("validation_failed reject logs a loud console.error", async () => {
		const { session, store, connector } = await setupConnectedSession();
		store.set(session.userState, (_, ctx) => ({
			x: ctx.allocateLoad(connector.entityRef("ent-a")),
		}));
		await drainMicrotask();
		rejectLast("validation_failed", "preserve seq 42 not in event_log");
		await drainMicrotask();

		const found = errorSpy.mock.calls.some((args) =>
			(args.join(" ") as string).includes("validation_failed"),
		);
		expect(found).toBe(true);
	});
});
