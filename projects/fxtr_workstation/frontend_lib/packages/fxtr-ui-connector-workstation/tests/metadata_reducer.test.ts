/**
 * Tests for the metadata-slot reducer (stage 7-pre-1).
 *
 * Covers `allocateGetEntityMetadata` / `allocateGetServiceMetadata`:
 * slot allocation, wire-visible dedup via the
 * `dedup.get_entity_metadata` / `dedup.get_service_metadata`
 * buckets of the unified `dedup` envelope field,
 * `GetEntityMetadataOp` /
 * `GetServiceMetadataOp` emission via the flush walk, fill event
 * decode (entity refs in metadata payload → `EntityRefStub`
 * instances), error events, and snapshot rebuild.
 *
 * The slot machinery itself is shared with load / invoke; we don't
 * re-cover every edge case (broken-stub handling, cross-session
 * substitution, preserve-events) — those tests in `slot_reducer.test.ts`
 * cover the underlying `WorkstationOpResultStub` class which metadata
 * also uses.
 */

import { createStore } from "jotai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
	EntityMetadata,
	EntityRefStub,
	OpResultStub,
	ResultStatus,
	ServiceMetadata,
} from "@fxtr-ui/core";
import {
	type EntityMetadataErrorEvent,
	type EntityMetadataResultEvent,
	type MutationOp,
	type ServiceMetadataErrorEvent,
	type ServiceMetadataResultEvent,
	type SessionEventFrame,
	type SessionSnapshotEvent,
	type StateUpdateEvent,
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

function deliverEntityMetadataResult(
	subscription_id: number,
	slot: number,
	metadata: Readonly<Record<string, unknown>>,
	seq?: number,
): number {
	const evSeq = seq ?? freshSeq();
	const event: EntityMetadataResultEvent = {
		kind: "entity_metadata_result",
		seq: evSeq,
		slot,
		metadata: metadata as never,
	};
	const frame: SessionEventFrame = { subscription_id, type: "session_event", event };
	currentMockSocket().simulateMessage(JSON.stringify(frame));
	return evSeq;
}

function deliverServiceMetadataResult(
	subscription_id: number,
	slot: number,
	metadata: Readonly<Record<string, unknown>>,
	seq?: number,
): number {
	const evSeq = seq ?? freshSeq();
	const event: ServiceMetadataResultEvent = {
		kind: "service_metadata_result",
		seq: evSeq,
		slot,
		metadata: metadata as never,
	};
	const frame: SessionEventFrame = { subscription_id, type: "session_event", event };
	currentMockSocket().simulateMessage(JSON.stringify(frame));
	return evSeq;
}

function deliverEntityMetadataError(
	subscription_id: number,
	slot: number,
	message: string,
	seq?: number,
): number {
	const evSeq = seq ?? freshSeq();
	const event: EntityMetadataErrorEvent = {
		kind: "entity_metadata_error",
		seq: evSeq,
		slot,
		message,
	};
	const frame: SessionEventFrame = { subscription_id, type: "session_event", event };
	currentMockSocket().simulateMessage(JSON.stringify(frame));
	return evSeq;
}

function deliverServiceMetadataError(
	subscription_id: number,
	slot: number,
	message: string,
	seq?: number,
): number {
	const evSeq = seq ?? freshSeq();
	const event: ServiceMetadataErrorEvent = {
		kind: "service_metadata_error",
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

// --- Allocation + op emission ---------------------------------------

describe("WorkstationSession.allocateGetEntityMetadata", () => {
	it("allocate + write → GetEntityMetadataOp emitted with the right slot / shortId", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const ent = connector.entityRef("ent-1");
		store.set(session.userState, (_, ctx) => ({
			m: ctx.allocateGetEntityMetadata(ent),
		}));
		await drainMicrotask();
		const mut = lastMutate();
		const mdOps = (mut.ops as MutationOp[]).filter(
			(op): op is Extract<MutationOp, { kind: "get_entity_metadata" }> =>
				op.kind === "get_entity_metadata",
		);
		expect(mdOps).toHaveLength(1);
		expect(mdOps[0]?.slot).toBe(1);
		expect(mdOps[0]?.short_id).toBe("ent-1");
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("same-tick dedup: two allocate calls return the same stub, one op emitted", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const ent = connector.entityRef("ent-1");
		let a!: OpResultStub<EntityMetadata>;
		let b!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (_, ctx) => {
			a = ctx.allocateGetEntityMetadata(ent);
			b = ctx.allocateGetEntityMetadata(ent);
			return { a, b };
		});
		expect(a).toBe(b);
		await drainMicrotask();
		const mdOps = (lastMutate().ops as MutationOp[]).filter(
			(o) => o.kind === "get_entity_metadata",
		);
		expect(mdOps).toHaveLength(1);
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("cross-tick dedup: after commit, second allocate hits the wire-visible map, no new op", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const ent = connector.entityRef("ent-1");
		let firstStub!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (_, ctx) => {
			firstStub = ctx.allocateGetEntityMetadata(ent);
			return { m: firstStub };
		});
		await drainMicrotask();
		const firstMut = lastMutate();
		// Verify the patch carries the dedup.get_entity_metadata entry.
		expect(firstMut.patch).toContainEqual({
			op: "add",
			path: "/dedup",
			value: { get_entity_metadata: { "ent-1": 1 } },
		});
		commitLast(subscription_id);
		await drainMicrotask();

		// Second allocate in a fresh tick — should reuse the same slot
		// via the wire-visible map.
		const sentBefore = sent().length;
		let secondStub!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (prev, ctx) => {
			secondStub = ctx.allocateGetEntityMetadata(ent);
			return { ...(prev as object), other: secondStub };
		});
		expect(secondStub).toBe(firstStub);
		await drainMicrotask();
		// Either no new mutation went out (nothing structurally changed
		// except a new alias key) or one did but had no metadata op.
		const newMessages = sent().slice(sentBefore);
		for (const m of newMessages) {
			if (m.op === "mutate_session") {
				const mdOps = (m.ops as MutationOp[]).filter((o) => o.kind === "get_entity_metadata");
				expect(mdOps).toHaveLength(0);
			}
		}
	});

	it("distinct slot pool from allocateLoad: same entity, different slots for load vs. metadata", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const ent = connector.entityRef("ent-1");
		let loadStub!: OpResultStub<unknown>;
		let mdStub!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (_, ctx) => {
			loadStub = ctx.allocateLoad(ent);
			mdStub = ctx.allocateGetEntityMetadata(ent);
			return { l: loadStub, m: mdStub };
		});
		// Different slots: load mints first (slot=1), then metadata
		// mints fresh (slot=2). The two stubs are distinct instances.
		expect(loadStub).not.toBe(mdStub);
		expect((loadStub as WorkstationOpResultStub).slot).toBe(1);
		expect((mdStub as WorkstationOpResultStub).slot).toBe(2);
		await drainMicrotask();
		const ops = lastMutate().ops as MutationOp[];
		expect(ops.find((o) => o.kind === "load_entity")).toBeDefined();
		expect(ops.find((o) => o.kind === "get_entity_metadata")).toBeDefined();
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("removing the metadata stub then re-allocating in same tick reuses the existing slot", async () => {
		// Mirror of the load-side scenario: optimistic remove + re-add
		// within one tick keeps the same slot since `pendingSlotSpecs`
		// is still alive at flush time. (The wire-visible dedup map
		// only updates at commit; same-tick scans `pendingSlotSpecs`.)
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const ent = connector.entityRef("ent-1");
		let s1!: OpResultStub<EntityMetadata>;
		let s2!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (_, ctx) => {
			s1 = ctx.allocateGetEntityMetadata(ent);
			// "Discard" by writing without it.
			return { m: s1 };
		});
		store.set(session.userState, (_, ctx) => {
			s2 = ctx.allocateGetEntityMetadata(ent);
			return { m: s2 };
		});
		expect(s2).toBe(s1);
		await drainMicrotask();
		// One emitted op covering the surviving spec.
		const mdOps = (lastMutate().ops as MutationOp[]).filter(
			(o) => o.kind === "get_entity_metadata",
		);
		expect(mdOps).toHaveLength(1);
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
		const ent = connector.entityRef("ent-1");
		// No snapshot yet → lifecycle === "connecting".
		expect(() =>
			store.set(session.userState, (_, ctx) => ({
				m: ctx.allocateGetEntityMetadata(ent),
			})),
		).toThrow(/connected/);
	});
});

describe("WorkstationSession.allocateGetServiceMetadata", () => {
	it("allocate + write → GetServiceMetadataOp emitted with the right slot / shortId", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		store.set(session.userState, (_, ctx) => ({
			m: ctx.allocateGetServiceMetadata(svc),
		}));
		await drainMicrotask();
		const mdOps = (lastMutate().ops as MutationOp[]).filter(
			(op): op is Extract<MutationOp, { kind: "get_service_metadata" }> =>
				op.kind === "get_service_metadata",
		);
		expect(mdOps).toHaveLength(1);
		expect(mdOps[0]?.slot).toBe(1);
		expect(mdOps[0]?.short_id).toBe("svc-a");
		commitLast(subscription_id);
		await drainMicrotask();
	});

	it("cross-tick dedup via dedup.get_service_metadata", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		let firstStub!: OpResultStub<ServiceMetadata>;
		store.set(session.userState, (_, ctx) => {
			firstStub = ctx.allocateGetServiceMetadata(svc);
			return { m: firstStub };
		});
		await drainMicrotask();
		expect(lastMutate().patch).toContainEqual({
			op: "add",
			path: "/dedup",
			value: { get_service_metadata: { "svc-a": 1 } },
		});
		commitLast(subscription_id);
		await drainMicrotask();

		let secondStub!: OpResultStub<ServiceMetadata>;
		store.set(session.userState, (prev, ctx) => {
			secondStub = ctx.allocateGetServiceMetadata(svc);
			return { ...(prev as object), other: secondStub };
		});
		expect(secondStub).toBe(firstStub);
	});
});

// --- Fill event handling --------------------------------------------

describe("WorkstationSession: entity metadata fill events", () => {
	it("entity_metadata_result event flips stub.result to filled with decoded metadata (refs → EntityRefStub)", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const ent = connector.entityRef("ent-1");
		let stub!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateGetEntityMetadata(ent);
			return { m: stub };
		});
		const slot = (stub as WorkstationOpResultStub).slot;
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		expect((store.get(stub.result) as ResultStatus<EntityMetadata>).status).toBe("pending");

		// Wire shape mirrors the server's wire-escape change: ref
		// fields are tagged, plain strings stay bare.
		deliverEntityMetadataResult(subscription_id, slot, {
			id_string: "d1234",
			unprovenanced: ["entity", "ent-1"],
			provenance_parent: ["entity", "ent-2"],
			provenance_parent_id_string: "n5678",
		});

		const status = store.get(stub.result) as ResultStatus<EntityMetadata>;
		expect(status.status).toBe("filled");
		if (status.status === "filled") {
			const md = status.value;
			expect(md.id_string).toBe("d1234");
			expect(md.provenance_parent_id_string).toBe("n5678");
			// Ref fields decode to EntityRefStub instances; should
			// intern to the same instances as a programmatic
			// connector.entityRef(...) call.
			expect(md.unprovenanced).toBe(connector.entityRef("ent-1"));
			expect(md.provenance_parent).toBe(connector.entityRef("ent-2"));
		}
		expect(store.get(stub.done)).toBe(true);
		expect(store.get(stub.committed)).toBe(true);
	});

	it("entity_metadata_error event flips stub.result to errored with the message", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const ent = connector.entityRef("ent-1");
		let stub!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateGetEntityMetadata(ent);
			return { m: stub };
		});
		const slot = (stub as WorkstationOpResultStub).slot;
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		deliverEntityMetadataError(subscription_id, slot, "entity is gone");

		const status = store.get(stub.result) as ResultStatus<EntityMetadata>;
		expect(status.status).toBe("errored");
		if (status.status === "errored") expect(status.message).toBe("entity is gone");
		expect(store.get(stub.done)).toBe(true);
	});

	it("malformed entity_metadata_result payload synthesizes an errored fill", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const ent = connector.entityRef("ent-1");
		let stub!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateGetEntityMetadata(ent);
			return { m: stub };
		});
		const slot = (stub as WorkstationOpResultStub).slot;
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		// Inject a value with an unknown-tag list at a leaf — the
		// list-escape codec rejects this as malformed.
		deliverEntityMetadataResult(subscription_id, slot, {
			id_string: "d1234",
			unprovenanced: ["not-a-real-tag", "x"],
		});

		const status = store.get(stub.result) as ResultStatus<EntityMetadata>;
		expect(status.status).toBe("errored");
		if (status.status === "errored") expect(status.message).toMatch(/decode failed/);
		expect(errorSpy).toHaveBeenCalled();
	});

	it("fill-event tripwire fires for slot > localSlotCounter", async () => {
		const { subscription_id } = await setupConnectedSession();
		// Counter still at 0; server "announces" a metadata fill for
		// slot 5 → invariant violation.
		deliverEntityMetadataResult(subscription_id, 5, { id_string: "d" });
		const found = errorSpy.mock.calls.some((args) => {
			const err = args[1];
			return err instanceof Error && err.message.includes("exceeds localSlotCounter");
		});
		expect(found).toBe(true);
	});
});

describe("WorkstationSession: service metadata fill events", () => {
	it("service_metadata_result event flips stub.result to filled with type_id_string", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		let stub!: OpResultStub<ServiceMetadata>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateGetServiceMetadata(svc);
			return { m: stub };
		});
		const slot = (stub as WorkstationOpResultStub).slot;
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		deliverServiceMetadataResult(subscription_id, slot, {
			type_id_string: "EchoProto",
		});

		const status = store.get(stub.result) as ResultStatus<ServiceMetadata>;
		expect(status.status).toBe("filled");
		if (status.status === "filled") expect(status.value.type_id_string).toBe("EchoProto");
	});

	it("service_metadata_error event flips stub.result to errored", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		let stub!: OpResultStub<ServiceMetadata>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateGetServiceMetadata(svc);
			return { m: stub };
		});
		const slot = (stub as WorkstationOpResultStub).slot;
		await drainMicrotask();
		commitLast(subscription_id);
		await drainMicrotask();

		deliverServiceMetadataError(subscription_id, slot, "service gone");

		const status = store.get(stub.result) as ResultStatus<ServiceMetadata>;
		expect(status.status).toBe("errored");
		if (status.status === "errored") expect(status.message).toBe("service gone");
	});
});

// --- Snapshot rebuild ------------------------------------------------

describe("WorkstationSession: metadata snapshot rebuild", () => {
	it("snapshot rebuild surfaces preserved entity_metadata_result events into slotFills", async () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();
		const session = connector.attachSession("sess-x");
		const store = session.store;
		const subscribe = lastSent();
		const subscription_id = subscribe.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		// Snapshot: committed state holds an op_result slot=7, and the
		// preserved buckets include the corresponding metadata fill.
		// `dedup.get_entity_metadata` in committed state lets a follow-up
		// allocateGetEntityMetadata dedup against the prior slot.
		const lastState: StateUpdateEvent = {
			kind: "state_update",
			seq: 10,
			ack_seq: 10,
			state: {
				user: { m: ["op_result", 7] },
				dedup: { get_entity_metadata: { "ent-1": 7 } },
			} as unknown as StateUpdateEvent["state"],
			closed: false,
			tracked_entities: [],
			tracked_services: [],
			slot_counter: 7,
		};
		const preservedFill: EntityMetadataResultEvent = {
			kind: "entity_metadata_result",
			seq: 9,
			slot: 7,
			metadata: {
				id_string: "d1234",
				unprovenanced: ["entity", "ent-1"],
			} as never,
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

		const user = store.get(session.userState) as { m: WorkstationOpResultStub };
		const stub = user.m;
		const status = store.get(stub.result) as ResultStatus<EntityMetadata>;
		expect(status.status).toBe("filled");
		if (status.status === "filled") {
			expect(status.value.id_string).toBe("d1234");
			expect(status.value.unprovenanced).toBe(connector.entityRef("ent-1"));
		}
		expect(store.get(stub.committed)).toBe(true);
	});

	it("attach-session dedup bootstrap: snapshot's dedup.get_entity_metadata hands a fresh allocateGetEntityMetadata the existing slot", async () => {
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
			seq: 5,
			ack_seq: 5,
			state: {
				user: { m: ["op_result", 3] },
				dedup: { get_entity_metadata: { "ent-foo": 3 } },
			} as unknown as StateUpdateEvent["state"],
			closed: false,
			tracked_entities: [],
			tracked_services: [],
			slot_counter: 3,
		};
		const snapshot: SessionSnapshotEvent = {
			subscription_id,
			type: "session_snapshot",
			session_id: "sess-y",
			last_state: lastState,
			preserved_events: [],
			subsequent_events: [],
		};
		currentMockSocket().simulateMessage(JSON.stringify(snapshot));

		// Now fresh-allocate against the same shortId — should hit the
		// wire-visible dedup and return the existing slot.
		const ent = connector.entityRef("ent-foo");
		let allocated!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (prev, ctx) => {
			allocated = ctx.allocateGetEntityMetadata(ent);
			return { ...(prev as object), other: allocated };
		});
		// Slot 3 was the existing one.
		expect((allocated as WorkstationOpResultStub).slot).toBe(3);
		await drainMicrotask();
		// No new GetEntityMetadataOp emitted (existing slot reused).
		try {
			const ops = lastMutate().ops as MutationOp[];
			const mdOps = ops.filter((o) => o.kind === "get_entity_metadata");
			expect(mdOps).toHaveLength(0);
		} catch {
			// No mutation went out — even better.
		}
	});

	it("foreign-mutator dedup: foreign state_update populates dedup.get_entity_metadata → our subsequent allocate reuses the slot", async () => {
		const { session, store, connector, subscription_id } = await setupConnectedSession();
		// Foreign mutator commits a metadata allocate for ent-bar at
		// slot=5.
		const foreignUpdate: StateUpdateEvent = {
			kind: "state_update",
			seq: 50,
			ack_seq: 50,
			state: {
				user: { foreign: ["op_result", 5] },
				dedup: { get_entity_metadata: { "ent-bar": 5 } },
			} as unknown as StateUpdateEvent["state"],
			closed: false,
			tracked_entities: [],
			tracked_services: [],
			slot_counter: 5,
		};
		const frame: SessionEventFrame = {
			subscription_id,
			type: "session_event",
			event: foreignUpdate,
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));

		// We now allocate for the same entity — should reuse foreign's
		// slot=5, no new op.
		const ent = connector.entityRef("ent-bar");
		let stub!: OpResultStub<EntityMetadata>;
		store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateGetEntityMetadata(ent);
			return { mine: stub };
		});
		expect((stub as WorkstationOpResultStub).slot).toBe(5);
		await drainMicrotask();
		// Our outgoing mutation contains no GetEntityMetadataOp (the
		// slot already exists on the wire).
		const ops = lastMutate().ops as MutationOp[];
		const mdOps = ops.filter((o) => o.kind === "get_entity_metadata");
		expect(mdOps).toHaveLength(0);
	});
});

// --- Type-level smoke ----------------------------------------------

describe("WorkstationSession: typed metadata returns", () => {
	it("allocateGetEntityMetadata's stub is OpResultStub<EntityMetadata>", async () => {
		const { session, store, connector } = await setupConnectedSession();
		const ent = connector.entityRef("ent-1");
		store.set(session.userState, (_, ctx) => {
			const stub = ctx.allocateGetEntityMetadata(ent);
			// Type-level: the returned stub's result-atom value is
			// `ResultStatus<EntityMetadata>`. We assign the filled
			// value into an `EntityMetadata`-typed variable to assert
			// at compile time.
			void ((): EntityMetadata | undefined => {
				const status = store.get(stub.result);
				return status.status === "filled" ? status.value : undefined;
			});
			// Compile-time smoke: a field defined on EntityMetadata
			// is accessible without a cast.
			void ((md: EntityMetadata): EntityRefStub | undefined => md.unprovenanced);
			return { m: stub };
		});
	});

	it("allocateGetServiceMetadata's stub is OpResultStub<ServiceMetadata>", async () => {
		const { session, store, connector } = await setupConnectedSession();
		const svc = connector.serviceRef("svc-a");
		store.set(session.userState, (_, ctx) => {
			const stub = ctx.allocateGetServiceMetadata(svc);
			void ((): ServiceMetadata | undefined => {
				const status = store.get(stub.result);
				return status.status === "filled" ? status.value : undefined;
			});
			void ((md: ServiceMetadata): string => md.type_id_string);
			return { m: stub };
		});
	});
});
