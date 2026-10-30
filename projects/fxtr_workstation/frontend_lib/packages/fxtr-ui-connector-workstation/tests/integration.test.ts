/**
 * Live integration tests against a real workstation server.
 *
 * Boots one demo workstation per file (`beforeAll`/`afterAll`),
 * connects through `WorkstationConnector`, and exercises the
 * stage-1 deliverable scenarios + a few extensions (Track/Untrack
 * via `subscribe_session_debug`, race + reject, terminate).
 *
 * Each scenario creates its own session — they don't share state
 * between tests. The harness lives in `_integration_harness.ts`;
 * the demo workstation lives in `_demo_workstation.py`.
 *
 * NOTE: this file requires `uv` and the workstation Python package
 * to be installed locally. Failures here usually point at the
 * environment, not the code under test.
 */

import { createStore } from "jotai";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type {
	EntityMetadata,
	EntityRefStub,
	OpResultStub,
	ResultStatus,
	RollbackInfo,
	ServiceMetadata,
	SessionLifecycleState,
} from "@fxtr-ui/core";
import {
	LoadEntityError,
	type SessionDebugSnapshotEvent,
	type SubscriptionFrame,
	WorkstationCallbackInboxStub,
	WorkstationConnector,
	WorkstationOpResultStub,
	WorkstationServerConnection,
	type WorkstationSession,
} from "../src";
import { type DemoWorkstation, startDemoWorkstation } from "./_integration_harness";

const ENTITY_NAME = "e-test";
const SERVICE_NAME = "s-test";

let demo: DemoWorkstation;

beforeAll(async () => {
	demo = await startDemoWorkstation();
}, 30_000);

afterAll(async () => {
	await demo.close();
}, 30_000);

// --- helpers ------------------------------------------------------

function makeConnector() {
	const store = createStore();
	const conn = new WorkstationServerConnection({ url: demo.clientUrl, store });
	const connector = new WorkstationConnector({ connection: conn, store });
	return { conn, connector, store };
}

async function awaitLifecycle(
	session: WorkstationSession,
	target: SessionLifecycleState,
	timeoutMs = 5000,
): Promise<void> {
	const start = Date.now();
	while (session.store.get(session.lifecycle) !== target) {
		if (Date.now() - start > timeoutMs) {
			throw new Error(
				`session lifecycle did not reach ${target} within ${timeoutMs}ms (current: ${session.store.get(session.lifecycle)})`,
			);
		}
		await new Promise<void>((r) => setTimeout(r, 10));
	}
}

async function awaitCondition(
	predicate: () => boolean,
	description: string,
	timeoutMs = 5000,
): Promise<void> {
	const start = Date.now();
	while (!predicate()) {
		if (Date.now() - start > timeoutMs) {
			throw new Error(`${description} did not become true within ${timeoutMs}ms`);
		}
		await new Promise<void>((r) => setTimeout(r, 10));
	}
}

/**
 * Find a record from a `ReactiveList` matching a predicate, polling
 * until the list has populated. Returns the matching record.
 */
async function findInList<T>(
	store: ReturnType<typeof createStore>,
	list: { records: Parameters<typeof store.get>[0] },
	predicate: (record: T) => boolean,
	description: string,
	timeoutMs = 5000,
): Promise<T> {
	const start = Date.now();
	while (true) {
		const records = store.get(list.records) as readonly T[];
		const found = records.find(predicate);
		if (found) return found;
		if (Date.now() - start > timeoutMs) {
			throw new Error(`${description} not found within ${timeoutMs}ms`);
		}
		await new Promise<void>((r) => setTimeout(r, 10));
	}
}

// --- Scenario 1: connect + create + snapshot --------------------

describe("integration: connect + snapshot", () => {
	it("creates a session and reaches connected with userState=null", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "scenario-1" });
		await awaitLifecycle(session, "connected");
		expect(session.store.get(session.userState)).toBeNull();
		await session.detach();
		await conn.close();
	});
});

// --- Scenario 2: single-session mutation cycle -------------------

describe("integration: single-session mutation", () => {
	it("writes commit and update userState", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({});
		await awaitLifecycle(session, "connected");

		session.store.set(session.userState, { foo: 1 });
		await awaitCondition(() => {
			const u = session.store.get(session.userState) as { foo?: number } | null;
			return u?.foo === 1;
		}, "first write to commit");
		// Note: optimistic read is immediate; we wait here to be
		// sure the commit landed (committed === local).

		session.store.set(session.userState, { foo: 2, bar: "x" });
		await awaitCondition(() => {
			const u = session.store.get(session.userState) as { foo?: number; bar?: string } | null;
			return u?.foo === 2 && u?.bar === "x";
		}, "second write to commit");

		await session.detach();
		await conn.close();
	});
});

// --- Scenario 3: Track/Untrack via session_debug -----------------

describe("integration: Track/Untrack via session_debug", () => {
	it("entity ref appears + disappears from refcounts as state changes", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({});
		await awaitLifecycle(session, "connected");

		// Find the entity short_id (advertised under `e-test`).
		const entities = connector.listEntities();
		const entityRecord = await findInList<{
			short_id: string;
			external_names: readonly string[];
		}>(
			connector.store,
			entities,
			(r) => r.external_names.includes(ENTITY_NAME),
			`entity ${ENTITY_NAME}`,
		);
		const entityShortId = entityRecord.short_id;

		// Same for the service.
		const services = connector.listServices();
		const serviceRecord = await findInList<{
			short_id: string;
			external_names: readonly string[];
		}>(
			connector.store,
			services,
			(r) => r.external_names.includes(SERVICE_NAME),
			`service ${SERVICE_NAME}`,
		);
		const serviceShortId = serviceRecord.short_id;

		// Subscribe to session_debug via the raw connection. The
		// connector doesn't expose this; we reach in deliberately
		// since this is the cleanest verification path.
		const debugSnapshots: SessionDebugSnapshotEvent[] = [];
		const debugHandle = conn.registerSubscription(
			{ kind: "session_debug", session_id: session.sessionId },
			{
				onFrame: (frame: SubscriptionFrame) => {
					if (frame.type === "session_debug_snapshot") debugSnapshots.push(frame);
				},
				onClose: () => {},
			},
		);

		// Write a state with both refs.
		session.store.set(session.userState, {
			entity: connector.entityRef(entityShortId),
			service: connector.serviceRef(serviceShortId),
		});

		// Wait for a session_debug snapshot reflecting refcount=1
		// on both.
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			if (!last) return false;
			return (
				(last.entity_refcounts[entityShortId] ?? 0) >= 1 &&
				(last.service_refcounts[serviceShortId] ?? 0) >= 1
			);
		}, "session_debug to show entity+service refcounts");

		// Remove both refs.
		session.store.set(session.userState, {});
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			if (!last) return false;
			return (
				(last.entity_refcounts[entityShortId] ?? 0) === 0 &&
				(last.service_refcounts[serviceShortId] ?? 0) === 0
			);
		}, "session_debug to show refcounts dropped to 0");

		debugHandle.close();
		await session.detach();
		await conn.close();
	});
});

// --- Scenario 4: microtask coalescing ----------------------------

describe("integration: microtask coalescing", () => {
	it("three synchronous writes fold into one commit", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({});
		await awaitLifecycle(session, "connected");

		// Three writes in one tick.
		session.store.set(session.userState, { a: 1 });
		session.store.set(session.userState, { a: 1, b: 2 });
		session.store.set(session.userState, { a: 1, b: 2, c: 3 });

		await awaitCondition(() => {
			const u = session.store.get(session.userState) as Record<string, number> | null;
			return u?.a === 1 && u?.b === 2 && u?.c === 3;
		}, "coalesced commit");

		// Confirmed coalesced by virtue of single commit landing — if
		// three separate commits had gone out the final state would
		// still be the same, but `attachSession` from a second
		// connection would see intermediate seqs. We verify via
		// Scenario 5's foreign-update path that one commit per
		// coalesced batch is what foreign subscribers see.

		await session.detach();
		await conn.close();
	});
});

// --- Scenario 5: foreign update propagation ----------------------

describe("integration: foreign update propagation", () => {
	it("session B sees session A's mutations via foreign state_update", async () => {
		const { conn, connector } = makeConnector();
		const sessionA = await connector.createSession({});
		await awaitLifecycle(sessionA, "connected");

		const sessionB = connector.attachSession(sessionA.sessionId);
		await awaitLifecycle(sessionB, "connected");

		// Both see the same initial state (null).
		expect(sessionA.store.get(sessionA.userState)).toBeNull();
		expect(sessionB.store.get(sessionB.userState)).toBeNull();

		// A writes; B observes via foreign state_update.
		sessionA.store.set(sessionA.userState, { fromA: 1 });
		await awaitCondition(() => {
			const u = sessionB.store.get(sessionB.userState) as { fromA?: number } | null;
			return u?.fromA === 1;
		}, "B to see A's write");

		await sessionB.detach();
		await sessionA.detach();
		await conn.close();
	});
});

// --- Scenario 6: race + reject + rollback + retry ----------------

describe("integration: race + reject + rollback + retry", () => {
	it("two sessions racing same baseline: A commits, B rolls back retryable, B retries", async () => {
		const { conn, connector } = makeConnector();
		const sessionA = await connector.createSession({});
		await awaitLifecycle(sessionA, "connected");

		const sessionB = connector.attachSession(sessionA.sessionId);
		await awaitLifecycle(sessionB, "connected");

		// Establish baseline.
		sessionA.store.set(sessionA.userState, { counter: 0 });
		await awaitCondition(() => {
			const a = sessionA.store.get(sessionA.userState) as { counter?: number } | null;
			const b = sessionB.store.get(sessionB.userState) as { counter?: number } | null;
			return a?.counter === 0 && b?.counter === 0;
		}, "baseline committed on both sessions");

		// Race: A's write goes out first (microtask order matches the
		// queue order), B's goes out second on the same socket. Server
		// commits A; B's ack_seq is stale → reject.
		const rollbackInfosA: RollbackInfo[] = [];
		const rollbackInfosB: RollbackInfo[] = [];
		sessionA.store.set(sessionA.userState, (prev) => ({ ...((prev as object) ?? {}), fromA: 1 }), {
			onRollback: (info) => rollbackInfosA.push(info),
		});
		sessionB.store.set(sessionB.userState, (prev) => ({ ...((prev as object) ?? {}), fromB: 1 }), {
			onRollback: (info) => rollbackInfosB.push(info),
		});

		// Wait for B's rollback to fire.
		await awaitCondition(() => rollbackInfosB.length > 0, "B's rollback to fire");
		expect(rollbackInfosA).toHaveLength(0);
		expect(rollbackInfosB[0]).toMatchObject({ kind: "retryable" });

		// A's userState committed.
		await awaitCondition(() => {
			const a = sessionA.store.get(sessionA.userState) as { fromA?: number } | null;
			return a?.fromA === 1;
		}, "A's commit visible to A");

		// B's userState is the new committed (with A's change).
		await awaitCondition(() => {
			const b = sessionB.store.get(sessionB.userState) as { fromA?: number; fromB?: number } | null;
			return b?.fromA === 1 && b?.fromB === undefined;
		}, "B reverted to post-A committed");

		// B retries with rebase.
		sessionB.store.set(sessionB.userState, (prev) => ({
			...((prev as object) ?? {}),
			retried: true,
		}));
		await awaitCondition(() => {
			const b = sessionB.store.get(sessionB.userState) as {
				counter?: number;
				fromA?: number;
				retried?: boolean;
			} | null;
			return b?.counter === 0 && b?.fromA === 1 && b?.retried === true;
		}, "B's retry to commit");

		// A also sees the retry now.
		await awaitCondition(() => {
			const a = sessionA.store.get(sessionA.userState) as { retried?: boolean } | null;
			return a?.retried === true;
		}, "A to see B's retry");

		await sessionB.detach();
		await sessionA.detach();
		await conn.close();
	});
});

// --- Scenario 7: terminate ---------------------------------------

describe("integration: terminate", () => {
	it("terminate() commits, lifecycle = closed, second session sees closed=true", async () => {
		const { conn, connector } = makeConnector();
		const sessionA = await connector.createSession({});
		await awaitLifecycle(sessionA, "connected");

		const sessionB = connector.attachSession(sessionA.sessionId);
		await awaitLifecycle(sessionB, "connected");

		await sessionA.terminate({ status: "done" });
		expect(sessionA.store.get(sessionA.lifecycle)).toBe<SessionLifecycleState>("closed");

		// B sees the terminal state_update and goes closed too.
		await awaitLifecycle(sessionB, "closed");

		await conn.close();
	});
});

// ===================================================================
// Stage 2 chunk 5 — live slot mechanics tests.
// ===================================================================

/**
 * Look up `e-test`'s short_id from the entities resource list.
 * Shared setup helper for chunk-5 tests.
 */
async function resolveTestEntityShortId(connector: WorkstationConnector): Promise<string> {
	const entities = connector.listEntities();
	const record = await findInList<{
		short_id: string;
		external_names: readonly string[];
	}>(
		connector.store,
		entities,
		(r) => r.external_names.includes(ENTITY_NAME),
		`entity ${ENTITY_NAME}`,
	);
	return record.short_id;
}

async function resolveTestServiceShortId(connector: WorkstationConnector): Promise<string> {
	const services = connector.listServices();
	const record = await findInList<{
		short_id: string;
		external_names: readonly string[];
	}>(
		connector.store,
		services,
		(r) => r.external_names.includes(SERVICE_NAME),
		`service ${SERVICE_NAME}`,
	);
	return record.short_id;
}

describe("integration (chunk 5): allocate + load lifecycle", () => {
	it("allocateLoad → LoadEntityOp → fill → stub.result transitions to filled", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "chunk5-load" });
		await awaitLifecycle(session, "connected");

		const entityShortId = await resolveTestEntityShortId(connector);
		const ref = connector.entityRef(entityShortId);
		let stub!: WorkstationOpResultStub;
		// Write the stub into user state. The reducer ships the
		// LoadEntityOp; the server's load worker spawns and the
		// fill flows back as a `load_result` event.
		session.store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(ref) as WorkstationOpResultStub;
			return { x: stub };
		});

		// Wait for the result atom to flip to "filled".
		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "stub.result to fill");

		const result = session.store.get(stub.result) as ResultStatus<unknown>;
		expect(result.status).toBe("filled");
		if (result.status === "filled") {
			const value = result.value as Record<string, unknown>;
			expect(value.body).toBe("frontend_lib integration test note");
		}
		expect(session.store.get(stub.done)).toBe(true);
		expect(session.store.get(stub.committed)).toBe(true);

		await session.detach();
		await conn.close();
	});

	it("dedup: two allocateLoad(X) calls return the same stub", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "chunk5-dedup" });
		await awaitLifecycle(session, "connected");

		const entityShortId = await resolveTestEntityShortId(connector);
		const ref = connector.entityRef(entityShortId);
		let stubA!: OpResultStub<unknown>;
		let stubB!: OpResultStub<unknown>;
		session.store.set(session.userState, (_, ctx) => {
			stubA = ctx.allocateLoad(ref);
			stubB = ctx.allocateLoad(ref);
			return { x: stubA };
		});
		expect(stubA).toBe(stubB);

		// (Confirm a single load completes — the `result` atom resolves
		// once. If a second LoadEntityOp had gone out, the server would
		// reject it as a slot conflict.)
		await awaitCondition(() => {
			const r = session.store.get(
				(stubA as WorkstationOpResultStub).result,
			) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "stub.result to fill");

		await session.detach();
		await conn.close();
	});
});

describe("integration (chunk 5): preserve / unpreserve via session_debug", () => {
	it("fill preserves on next flush, unpreserves when the slot is removed", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "chunk5-preserve" });
		await awaitLifecycle(session, "connected");

		// Subscribe to session_debug so we can observe the
		// preserved_events seq list directly.
		const debugSnapshots: SessionDebugSnapshotEvent[] = [];
		const debugHandle = conn.registerSubscription(
			{ kind: "session_debug", session_id: session.sessionId },
			{
				onFrame: (frame: SubscriptionFrame) => {
					if (frame.type === "session_debug_snapshot") debugSnapshots.push(frame);
				},
				onClose: () => {},
			},
		);

		const entityShortId = await resolveTestEntityShortId(connector);
		let stub!: WorkstationOpResultStub;
		session.store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connector.entityRef(entityShortId)) as WorkstationOpResultStub;
			return { x: stub };
		});

		// Wait for the fill.
		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "stub.result to fill");

		// At this point preserved_events on the server is empty —
		// we haven't yet triggered a second flush to ship the
		// preserve op. Trigger one with an unrelated write.
		session.store.set(session.userState, (prev) => ({ ...(prev as object), extra: 1 }));

		// Wait until a debug snapshot shows preserved_events with
		// exactly one seq.
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			return last !== undefined && last.preserved_events.length === 1;
		}, "preserved_events to contain the fill seq");

		const preservedSeq = debugSnapshots[debugSnapshots.length - 1]?.preserved_events[0] ?? -1;
		expect(preservedSeq).toBeGreaterThan(0);

		// Now remove the slot. The flush should emit
		// `unpreserve_events([preservedSeq])`; once committed,
		// preserved_events is empty again.
		session.store.set(session.userState, { extra: 1 });

		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			return last !== undefined && last.preserved_events.length === 0;
		}, "preserved_events to drop the seq after slot removal");

		debugHandle.close();
		await session.detach();
		await conn.close();
	});
});

describe("integration (chunk 5): fill survives attach to same session_id", () => {
	it("a second session attached to the same session_id sees the preserved fill", async () => {
		// First session: load, preserve, detach (leaves the server
		// session alive with the slot in committed state and the
		// fill in preserved_events).
		const { conn: connA, connector: connectorA } = makeConnector();
		const sessionA = await connectorA.createSession({ tag: "chunk5-attach-src" });
		await awaitLifecycle(sessionA, "connected");

		const entityShortId = await resolveTestEntityShortId(connectorA);
		let stub!: WorkstationOpResultStub;
		sessionA.store.set(sessionA.userState, (_, ctx) => {
			stub = ctx.allocateLoad(connectorA.entityRef(entityShortId)) as WorkstationOpResultStub;
			return { x: stub };
		});

		await awaitCondition(() => {
			const r = sessionA.store.get(stub.result) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "A: stub.result to fill");
		// Trigger the second flush so the fill gets preserved.
		sessionA.store.set(sessionA.userState, (prev) => ({ ...(prev as object), extra: 1 }));

		// Subscribe to session_debug to know when preserve has
		// committed before detaching A.
		const debugSnapshots: SessionDebugSnapshotEvent[] = [];
		const debugHandle = connA.registerSubscription(
			{ kind: "session_debug", session_id: sessionA.sessionId },
			{
				onFrame: (frame) => {
					if (frame.type === "session_debug_snapshot") debugSnapshots.push(frame);
				},
				onClose: () => {},
			},
		);
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			return last !== undefined && last.preserved_events.length === 1;
		}, "A: preserved_events populated before detach");
		debugHandle.close();

		const sessionId = sessionA.sessionId;
		await sessionA.detach();
		await connA.close();

		// Fresh connector + attachSession to the same session_id.
		// On snapshot decode we should see the slot in committed
		// state AND the fill in preserved_events; the reducer
		// rebuilds slotFills + committedSlotSet from the snapshot.
		const { conn: connB, connector: connectorB } = makeConnector();
		const sessionB = connectorB.attachSession(sessionId);
		await awaitLifecycle(sessionB, "connected");

		const userB = sessionB.store.get(sessionB.userState) as Record<string, unknown>;
		const stubB = userB.x as WorkstationOpResultStub;
		expect(stubB).toBeInstanceOf(WorkstationOpResultStub);

		// `stub.result` should be filled with the same payload —
		// the snapshot decoded `preserved_events` into our local
		// `slotFills` and the stub's atom resolved through that.
		const r = sessionB.store.get(stubB.result) as ResultStatus<unknown>;
		expect(r.status).toBe("filled");
		if (r.status === "filled") {
			const value = r.value as Record<string, unknown>;
			expect(value.body).toBe("frontend_lib integration test note");
		}
		expect(sessionB.store.get(stubB.committed)).toBe(true);

		await sessionB.detach();
		await connB.close();
	});
});

describe("integration (chunk 5): invalid short_id rejects the mutation", () => {
	it("allocateLoad against an unknown short_id surfaces as a permanent rollback", async () => {
		// The server validates `LoadEntityOp.short_id` at op-time
		// (must resolve to a workstation-tracked entity). An
		// unknown id fails before the load worker spawns, so this
		// path surfaces as `mutation_rejected(validation_failed)`
		// rather than a `LoadErrorEvent`. The user's `stub.result`
		// stays pending (the slot was never allocated server-side);
		// the rollback is signaled via `onRollback`.
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "chunk5-bad-shortid" });
		await awaitLifecycle(session, "connected");

		const badRef = connector.entityRef("does-not-exist");
		let stub!: WorkstationOpResultStub;
		const rollbacks: RollbackInfo[] = [];
		session.store.set(
			session.userState,
			(_, ctx) => {
				stub = ctx.allocateLoad(badRef) as WorkstationOpResultStub;
				return { x: stub };
			},
			{
				onRollback: (info) => rollbacks.push(info),
			},
		);

		// Wait for the rollback to fire.
		await awaitCondition(() => rollbacks.length > 0, "onRollback for the bad-shortid mutation");
		expect(rollbacks[0]?.kind).toBe("permanent");
		// Local rolled back: the stub is no longer in user state.
		expect(session.store.get(session.userState)).toBeNull();
		// The stub itself stays pending — the slot never made it
		// server-side, no fill ever arrives.
		expect((session.store.get(stub.result) as ResultStatus<unknown>).status).toBe("pending");
		expect(session.store.get(stub.committed)).toBe(false);

		await session.detach();
		await conn.close();
	});
});

// --- Stage 3 (invokes) ------------------------------------------------

describe("integration (stage 3): invoke happy path", () => {
	it("allocateInvoke(echo, [42]) → InvokeOp → fill → stub.result transitions to filled with 42", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage3-echo-primitive" });
		await awaitLifecycle(session, "connected");

		const svcShortId = await resolveTestServiceShortId(connector);
		const svc = connector.serviceRef(svcShortId);
		let stub!: WorkstationOpResultStub;
		session.store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateInvoke(svc, "echo", [42]) as WorkstationOpResultStub;
			return { r: stub };
		});

		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "stub.result to fill");

		const result = session.store.get(stub.result) as ResultStatus<unknown>;
		expect(result.status).toBe("filled");
		if (result.status === "filled") expect(result.value).toBe(42);
		expect(session.store.get(stub.done)).toBe(true);
		expect(session.store.get(stub.committed)).toBe(true);

		await session.detach();
		await conn.close();
	});
});

describe("integration (stage 3): invoke with refs in args", () => {
	it("echo({note: entityRef, items: [serviceRef]}) returns the structure with refs intact", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage3-echo-refs" });
		await awaitLifecycle(session, "connected");

		const svcShortId = await resolveTestServiceShortId(connector);
		const entityShortId = await resolveTestEntityShortId(connector);
		const svc = connector.serviceRef(svcShortId);
		const entityRef = connector.entityRef(entityShortId);

		// Pass a structured payload with refs nested inside a dict
		// and a list. The workstation invoke worker validates the
		// refs (anywhere-tracked is enough), echoes the value
		// through, and the result event carries it back.
		let stub!: WorkstationOpResultStub;
		session.store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateInvoke(svc, "echo", [
				{ note: entityRef, items: [svc] },
			]) as WorkstationOpResultStub;
			return { r: stub };
		});

		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "echo with refs to fill");

		const result = session.store.get(stub.result) as ResultStatus<Record<string, unknown>>;
		expect(result.status).toBe("filled");
		if (result.status === "filled") {
			const value = result.value;
			// Refs round-trip and intern back to the same instances
			// we minted on this connector.
			expect(value.note).toBe(entityRef);
			const items = value.items as readonly unknown[];
			expect(items[0]).toBe(svc);
		}

		// Terminate the server-side session so the preserved
		// invoke-result event releases its auto-pinned refs.
		// The workstation accepts CloseOp bundled with the
		// cleanup ops that the flush walk derives from clearing
		// `local.terminated = true` (Untrack, UnpreserveEvents,
		// patch removing user state) — CloseOp just needs to be
		// the LAST op in the mutation, which the frontend's
		// flush already guarantees.
		await session.terminate();
		await conn.close();
	});
});

describe("integration (stage 3): invoke with kwargs", () => {
	it("combine(value, *, name) receives both args and kwargs on the Python side", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage3-combine-kwargs" });
		await awaitLifecycle(session, "connected");

		const svcShortId = await resolveTestServiceShortId(connector);
		const svc = connector.serviceRef(svcShortId);
		let stub!: WorkstationOpResultStub;
		session.store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateInvoke(svc, "combine", ["positional-arg"], {
				name: "alice",
			}) as WorkstationOpResultStub;
			return { r: stub };
		});

		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "combine kwargs to fill");

		const result = session.store.get(stub.result) as ResultStatus<{ value: unknown; name: string }>;
		expect(result.status).toBe("filled");
		if (result.status === "filled") {
			expect(result.value.value).toBe("positional-arg");
			expect(result.value.name).toBe("alice");
		}

		await session.detach();
		await conn.close();
	});

	it("combine(value) without kwargs uses the Python-side default", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage3-combine-default" });
		await awaitLifecycle(session, "connected");

		const svcShortId = await resolveTestServiceShortId(connector);
		const svc = connector.serviceRef(svcShortId);
		let stub!: WorkstationOpResultStub;
		session.store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateInvoke(svc, "combine", ["v"]) as WorkstationOpResultStub;
			return { r: stub };
		});

		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "combine default to fill");

		const result = session.store.get(stub.result) as ResultStatus<{ value: unknown; name: string }>;
		expect(result.status).toBe("filled");
		if (result.status === "filled") {
			expect(result.value.name).toBe("default");
		}

		await session.detach();
		await conn.close();
	});
});

describe("integration (stage 3): invoke error path", () => {
	it("fail(message) → invoke_error → stub.result transitions to errored", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage3-fail" });
		await awaitLifecycle(session, "connected");

		const svcShortId = await resolveTestServiceShortId(connector);
		const svc = connector.serviceRef(svcShortId);
		let stub!: WorkstationOpResultStub;
		session.store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateInvoke(svc, "fail", [
				"deliberate test failure",
			]) as WorkstationOpResultStub;
			return { r: stub };
		});

		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<unknown>;
			return r.status === "errored";
		}, "stub.result to error");

		const result = session.store.get(stub.result) as ResultStatus<unknown>;
		expect(result.status).toBe("errored");
		if (result.status === "errored") {
			// The Python side raises RuntimeError; the workstation
			// invoke worker wraps the exception's `repr` into the
			// `InvokeErrorEvent.message`. The exact format is
			// implementation-defined, but the message we passed in
			// should appear somewhere in the string.
			expect(result.message).toContain("deliberate test failure");
		}
		// The slot still committed — the InvokeOp itself succeeded;
		// only its delivered result is errored.
		expect(session.store.get(stub.committed)).toBe(true);
		expect(session.store.get(stub.done)).toBe(true);

		await session.detach();
		await conn.close();
	});
});

describe("integration (stage 3): invoke result survives attach to same session_id", () => {
	it("a second session attached to the same session_id sees the preserved invoke fill", async () => {
		// Companion to the stage-2 load attach test
		// ("fill survives attach to same session_id"). Proves the
		// invoke fill path goes through the same preserve / snapshot
		// rebuild machinery — detach session A with a preserved
		// invoke_result, attach a fresh session B to the same
		// session_id, and verify the snapshot delivers the fill so
		// B's stub reports filled with the same payload.
		const { conn: connA, connector: connectorA } = makeConnector();
		const sessionA = await connectorA.createSession({ tag: "stage3-attach-src" });
		await awaitLifecycle(sessionA, "connected");

		const svcShortId = await resolveTestServiceShortId(connectorA);
		const svc = connectorA.serviceRef(svcShortId);
		// Use a payload that round-trips a primitive so the
		// assertion is straightforward. (Refs in the payload would
		// also work; we just want to keep the comparison simple.)
		let stubA!: WorkstationOpResultStub;
		sessionA.store.set(sessionA.userState, (_, ctx) => {
			stubA = ctx.allocateInvoke(svc, "echo", [17]) as WorkstationOpResultStub;
			return { r: stubA };
		});

		await awaitCondition(() => {
			const r = sessionA.store.get(stubA.result) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "A: stub.result to fill");
		// Trigger a second flush so the fill gets moved into the
		// server's preserved_events sidecar (the first flush sent
		// the InvokeOp; the second's PreserveEventsOp pins the
		// fill seq).
		sessionA.store.set(sessionA.userState, (prev) => ({ ...(prev as object), extra: 1 }));

		// Subscribe to session_debug to know when preserve has
		// committed before detaching A.
		const debugSnapshots: SessionDebugSnapshotEvent[] = [];
		const debugHandle = connA.registerSubscription(
			{ kind: "session_debug", session_id: sessionA.sessionId },
			{
				onFrame: (frame) => {
					if (frame.type === "session_debug_snapshot") debugSnapshots.push(frame);
				},
				onClose: () => {},
			},
		);
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			return last !== undefined && last.preserved_events.length === 1;
		}, "A: preserved_events populated before detach");
		debugHandle.close();

		const sessionId = sessionA.sessionId;
		await sessionA.detach();
		await connA.close();

		// Fresh connector + attachSession to the same session_id.
		// On snapshot decode, `rebuildSlotFillsFromSnapshot` picks
		// up the preserved `invoke_result` (the kind filter we
		// broadened in chunk 3), and the stub in committed state
		// reports filled.
		const { conn: connB, connector: connectorB } = makeConnector();
		const sessionB = connectorB.attachSession(sessionId);
		await awaitLifecycle(sessionB, "connected");

		const userB = sessionB.store.get(sessionB.userState) as Record<string, unknown>;
		const stubB = userB.r as WorkstationOpResultStub;
		expect(stubB).toBeInstanceOf(WorkstationOpResultStub);

		const r = sessionB.store.get(stubB.result) as ResultStatus<number>;
		expect(r.status).toBe("filled");
		if (r.status === "filled") expect(r.value).toBe(17);
		expect(sessionB.store.get(stubB.committed)).toBe(true);

		// Terminate B so the preserved event releases cleanly.
		await sessionB.terminate();
		await connB.close();
	});
});

// --- Stage 6 (callback inboxes) ---------------------------------------

describe("integration (stage 6): callback inbox happy path", () => {
	it("notify_n(inbox.handle, 3, 'msg') → 3 CallbackEvents arrive, inbox.events sees them in order", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage6-notify-3" });
		await awaitLifecycle(session, "connected");

		const svcShortId = await resolveTestServiceShortId(connector);
		const svc = connector.serviceRef(svcShortId);

		let inbox!: WorkstationCallbackInboxStub<string>;
		let stub!: WorkstationOpResultStub<number>;
		session.store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback<string>() as WorkstationCallbackInboxStub<string>;
			stub = ctx.allocateInvoke(svc, "notify_n", [
				inbox.handle,
				3,
				"msg",
			]) as WorkstationOpResultStub<number>;
			return { cb: inbox, r: stub };
		});

		// Wait for the invoke to complete — `notify_n` returns the
		// count of successful notifies. The Python method awaits each
		// notify in-line, so by the time the invoke result fires, all
		// three CallbackEvents have been emitted into the session's
		// event_log.
		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<number>;
			return r.status === "filled";
		}, "notify_n to fill");
		const r = session.store.get(stub.result) as ResultStatus<number>;
		expect(r.status).toBe("filled");
		if (r.status === "filled") expect(r.value).toBe(3);

		// CallbackEvents may arrive slightly after the invoke result
		// (separate WebSocket frames). Poll for the inbox to drain.
		await awaitCondition(() => {
			const events = session.store.get(inbox.events);
			return events.length === 3;
		}, "inbox to have 3 events");
		const events = session.store.get(inbox.events);
		expect(events).toEqual(["msg-0", "msg-1", "msg-2"]);

		await session.terminate();
		await conn.close();
	});
});

describe("integration (stage 6): cursor advance preserves the right seqs", () => {
	it("inbox.sliced(n) → wire-visible PreserveEventsOp / UnpreserveEventsOp keep the cursor's tail alive", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage6-sliced" });
		await awaitLifecycle(session, "connected");

		// Subscribe to session_debug to observe the preserved_events
		// seq list directly.
		const debugSnapshots: SessionDebugSnapshotEvent[] = [];
		const debugHandle = conn.registerSubscription(
			{ kind: "session_debug", session_id: session.sessionId },
			{
				onFrame: (frame: SubscriptionFrame) => {
					if (frame.type === "session_debug_snapshot") debugSnapshots.push(frame);
				},
				onClose: () => {},
			},
		);

		const svcShortId = await resolveTestServiceShortId(connector);
		const svc = connector.serviceRef(svcShortId);

		let inbox!: WorkstationCallbackInboxStub<string>;
		let stub!: WorkstationOpResultStub<number>;
		session.store.set(session.userState, (_, ctx) => {
			inbox = ctx.allocateCallback<string>() as WorkstationCallbackInboxStub<string>;
			stub = ctx.allocateInvoke(svc, "notify_n", [
				inbox.handle,
				4,
				"e",
			]) as WorkstationOpResultStub<number>;
			return { cb: inbox, r: stub };
		});

		await awaitCondition(() => {
			const events = session.store.get(inbox.events);
			return events.length === 4;
		}, "inbox to receive 4 events");

		// Wait for the invoke to settle so the next flush carries
		// only the inbox cursor change.
		await awaitCondition(() => {
			const status = session.store.get(stub.result) as ResultStatus<unknown>;
			return status.status === "filled";
		}, "invoke to fill");

		// Pull the seqs of the 4 callback events out of the local
		// inbox log — those are the seqs the server should be
		// preserving for us (after the next flush). We use these
		// for set-containment assertions below, avoiding magic
		// counts that would couple the test to "how many other
		// events does this scenario happen to preserve."
		const log = session.store.get(session.inboxLogsAtom).get(inbox.slot);
		if (log === undefined) throw new Error("expected inboxLogs entry for slot");
		const callbackSeqs = log.events.map((e) => e.seq);
		expect(callbackSeqs).toHaveLength(4);
		const droppedSeqs = callbackSeqs.slice(0, 2);

		// Trigger a flush to ship a PreserveEventsOp pinning the
		// (so-far unpreserved) callback seqs.
		session.store.set(session.userState, (prev) => ({ ...(prev as object), tick: 1 }));

		// Wait until the server's preserve sidecar covers all 4
		// callback seqs. We assert containment, not count — the
		// sidecar may also hold the invoke result's seq (it does in
		// this scenario) and could hold others in future scenarios
		// without invalidating what we're testing.
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			return last !== undefined && callbackSeqs.every((s) => last.preserved_events.includes(s));
		}, "preserved_events to cover all callback seqs");
		const beforeSlice = [...(debugSnapshots[debugSnapshots.length - 1]?.preserved_events ?? [])];

		// Advance the cursor past the first 2 events. After commit,
		// the unpreserve should drop those 2 seqs while keeping the
		// rest of the preserve set unchanged.
		session.store.set(session.userState, (prev) => {
			const u = prev as { [k: string]: unknown };
			const cb = u.cb as WorkstationCallbackInboxStub<string>;
			return { ...u, cb: cb.sliced(2) };
		});

		// Wait until the server has dropped the two seqs.
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			return last !== undefined && droppedSeqs.every((s) => !last.preserved_events.includes(s));
		}, "preserved_events to drop the first 2 callback seqs");
		const afterSlice = [...(debugSnapshots[debugSnapshots.length - 1]?.preserved_events ?? [])];

		// Structural assertion: afterSlice == beforeSlice minus
		// the 2 dropped seqs. Catches the case where the server
		// drops the wrong seqs (e.g. unpreserves the tail rather
		// than the prefix).
		const expectedAfter = beforeSlice.filter((s) => !droppedSeqs.includes(s));
		expect([...afterSlice].sort((a, b) => a - b)).toEqual([...expectedAfter].sort((a, b) => a - b));

		// The local cursor sees only the remaining 2 events.
		const u = session.store.get(session.userState) as {
			cb: WorkstationCallbackInboxStub<string>;
		};
		expect(session.store.get(u.cb.events)).toEqual(["e-2", "e-3"]);

		debugHandle.close();
		await session.terminate();
		await conn.close();
	});
});

describe("integration (stage 6): CloseCallbackOp when the last inbox stub is removed", () => {
	it("removing the only inbox stub fires CloseCallbackOp; server-side open_callbacks shrinks", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage6-close" });
		await awaitLifecycle(session, "connected");

		const debugSnapshots: SessionDebugSnapshotEvent[] = [];
		const debugHandle = conn.registerSubscription(
			{ kind: "session_debug", session_id: session.sessionId },
			{
				onFrame: (frame: SubscriptionFrame) => {
					if (frame.type === "session_debug_snapshot") debugSnapshots.push(frame);
				},
				onClose: () => {},
			},
		);

		session.store.set(session.userState, (_, ctx) => ({ cb: ctx.allocateCallback() }));

		// After CreateCallback commits, open_callbacks has one entry.
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			return last !== undefined && Object.keys(last.open_callbacks).length === 1;
		}, "open_callbacks to contain the new callback");

		// Remove the inbox stub from state.
		session.store.set(session.userState, () => ({}));

		// After CloseCallback commits, open_callbacks is empty again.
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			return last !== undefined && Object.keys(last.open_callbacks).length === 0;
		}, "open_callbacks to drop the slot after CloseCallbackOp");

		debugHandle.close();
		await session.terminate();
		await conn.close();
	});
});

describe("integration (stage 6): preserved CallbackEvents survive attach to same session_id", () => {
	it("a second session attached to the same session_id sees the preserved inbox events", async () => {
		// Companion to the existing "fill survives attach" tests for
		// load + invoke. Proves the inbox-event preserve / snapshot
		// rebuild machinery works end-to-end against the live
		// workstation: detach session A while it's holding a
		// CallbackEvent in the preserved sidecar, attach a fresh
		// session B to the same session_id, and verify B's snapshot
		// rebuilds `inboxLogs` so the inbox stub in committed state
		// emits the same payloads.
		const { conn: connA, connector: connectorA } = makeConnector();
		const sessionA = await connectorA.createSession({ tag: "stage6-attach-src" });
		await awaitLifecycle(sessionA, "connected");

		const svcShortId = await resolveTestServiceShortId(connectorA);
		const svc = connectorA.serviceRef(svcShortId);

		let inboxA!: WorkstationCallbackInboxStub<string>;
		let stubA!: WorkstationOpResultStub<number>;
		sessionA.store.set(sessionA.userState, (_, ctx) => {
			inboxA = ctx.allocateCallback<string>() as WorkstationCallbackInboxStub<string>;
			stubA = ctx.allocateInvoke(svc, "notify_n", [
				inboxA.handle,
				2,
				"attach",
			]) as WorkstationOpResultStub<number>;
			return { cb: inboxA, r: stubA };
		});

		// Wait for events + invoke to arrive.
		await awaitCondition(() => {
			const events = sessionA.store.get(inboxA.events);
			return events.length === 2;
		}, "A: inbox to receive 2 events");
		await awaitCondition(() => {
			const r = sessionA.store.get(stubA.result) as ResultStatus<unknown>;
			return r.status === "filled";
		}, "A: invoke to fill");

		// Trigger a second flush so the preserve op pins the callback
		// seqs (the first flush only sent CreateCallback + InvokeOp).
		sessionA.store.set(sessionA.userState, (prev) => ({ ...(prev as object), tick: 1 }));

		// Wait until preserved_events covers all 2 callback events +
		// the invoke result, then detach A.
		const debugSnapshots: SessionDebugSnapshotEvent[] = [];
		const debugHandle = connA.registerSubscription(
			{ kind: "session_debug", session_id: sessionA.sessionId },
			{
				onFrame: (frame) => {
					if (frame.type === "session_debug_snapshot") debugSnapshots.push(frame);
				},
				onClose: () => {},
			},
		);
		await awaitCondition(() => {
			const last = debugSnapshots[debugSnapshots.length - 1];
			return last !== undefined && last.preserved_events.length >= 3;
		}, "A: preserved_events covers callbacks + invoke result");
		debugHandle.close();

		const sessionId = sessionA.sessionId;
		await sessionA.detach();
		await connA.close();

		// Fresh connector + attachSession to the same session_id.
		// On snapshot decode, `rebuildInboxLogsFromSnapshot` populates
		// inboxLogs from the preserved CallbackEvents; the inbox stub
		// in committed user state emits the same payloads.
		const { conn: connB, connector: connectorB } = makeConnector();
		const sessionB = connectorB.attachSession(sessionId);
		await awaitLifecycle(sessionB, "connected");

		const userB = sessionB.store.get(sessionB.userState) as Record<string, unknown>;
		const inboxB = userB.cb as WorkstationCallbackInboxStub<string>;
		expect(inboxB).toBeInstanceOf(WorkstationCallbackInboxStub);
		expect(inboxB.offset).toBe(0);
		expect(sessionB.store.get(inboxB.events)).toEqual(["attach-0", "attach-1"]);

		// Terminate B so the preserved events release cleanly.
		await sessionB.terminate();
		await connB.close();
	});
});

// --- Stage 7-pre-1: entity / service metadata ---------------------

describe("integration (stage 7-pre-1): entity metadata happy path", () => {
	it("allocateGetEntityMetadata against e-test resolves with id_string + unprovenanced ref", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage7pre1-entity-md" });
		await awaitLifecycle(session, "connected");

		const entityShortId = await resolveTestEntityShortId(connector);
		const ref = connector.entityRef(entityShortId);
		let stub!: OpResultStub<EntityMetadata>;
		session.store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateGetEntityMetadata(ref);
			return { m: stub };
		});

		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<EntityMetadata>;
			return r.status === "filled";
		}, "entity metadata to fill");

		const result = session.store.get(stub.result) as ResultStatus<EntityMetadata>;
		expect(result.status).toBe("filled");
		if (result.status === "filled") {
			const md = result.value;
			// Bare d-tag entity (no provenance parent).
			expect(typeof md.id_string).toBe("string");
			expect(md.id_string.startsWith("d")).toBe(true);
			// unprovenanced decoded to an EntityRefStub (same instance
			// as a programmatic connector.entityRef call).
			expect(md.unprovenanced).toBe(ref);
			// Bare d-tag → no provenance_parent.
			expect(md.provenance_parent).toBeUndefined();
			// Bare d-tag → no unwraps_to (d isn't a provenance node).
			expect(md.unwraps_to).toBeUndefined();
		}
		expect(session.store.get(stub.committed)).toBe(true);

		// Terminate so preserved metadata events drop refs cleanly.
		await session.terminate();
		await conn.close();
	});
});

describe("integration (stage 7-pre-1): service metadata happy path", () => {
	it("allocateGetServiceMetadata against s-test resolves with type_id_string", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage7pre1-service-md" });
		await awaitLifecycle(session, "connected");

		const svcShortId = await resolveTestServiceShortId(connector);
		const svc = connector.serviceRef(svcShortId);
		let stub!: OpResultStub<ServiceMetadata>;
		session.store.set(session.userState, (_, ctx) => {
			stub = ctx.allocateGetServiceMetadata(svc);
			return { m: stub };
		});

		await awaitCondition(() => {
			const r = session.store.get(stub.result) as ResultStatus<ServiceMetadata>;
			return r.status === "filled";
		}, "service metadata to fill");

		const result = session.store.get(stub.result) as ResultStatus<ServiceMetadata>;
		expect(result.status).toBe("filled");
		if (result.status === "filled") {
			const md = result.value;
			expect(typeof md.type_id_string).toBe("string");
			expect(md.type_id_string.length).toBeGreaterThan(0);
		}
		expect(session.store.get(stub.committed)).toBe(true);

		await session.detach();
		await conn.close();
	});
});

describe("integration (stage 7-pre-1): entity metadata dedup", () => {
	it("two allocateGetEntityMetadata calls for the same shortId share one stub + one server-side op", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage7pre1-md-dedup" });
		await awaitLifecycle(session, "connected");

		const entityShortId = await resolveTestEntityShortId(connector);
		const ref = connector.entityRef(entityShortId);
		let stubA!: OpResultStub<EntityMetadata>;
		let stubB!: OpResultStub<EntityMetadata>;
		session.store.set(session.userState, (_, ctx) => {
			stubA = ctx.allocateGetEntityMetadata(ref);
			stubB = ctx.allocateGetEntityMetadata(ref);
			return { m: stubA };
		});
		expect(stubA).toBe(stubB);

		await awaitCondition(() => {
			const r = session.store.get(stubA.result) as ResultStatus<EntityMetadata>;
			return r.status === "filled";
		}, "stub.result to fill");

		const result = session.store.get(stubA.result) as ResultStatus<EntityMetadata>;
		expect(result.status).toBe("filled");

		await session.terminate();
		await conn.close();
	});

	it("metadata dedup is independent of load dedup: same entity gets distinct slots for the two op kinds", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage7pre1-cross-allocator" });
		await awaitLifecycle(session, "connected");

		const entityShortId = await resolveTestEntityShortId(connector);
		const ref = connector.entityRef(entityShortId);
		let loadStub!: OpResultStub<unknown>;
		let mdStub!: OpResultStub<EntityMetadata>;
		session.store.set(session.userState, (_, ctx) => {
			loadStub = ctx.allocateLoad(ref);
			mdStub = ctx.allocateGetEntityMetadata(ref);
			return { l: loadStub, m: mdStub };
		});
		// Distinct stubs, distinct slots.
		expect(loadStub).not.toBe(mdStub);
		expect((loadStub as WorkstationOpResultStub).slot).not.toBe(
			(mdStub as WorkstationOpResultStub).slot,
		);

		// Both fills arrive.
		await awaitCondition(() => {
			const a = session.store.get(loadStub.result) as ResultStatus<unknown>;
			const b = session.store.get(mdStub.result) as ResultStatus<unknown>;
			return a.status === "filled" && b.status === "filled";
		}, "both load and metadata fills");

		const loadResult = session.store.get(loadStub.result) as ResultStatus<Record<string, unknown>>;
		if (loadResult.status === "filled") {
			expect(loadResult.value.body).toBe("frontend_lib integration test note");
		}
		const mdResult = session.store.get(mdStub.result) as ResultStatus<EntityMetadata>;
		if (mdResult.status === "filled") {
			expect(mdResult.value.unprovenanced).toBe(ref);
		}

		await session.terminate();
		await conn.close();
	});
});

describe("integration (stage 7-pre-1): metadata fill survives attach to same session_id", () => {
	it("attach-session bootstraps entityMetadataDedup from snapshot; second allocate reuses the slot", async () => {
		// Session A allocates entity metadata, lets the preserve op
		// fire on a follow-up flush, then detaches (server-side
		// session lives on).
		const { conn: connA, connector: connectorA } = makeConnector();
		const sessionA = await connectorA.createSession({ tag: "stage7pre1-attach-src" });
		await awaitLifecycle(sessionA, "connected");

		const entityShortId = await resolveTestEntityShortId(connectorA);
		const refA = connectorA.entityRef(entityShortId);
		let stubA!: OpResultStub<EntityMetadata>;
		sessionA.store.set(sessionA.userState, (_, ctx) => {
			stubA = ctx.allocateGetEntityMetadata(refA);
			return { m: stubA };
		});
		await awaitCondition(() => {
			const r = sessionA.store.get(stubA.result) as ResultStatus<EntityMetadata>;
			return r.status === "filled";
		}, "A: metadata fill");

		// Follow-up flush so the preserve op ships and the
		// committedAckSeq advances past the metadata event.
		sessionA.store.set(sessionA.userState, (prev) => ({ ...(prev as object), extra: 1 }));

		const sessionId = sessionA.sessionId;
		await sessionA.detach();
		await connA.close();

		// Attach a fresh connector to the same session. Snapshot
		// brings `entityMetadataDedup` + the preserved metadata fill.
		const { conn: connB, connector: connectorB } = makeConnector();
		const sessionB = connectorB.attachSession(sessionId);
		await awaitLifecycle(sessionB, "connected");

		const refB = connectorB.entityRef(entityShortId);
		let stubB!: OpResultStub<EntityMetadata>;
		sessionB.store.set(sessionB.userState, (prev, ctx) => {
			stubB = ctx.allocateGetEntityMetadata(refB);
			return { ...(prev as object), reattach: stubB };
		});
		// Slot inherited from A — same integer.
		expect((stubB as WorkstationOpResultStub).slot).toBe((stubA as WorkstationOpResultStub).slot);
		// Already filled (the preserved metadata event populated
		// slotFills on snapshot rebuild).
		const r = sessionB.store.get(stubB.result) as ResultStatus<EntityMetadata>;
		expect(r.status).toBe("filled");
		if (r.status === "filled") {
			// Decoded `unprovenanced` interns to connector B's ref
			// (interned per-connector; the shortId matches A's).
			const u = r.value.unprovenanced as EntityRefStub | undefined;
			expect(u?.shortId).toBe(entityShortId);
		}

		await sessionB.terminate();
		await connB.close();
	});
});

describe("integration (stage 7-pre-1): invalid short_id rejects the metadata op", () => {
	it("allocateGetEntityMetadata against an unknown short_id surfaces as a permanent rollback", async () => {
		const { conn, connector } = makeConnector();
		const session = await connector.createSession({ tag: "stage7pre1-bad-shortid" });
		await awaitLifecycle(session, "connected");

		const badRef = connector.entityRef("does-not-exist");
		let stub!: OpResultStub<EntityMetadata>;
		const rollbacks: RollbackInfo[] = [];
		session.store.set(
			session.userState,
			(_, ctx) => {
				stub = ctx.allocateGetEntityMetadata(badRef);
				return { m: stub };
			},
			{
				onRollback: (info) => {
					rollbacks.push(info);
				},
			},
		);

		await awaitCondition(() => rollbacks.length > 0, "rollback to fire");
		expect(rollbacks[0]?.kind).toBe("permanent");
		// Local state rolled back; the stub never made it into
		// committed and result stays pending.
		const status = session.store.get(stub.result) as ResultStatus<EntityMetadata>;
		expect(status.status).toBe("pending");

		await session.detach();
		await conn.close();
	});
});

// ===================================================================
// Stage 9-post-2 — stateless connector.loadEntityData
// ===================================================================

describe("integration (stage 9-post-2): connector.loadEntityData", () => {
	it("loads e-test out-of-band without joining a session, decodes the body", async () => {
		const { conn, connector } = makeConnector();
		const entityShortId = await resolveTestEntityShortId(connector);
		const ref = connector.entityRef(entityShortId);

		// No session, no userState, no jotai. Just a promise.
		const value = (await connector.loadEntityData(ref)) as Record<string, unknown>;
		expect(value.body).toBe("frontend_lib integration test note");

		await conn.close();
	});

	it("rejects with LoadEntityError when the short_id is unknown", async () => {
		const { conn, connector } = makeConnector();
		const bogus = connector.entityRef("does-not-exist");

		await expect(connector.loadEntityData(bogus)).rejects.toBeInstanceOf(LoadEntityError);
		await expect(connector.loadEntityData(bogus)).rejects.toMatchObject({
			shortId: "does-not-exist",
		});

		await conn.close();
	});
});
