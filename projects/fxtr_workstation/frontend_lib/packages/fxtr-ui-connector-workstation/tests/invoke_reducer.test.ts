/**
 * Tests for the invoke-slot reducer (stage 3 chunk 2).
 *
 * Covers `allocateInvoke` slot allocation + spec capture, `InvokeOp`
 * emission via the flush walk, no-dedup behavior, arg/kwarg
 * encoding through `escapeInvokeArgs`, and up-front rejection of
 * forbidden stub kinds.
 *
 * Snapshot rebuild + invoke fill events land in chunk 3.
 */

import { createStore } from "jotai";
import { afterEach, beforeEach, describe, expect, expectTypeOf, it, vi } from "vitest";

import {
	CallbackInboxStub,
	type Method,
	type OpResultStub,
	type ResultStatus,
	type ResultValue,
} from "@fxtr-ui/core";
import {
	type InvokeErrorEvent,
	type InvokeResultEvent,
	type MutationOp,
	type SessionEventFrame,
	type SessionSnapshotEvent,
	type StateUpdateEvent,
	WireEscapeError,
	WorkstationConnector,
	type WorkstationOpResultStub,
	WorkstationServerConnection,
	type WorkstationSession,
} from "../src";
import { MockWebSocket, currentMockSocket, resetMockWebSockets } from "./_mock_websocket";

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

function deliverInvokeResult(
	subscription_id: number,
	slot: number,
	payload: unknown,
	seq?: number,
): number {
	const evSeq = seq ?? freshSeq();
	const event: InvokeResultEvent = {
		kind: "invoke_result",
		seq: evSeq,
		slot,
		payload: payload as never,
	};
	const frame: SessionEventFrame = { subscription_id, type: "session_event", event };
	currentMockSocket().simulateMessage(JSON.stringify(frame));
	return evSeq;
}

function deliverInvokeError(
	subscription_id: number,
	slot: number,
	message: string,
	seq?: number,
): number {
	const evSeq = seq ?? freshSeq();
	const event: InvokeErrorEvent = {
		kind: "invoke_error",
		seq: evSeq,
		slot,
		message,
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

// --- Tests ----------------------------------------------------------

describe("WorkstationSession.allocateInvoke", () => {
	it("allocate + write → InvokeOp emitted with the right slot / service / method / args / kwargs", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		store.set(session.userState, (_, ctx) => ({
			result: ctx.allocateInvoke(svc, "echo", [1, "two"], { kw: true }),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const invokeOps = (mut.ops as MutationOp[]).filter((op) => op.kind === "invoke");
		expect(invokeOps).toHaveLength(1);
		const op = invokeOps[0] as Extract<MutationOp, { kind: "invoke" }>;
		// localSlotCounter started at 0; first allocate bumps to 1.
		expect(op.slot).toBe(1);
		expect(op.service_short_id).toBe("svc-a");
		expect(op.method_name).toBe("echo");
		expect(op.args).toEqual([1, "two"]);
		expect(op.kwargs).toEqual({ kw: true });

		// Also commit the mutation so the session ends in a clean
		// state (no danglers).
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("omitted kwargs → empty dict on the wire", async () => {
		const { session, store, connector } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		store.set(session.userState, (_, ctx) => ({
			ping: ctx.allocateInvoke(svc, "ping", []),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const op = (mut.ops as MutationOp[]).find(
			(o): o is Extract<MutationOp, { kind: "invoke" }> => o.kind === "invoke",
		);
		expect(op).toBeDefined();
		expect(op?.kwargs).toEqual({});
	});

	it("allocate without storing the stub → no InvokeOp emitted", async () => {
		// In the ctx-style API every `allocate*` happens inside a
		// `set(userState, ...)`, so a mutation may still ship (e.g.
		// `add /user null` if the snapshot's envelope had no `user`
		// field). What we care about here is that the InvokeOp itself
		// only fires when the user actually stores the returned stub
		// in state — discard it inside the updater and the flush
		// walk's `pendingSlotSpecs` wipe drops the spec without
		// emitting `InvokeOp`.
		const { session, store, connector } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		store.set(session.userState, (prev, ctx) => {
			ctx.allocateInvoke(svc, "echo", [1]);
			return prev;
		});
		await drainMicrotask();
		// A mutation may or may not have been sent depending on
		// envelope-diff details; either way, no InvokeOp should
		// appear in it.
		let invokeOps: MutationOp[] = [];
		try {
			invokeOps = (lastMutate().ops as MutationOp[]).filter((op) => op.kind === "invoke");
		} catch {
			// `no mutate_session` — also fine; trivially no InvokeOp.
		}
		expect(invokeOps).toHaveLength(0);
	});

	it("two distinct allocateInvoke calls mint distinct slots (no dedup)", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		let a!: OpResultStub<unknown>;
		let b!: OpResultStub<unknown>;
		store.set(session.userState, (_, ctx) => {
			a = ctx.allocateInvoke(svc, "ping", []);
			b = ctx.allocateInvoke(svc, "ping", []);
			return { a, b };
		});
		expect(a).not.toBe(b);
		await drainMicrotask();
		const mut = lastMutate();
		const invokeOps = (mut.ops as MutationOp[]).filter((op) => op.kind === "invoke");
		expect(invokeOps).toHaveLength(2);
		const slots = invokeOps.map((op) => (op as Extract<MutationOp, { kind: "invoke" }>).slot);
		// Slots are strictly monotonic.
		expect(slots[0]).toBeLessThan(slots[1] as number);
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("encodes entity / service refs in args as tagged forms", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		const entArg = connector.entityRef("ent-1");
		const svcArg = connector.serviceRef("svc-b");
		store.set(session.userState, (_, ctx) => ({
			r: ctx.allocateInvoke(svc, "echo", [entArg, svcArg]),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const op = (mut.ops as MutationOp[]).find(
			(o): o is Extract<MutationOp, { kind: "invoke" }> => o.kind === "invoke",
		);
		expect(op?.args).toEqual([
			["entity", "ent-1"],
			["service", "svc-b"],
		]);
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("encodes dicts / lists in args via the list-escape codec", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		const entArg = connector.entityRef("ent-1");
		store.set(session.userState, (_, ctx) => ({
			r: ctx.allocateInvoke(svc, "echo", [{ items: [1, entArg], name: "n" }]),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const op = (mut.ops as MutationOp[]).find(
			(o): o is Extract<MutationOp, { kind: "invoke" }> => o.kind === "invoke",
		);
		expect(op?.args).toEqual([
			{
				items: [[1, ["entity", "ent-1"]]],
				name: "n",
			},
		]);
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("encodes refs through kwargs", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		const entArg = connector.entityRef("ent-1");
		store.set(session.userState, (_, ctx) => ({
			r: ctx.allocateInvoke(svc, "echo", [], { ref: entArg, n: 5 }),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const op = (mut.ops as MutationOp[]).find(
			(o): o is Extract<MutationOp, { kind: "invoke" }> => o.kind === "invoke",
		);
		expect(op?.args).toEqual([]);
		expect(op?.kwargs).toEqual({ ref: ["entity", "ent-1"], n: 5 });
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("rejects OpResultStub in args at allocate-time (no slot is burned)", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");

		// First make a legitimate slot to capture as a "smuggled" OpResultStub.
		let loadStub!: OpResultStub<unknown>;
		store.set(session.userState, (_, ctx) => {
			loadStub = ctx.allocateLoad(connector.entityRef("ent-1"));
			return { l: loadStub };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Try to use the load slot as an invoke arg — the updater throws
		// out of `store.set`. (We never reach a state where the bad
		// allocate could have been "stored" — the throw aborts before
		// the updater returns.)
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
				bad: ctx.allocateInvoke(svc, "echo", [loadStub]),
			})),
		).toThrow(WireEscapeError);
		// Nested in a dict / list — same.
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
				bad: ctx.allocateInvoke(svc, "echo", [{ inner: loadStub }]),
			})),
		).toThrow(WireEscapeError);
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
				bad: ctx.allocateInvoke(svc, "echo", [[1, loadStub]]),
			})),
		).toThrow(WireEscapeError);
		// In kwargs.
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
				bad: ctx.allocateInvoke(svc, "echo", [], { r: loadStub }),
			})),
		).toThrow(WireEscapeError);
	});

	it("rejects CallbackInboxStub in args at allocate-time", async () => {
		const { session, store, connector } = await setupConnectedSession();
		class TestCallbackInboxStub extends CallbackInboxStub {
			get offset() {
				return 0;
			}
			get events() {
				return null as never;
			}
			get state() {
				return null as never;
			}
			get connectionState() {
				return null as never;
			}
			get subscriptionBreakReason() {
				return null as never;
			}
			get committed() {
				return null as never;
			}
			get handle() {
				return null as never;
			}
			sliced() {
				return this;
			}
		}
		const cb = new TestCallbackInboxStub();
		const svc = connector.serviceRef("svc-a");
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
				bad: ctx.allocateInvoke(svc, "echo", [cb]),
			})),
		).toThrow(WireEscapeError);
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
				bad: ctx.allocateInvoke(svc, "echo", [], { cb }),
			})),
		).toThrow(WireEscapeError);
	});

	it("rejection does not burn a slot (next allocate uses the same counter)", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		class TestCallbackInboxStub extends CallbackInboxStub {
			get offset() {
				return 0;
			}
			get events() {
				return null as never;
			}
			get state() {
				return null as never;
			}
			get connectionState() {
				return null as never;
			}
			get subscriptionBreakReason() {
				return null as never;
			}
			get committed() {
				return null as never;
			}
			get handle() {
				return null as never;
			}
			sliced() {
				return this;
			}
		}
		const cb = new TestCallbackInboxStub();
		// Bad allocate throws.
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
				bad: ctx.allocateInvoke(svc, "echo", [cb]),
			})),
		).toThrow(WireEscapeError);
		// Next legitimate allocate should mint slot=1 (no slot was
		// consumed by the rejected call).
		store.set(session.userState, (_, ctx) => ({
			r: ctx.allocateInvoke(svc, "echo", [42]),
		}));
		await drainMicrotask();
		const op = (lastMutate().ops as MutationOp[]).find(
			(o): o is Extract<MutationOp, { kind: "invoke" }> => o.kind === "invoke",
		);
		expect(op?.slot).toBe(1);
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("throws if lifecycle is not connected", async () => {
		const { connector, store } = makeConnector();
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
		// Session is in `connecting` (subscribe sent, no snapshot
		// yet). The updater's allocateInvoke should throw.
		const svc = connector.serviceRef("svc-a");
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				r: ctx.allocateInvoke(svc, "echo", [1]),
			})),
		).toThrow(/connected/);
	});

	it("throws if session is closed / terminating / detaching", async () => {
		const { session, store, connector } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		// Close the underlying connection — drives the session to
		// `closed`.
		const detachPromise = session.detach();
		// detach is async; before it resolves the session is detaching
		// and the write API itself should refuse (assertWritable kicks
		// in before the updater ever runs, so the throw comes from
		// handleWrite's gate rather than from ctx.allocateInvoke).
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				r: ctx.allocateInvoke(svc, "echo", [1]),
			})),
		).toThrow(/detaching|closed/);
		await detachPromise;
		// And after detach completes, still refuses.
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				r: ctx.allocateInvoke(svc, "echo", [1]),
			})),
		).toThrow(/closed/);
	});

	it("emits Track ops for refs that appear in state alongside the invoke result stub", async () => {
		// We don't auto-track refs that appear ONLY in args, but if
		// the user writes a ref into state (next to the result stub
		// or otherwise), Track ops still fire as part of the normal
		// state-walk pipeline. This test confirms the invoke slot
		// path doesn't interfere with that.
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		const ent = connector.entityRef("ent-1");
		store.set(session.userState, (_, ctx) => ({
			r: ctx.allocateInvoke(svc, "echo", [ent]),
			e: ent,
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const ops = mut.ops as MutationOp[];
		// track_entity for the in-state ent.
		expect(ops).toContainEqual({ kind: "track_entity", short_id: "ent-1" });
		// No track_entity for refs-only-in-args (sanity: only one
		// entry).
		const trackEntities = ops.filter((o) => o.kind === "track_entity");
		expect(trackEntities).toHaveLength(1);
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("does NOT emit a track_service for the service ref that appears only in invoke args", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		store.set(session.userState, (_, ctx) => ({
			r: ctx.allocateInvoke(svc, "echo", []),
		}));
		// The svc is referenced by the invoke op (service_short_id)
		// but never appears in the user state tree, so no
		// track_service should be emitted.
		await drainMicrotask();
		const mut = lastMutate();
		const ops = mut.ops as MutationOp[];
		const trackServices = ops.filter((o) => o.kind === "track_service");
		expect(trackServices).toHaveLength(0);
		commitLast(subscription_id);
		await drainMicrotask();
	});
});

describe("WorkstationSession: invoke fill events (stage 3 chunk 3)", () => {
	it("invoke_result event flips stub.result to filled with the decoded payload", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		let stub!: OpResultStub<number>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateInvoke(svc, "echo", [42]) as OpResultStub<number>;
			return { r: stub };
		});
		const slot = (stub as WorkstationOpResultStub).slot;
		await drainMicrotask();
		expect((store.get(stub.result) as ResultStatus<number>).status).toBe("pending");

		commitLast(subscription_id);
		await drainMicrotask();

		// The Python side would echo 42 back; payload arrives as a
		// plain primitive (no wire escaping needed for primitives).
		deliverInvokeResult(subscription_id, slot, 42);

		const status = store.get(stub.result) as ResultStatus<number>;
		expect(status.status).toBe("filled");
		if (status.status === "filled") expect(status.value).toBe(42);
		expect(store.get(stub.done)).toBe(true);
		expect(store.get(stub.committed)).toBe(true);
	});

	it("invoke_error event flips stub.result to errored with the message", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		let stub!: OpResultStub<unknown>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateInvoke(svc, "fail", []);
			return { r: stub };
		});
		const slot = (stub as WorkstationOpResultStub).slot;
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		deliverInvokeError(subscription_id, slot, "boom");

		const status = store.get(stub.result) as ResultStatus<unknown>;
		expect(status.status).toBe("errored");
		if (status.status === "errored") expect(status.message).toBe("boom");
		expect(store.get(stub.done)).toBe(true);
	});

	it("invoke_result payload with refs decodes through unescapeWire", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		let stub!: OpResultStub<{ ref: unknown }>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateInvoke(svc, "echo", []) as OpResultStub<{ ref: unknown }>;
			return { r: stub };
		});
		const slot = (stub as WorkstationOpResultStub).slot;
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Wire payload: an object with an entity ref leaf, encoded
		// in the list-escape form.
		deliverInvokeResult(subscription_id, slot, { ref: ["entity", "ent-1"] });

		const status = store.get(stub.result) as ResultStatus<{ ref: unknown }>;
		expect(status.status).toBe("filled");
		if (status.status === "filled") {
			// `ent-1` interns to the same instance as a programmatic
			// `connector.entityRef("ent-1")`.
			const sameRef = connector.entityRef("ent-1");
			expect((status.value as { ref: unknown }).ref).toBe(sameRef);
		}
	});

	it("malformed invoke_result payload synthesizes an errored fill (decoder does not throw)", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		let stub!: OpResultStub<unknown>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateInvoke(svc, "echo", []);
			return { r: stub };
		});
		const slot = (stub as WorkstationOpResultStub).slot;
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Two-element list whose first entry is a non-reserved
		// string; the list-escape codec rejects this as malformed.
		deliverInvokeResult(subscription_id, slot, ["nope", "x"]);

		const status = store.get(stub.result) as ResultStatus<unknown>;
		expect(status.status).toBe("errored");
		if (status.status === "errored") expect(status.message).toMatch(/decode failed/);
		// Decode-error path logs through console.error.
		expect(errorSpy).toHaveBeenCalled();
	});

	it("fill-event tripwire fires for slot > localSlotCounter", async () => {
		const { subscription_id } = await setupConnectedSession();
		// No allocate; counter still at 0. Server "announces" an
		// invoke fill for slot 5 without a prior state_update →
		// invariant violation. The throw bubbles up through the
		// session's `onFrame`; the connection layer's try/catch
		// around handler calls routes it to `console.error`.
		deliverInvokeResult(subscription_id, 5, "anything");
		const found = errorSpy.mock.calls.some((args) => {
			const err = args[1];
			return err instanceof Error && err.message.includes("exceeds localSlotCounter");
		});
		expect(found).toBe(true);
	});

	it("snapshot rebuild surfaces preserved invoke_result events into slotFills", async () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();
		// Use attachSession to a known session id with a snapshot
		// carrying a preserved invoke result.
		const session = connector.attachSession("sess-x");
		const store = session.store;
		const subscribe = lastSent();
		const subscription_id = subscribe.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		// Snapshot: committed state carries an op_result slot=7
		// referencing the preserved invoke_result we ship in the
		// sidecar. `slot_counter=7` so the tripwire stays happy.
		const lastState: StateUpdateEvent = {
			kind: "state_update",
			seq: 10,
			ack_seq: 10,
			state: { user: { r: ["op_result", 7] } } as unknown as StateUpdateEvent["state"],
			closed: false,
			tracked_entities: [],
			tracked_services: [],
			slot_counter: 7,
		};
		const preservedFill: InvokeResultEvent = {
			kind: "invoke_result",
			seq: 9,
			slot: 7,
			payload: 123 as never,
		};
		const snapshot: SessionSnapshotEvent = {
			subscription_id,
			type: "session_snapshot",
			session_id: "sess-x",
			last_state: lastState,
			preserved_events: [preservedFill],
			subsequent_events: [],
		};
		currentMockSocket().simulateMessage(JSON.stringify(snapshot));

		// The session's userState should reflect the committed slot;
		// the stub there should report filled with the preserved
		// invoke payload.
		const user = store.get(session.userState) as { r: WorkstationOpResultStub };
		const stub = user.r;
		const status = store.get(stub.result) as ResultStatus<number>;
		expect(status.status).toBe("filled");
		if (status.status === "filled") expect(status.value).toBe(123);
		expect(store.get(stub.done)).toBe(true);
		expect(store.get(stub.committed)).toBe(true);
	});

	it("snapshot rebuild surfaces preserved invoke_error events into slotFills", async () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();
		const session = connector.attachSession("sess-y");
		const store = session.store;
		const subscribe = lastSent();
		const subscription_id = subscribe.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		const lastState: StateUpdateEvent = {
			kind: "state_update",
			seq: 4,
			ack_seq: 4,
			state: { user: { r: ["op_result", 2] } } as unknown as StateUpdateEvent["state"],
			closed: false,
			tracked_entities: [],
			tracked_services: [],
			slot_counter: 2,
		};
		const preservedErr: InvokeErrorEvent = {
			kind: "invoke_error",
			seq: 3,
			slot: 2,
			message: "preserved boom",
		};
		const snapshot: SessionSnapshotEvent = {
			subscription_id,
			type: "session_snapshot",
			session_id: "sess-y",
			last_state: lastState,
			preserved_events: [preservedErr],
			subsequent_events: [],
		};
		currentMockSocket().simulateMessage(JSON.stringify(snapshot));

		const user = store.get(session.userState) as { r: WorkstationOpResultStub };
		const stub = user.r;
		const status = store.get(stub.result) as ResultStatus<unknown>;
		expect(status.status).toBe("errored");
		if (status.status === "errored") expect(status.message).toBe("preserved boom");
	});
});

// --- Typed protocols (Option L) -----------------------------------

interface TestServiceProto {
	echo: Method<[number], Record<string, never>, number>;
	combine: Method<[string], { name: string }, { value: string; name: string }>;
	fail: Method<[string], Record<string, never>, never>;
}

describe("WorkstationSession.allocateInvoke: typed protocols (Option L)", () => {
	// Helper to extract the `R` type parameter from an
	// `OpResultStub<R>`. `expectTypeOf(...).toEqualTypeOf<OpResultStub<X>>()`
	// trips over the phantom brand, so we compare the unwrapped
	// return type instead — the only thing that actually matters
	// for typed-protocol consumers.
	type ResultOf<S> = S extends OpResultStub<infer R> ? R : never;

	it("infers return type from the protocol's Method declarations", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef<TestServiceProto>("svc-a");

		let echoStub!: OpResultStub<number>;
		let combineStub!: OpResultStub<{ value: string; name: string }>;
		let failStub!: OpResultStub<never>;
		store.set(session.userState, (_, ctx) => {
			echoStub = ctx.allocateInvoke(svc, "echo", [42]);
			expectTypeOf<ResultOf<typeof echoStub>>().toEqualTypeOf<number>();
			combineStub = ctx.allocateInvoke(svc, "combine", ["v"], { name: "alice" });
			expectTypeOf<ResultOf<typeof combineStub>>().toEqualTypeOf<{
				value: string;
				name: string;
			}>();
			failStub = ctx.allocateInvoke(svc, "fail", ["nope"]);
			expectTypeOf<ResultOf<typeof failStub>>().toEqualTypeOf<never>();
			return { e: echoStub, c: combineStub, f: failStub };
		});
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("an untyped (ServiceRefStub<unknown>) ref accepts any method name and yields OpResultStub<ResultValue>", async () => {
		const { session, store, connector } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a"); // ServiceRefStub<unknown>

		// Untyped path: `MethodNames<unknown>` widens to `string`,
		// so any method name compiles; args type falls back to
		// `readonly InvokeArgValue[]`; return type falls back to
		// `ResultValue` (payloads from `unescapeWire` for result
		// events). Consumers who want a narrower typed return should
		// declare a protocol; one-off untyped uses can cast.
		store.set(session.userState, (_, ctx) => {
			const stub = ctx.allocateInvoke(svc, "anything", [1, 2, 3]);
			expectTypeOf<ResultOf<typeof stub>>().toEqualTypeOf<ResultValue>();
			return { s: stub };
		});
	});

	it("typed overload rejects an unknown method name at compile time", async () => {
		const { session, store, connector } = await setupConnectedSession();
		const svc = connector.serviceRef<TestServiceProto>("svc-a");
		// `@ts-expect-error` asserts the following line fails to
		// typecheck. If the line ever starts compiling cleanly, TS
		// raises "Unused @ts-expect-error directive" instead — a
		// loud signal that the type safety regressed.
		store.set(session.userState, (_, ctx) => {
			// @ts-expect-error - "nope" is not a key of TestServiceProto
			ctx.allocateInvoke(svc, "nope", []);
			return null;
		});

		expectTypeOf<keyof TestServiceProto>().toEqualTypeOf<"echo" | "combine" | "fail">();
	});

	it("typed overload rejects mismatched arg types at compile time", async () => {
		const { session, store, connector } = await setupConnectedSession();
		const svc = connector.serviceRef<TestServiceProto>("svc-a");
		// echo's args type is `[number]`; passing a string should
		// fail to typecheck.
		store.set(session.userState, (_, ctx) => {
			// @ts-expect-error - args[0] must be `number`, got `string`
			ctx.allocateInvoke(svc, "echo", ["wrong"]);
			return null;
		});
	});

	it("typed overload rejects mismatched kwargs at compile time", async () => {
		const { session, store, connector } = await setupConnectedSession();
		const svc = connector.serviceRef<TestServiceProto>("svc-a");
		// combine's kwargs require `name: string`; passing a
		// number should fail.
		store.set(session.userState, (_, ctx) => {
			// @ts-expect-error - kwargs.name must be `string`, got `number`
			ctx.allocateInvoke(svc, "combine", ["v"], { name: 5 });
			return null;
		});
	});

	it("Method's Returns type is constrained to StateValue", () => {
		// Method's Returns extends StateValue — the runtime
		// return value always flows through the wire as a
		// StateValue, so declaring something incompatible (a
		// function, a class instance that isn't a stub, etc.) is a
		// type error at the declaration site.
		// @ts-expect-error - functions aren't a valid wire return shape
		type BadFn = Method<[], Record<string, never>, () => void>;
		// @ts-expect-error - symbols don't survive JSON serialization
		type BadSymbol = Method<[], Record<string, never>, symbol>;
		// Sanity: valid declarations still compile.
		type OkNumber = Method<[], Record<string, never>, number>;
		type OkObject = Method<[], Record<string, never>, { name: string }>;
		type OkNested = Method<[], Record<string, never>, { items: readonly number[] }>;
		// Reference each so `noUnusedLocals` is happy — the
		// `@ts-expect-error` directives above are what we actually
		// care about; these references just keep TS quiet.
		expectTypeOf<BadFn>().not.toBeNever();
		expectTypeOf<BadSymbol>().not.toBeNever();
		expectTypeOf<OkNumber>().not.toBeNever();
		expectTypeOf<OkObject>().not.toBeNever();
		expectTypeOf<OkNested>().not.toBeNever();
	});
});
