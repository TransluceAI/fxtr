/**
 * Unit tests for `WorkstationConnector` and `ReactiveList`. Drives
 * the `WorkstationServerConnection` via `MockWebSocket` rather than
 * spawning a real workstation; live-server coverage lands with the
 * stage-1 integration test (chunk 7).
 *
 * Conventions match `connection.test.ts`: per-test connector, jotai
 * `createStore` for isolation, `console.warn`/`error` spied to keep
 * unexpected-warning assertions tight.
 */

import { createStore } from "jotai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
	ConnectorClosedError,
	type EntityRecord,
	type ReactiveListStatus,
	type ResourceChangesEvent,
	type ResourceSnapshotEvent,
	type ServiceRecord,
	WorkstationConnector,
	WorkstationServerConnection,
} from "../src";
import { MockWebSocket, currentMockSocket, resetMockWebSockets } from "./_mock_websocket";

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

/** Last sent message, parsed. Throws if nothing was sent. */
function lastSent(): { request_id: number; op: string; [k: string]: unknown } {
	const sent = currentMockSocket().sentParsed;
	const last = sent[sent.length - 1];
	if (!last) throw new Error("no message has been sent");
	return last as { request_id: number; op: string; [k: string]: unknown };
}

/** Acknowledge the most recent subscribe request and deliver a snapshot. */
function ackAndSnapshot(records: readonly unknown[]): { subscription_id: number } {
	const subscribe = lastSent();
	expect(subscribe.op).toBe("subscribe");
	const subscription_id = subscribe.request_id;
	currentMockSocket().simulateMessage(JSON.stringify({ type: "ack", request_id: subscription_id }));
	const snapshot: ResourceSnapshotEvent = {
		subscription_id,
		type: "resource_snapshot",
		data: records as readonly Readonly<Record<string, unknown>>[],
	};
	currentMockSocket().simulateMessage(JSON.stringify(snapshot));
	return { subscription_id };
}

function deliverChanges(
	subscription_id: number,
	added: readonly unknown[] = [],
	updated: readonly unknown[] = [],
	removed: readonly string[] = [],
): void {
	const frame: ResourceChangesEvent = {
		subscription_id,
		type: "resource_changes",
		added: added as readonly Readonly<Record<string, unknown>>[],
		updated: updated as readonly Readonly<Record<string, unknown>>[],
		removed: [...removed],
	};
	currentMockSocket().simulateMessage(JSON.stringify(frame));
}

let warnSpy: ReturnType<typeof vi.spyOn>;
let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
	resetMockWebSockets();
	warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
	errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
	warnSpy.mockRestore();
	errorSpy.mockRestore();
	vi.useRealTimers();
});

// --- ReactiveList lifecycle ----------------------------------------

describe("ReactiveList: snapshot and deltas", () => {
	it("starts in 'loading' with an empty records array and sends the subscribe", () => {
		const { connector, store } = makeConnector();
		const list = connector.listEntities();
		expect(store.get(list.status)).toBe<ReactiveListStatus>("loading");
		expect(store.get(list.records)).toEqual([]);

		currentMockSocket().simulateOpen();
		const sent = lastSent();
		expect(sent).toMatchObject({ op: "subscribe", resource: "entities", debug: false });
	});

	it("populates records from the initial snapshot and transitions to 'ready'", () => {
		const { connector, store } = makeConnector();
		const list = connector.listEntities();
		currentMockSocket().simulateOpen();

		const e1: EntityRecord = { short_id: "e1", fxtr_id: "f1", external_names: ["a"] };
		const e2: EntityRecord = { short_id: "e2", fxtr_id: "f2", external_names: [] };
		ackAndSnapshot([e1, e2]);

		expect(store.get(list.status)).toBe<ReactiveListStatus>("ready");
		expect(store.get(list.records)).toEqual([e1, e2]);
	});

	it("applies added / updated / removed deltas, keying by the resource's primary-key field", () => {
		const { connector, store } = makeConnector();
		const list = connector.listEntities();
		currentMockSocket().simulateOpen();

		const e1: EntityRecord = { short_id: "e1", fxtr_id: "f1", external_names: [] };
		const e2: EntityRecord = { short_id: "e2", fxtr_id: "f2", external_names: [] };
		const { subscription_id } = ackAndSnapshot([e1, e2]);

		const e3: EntityRecord = { short_id: "e3", fxtr_id: "f3", external_names: [] };
		const e2updated: EntityRecord = { short_id: "e2", fxtr_id: "f2", external_names: ["renamed"] };
		deliverChanges(subscription_id, [e3], [e2updated], ["e1"]);

		expect(store.get(list.records)).toEqual([e2updated, e3]);
	});

	it("uses the right primary-key field per resource (services -> short_id)", () => {
		const { connector, store } = makeConnector();
		const list = connector.listServices();
		currentMockSocket().simulateOpen();

		const s1: ServiceRecord = { short_id: "s1", service_type_id: "T", external_names: [] };
		const { subscription_id } = ackAndSnapshot([s1]);
		deliverChanges(subscription_id, [], [], ["s1"]);
		expect(store.get(list.records)).toEqual([]);
	});

	it("emits a fresh array reference on every publish (jotai's equality is referential)", () => {
		const { connector, store } = makeConnector();
		const list = connector.listEntities();
		currentMockSocket().simulateOpen();

		const e1: EntityRecord = { short_id: "e1", fxtr_id: "f1", external_names: [] };
		const { subscription_id } = ackAndSnapshot([e1]);

		const a = store.get(list.records);
		const e2: EntityRecord = { short_id: "e2", fxtr_id: "f2", external_names: [] };
		deliverChanges(subscription_id, [e2]);
		const b = store.get(list.records);

		expect(b).not.toBe(a);
		expect(b).toEqual([e1, e2]);
	});

	it("warns and skips records missing the primary-key field", () => {
		const { connector, store } = makeConnector();
		const list = connector.listEntities();
		currentMockSocket().simulateOpen();

		ackAndSnapshot([{ fxtr_id: "f0", external_names: [] }]);
		expect(store.get(list.records)).toEqual([]);
		expect(warnSpy).toHaveBeenCalledWith(
			expect.stringContaining('record missing key field "short_id"'),
			expect.anything(),
		);
	});
});

// --- ReactiveList: caching and debug flag --------------------------

describe("WorkstationConnector: list caching", () => {
	it("returns the same list instance for repeated calls with the same options", () => {
		const { connector } = makeConnector();
		const a = connector.listEntities();
		const b = connector.listEntities();
		expect(a).toBe(b);

		// Only one subscribe should have been sent.
		currentMockSocket().simulateOpen();
		const subscribes = currentMockSocket().sentParsed.filter(
			(m) => (m as { op?: string }).op === "subscribe",
		);
		expect(subscribes).toHaveLength(1);
	});

	it("treats debug=true and debug=false as separate subscriptions", () => {
		const { connector } = makeConnector();
		const plain = connector.listEntities();
		const debug = connector.listEntities({ debug: true });
		expect(plain).not.toBe(debug);

		currentMockSocket().simulateOpen();
		const subscribes = currentMockSocket().sentParsed.filter(
			(m) => (m as { op?: string }).op === "subscribe",
		);
		expect(subscribes).toHaveLength(2);
		expect(subscribes[0]).toMatchObject({ resource: "entities", debug: false });
		expect(subscribes[1]).toMatchObject({ resource: "entities", debug: true });
	});
});

// --- ReactiveList: terminal close paths ----------------------------

describe("ReactiveList: terminal close paths", () => {
	it("subscribe_failed (server ErrorResponse) transitions to 'closed' with last-known records, and logs the error", () => {
		const { connector, store } = makeConnector();
		const list = connector.listEntities();
		currentMockSocket().simulateOpen();

		const subscribe_id = lastSent().request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "error", request_id: subscribe_id, message: "no such resource" }),
		);

		expect(store.get(list.status)).toBe<ReactiveListStatus>("closed");
		expect(store.get(list.records)).toEqual([]);
		expect(errorSpy).toHaveBeenCalledWith(
			expect.stringContaining("ReactiveList(entities) subscribe failed"),
			expect.objectContaining({ message: expect.stringContaining("no such resource") }),
		);
	});

	it("a closed list (after subscribe_failed) stays cached; a subsequent call returns the same closed instance", () => {
		const { connector, store } = makeConnector();
		const a = connector.listEntities();
		currentMockSocket().simulateOpen();

		const subscribe_id = lastSent().request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "error", request_id: subscribe_id, message: "no such resource" }),
		);
		expect(store.get(a.status)).toBe<ReactiveListStatus>("closed");

		const b = connector.listEntities();
		expect(b).toBe(a);
		// No extra subscribe was sent — we did not silently retry.
		const subscribes = currentMockSocket().sentParsed.filter(
			(m) => (m as { op?: string }).op === "subscribe",
		);
		expect(subscribes).toHaveLength(1);
	});
});

// --- ReactiveList: reset / re-register -----------------------------

describe("ReactiveList: socket drop and re-register", () => {
	it("on socket drop, status flips to 'stale', records persist, and a fresh subscribe is queued", async () => {
		vi.useFakeTimers();
		const { connector, store } = makeConnector();
		const list = connector.listEntities();
		currentMockSocket().simulateOpen();

		const e1: EntityRecord = { short_id: "e1", fxtr_id: "f1", external_names: [] };
		ackAndSnapshot([e1]);
		expect(store.get(list.status)).toBe<ReactiveListStatus>("ready");
		expect(store.get(list.records)).toEqual([e1]);

		const firstSocket = currentMockSocket();
		firstSocket.simulateClose();

		// Records preserved, status flips to stale.
		expect(store.get(list.status)).toBe<ReactiveListStatus>("stale");
		expect(store.get(list.records)).toEqual([e1]);

		// Reconnect timer fires; a new socket is constructed and the
		// re-registered subscribe drains.
		await vi.advanceTimersByTimeAsync(200);
		const secondSocket = currentMockSocket();
		expect(secondSocket).not.toBe(firstSocket);
		secondSocket.simulateOpen();

		const sentOnNewSocket = secondSocket.sentParsed;
		const subscribe = sentOnNewSocket.find((m) => (m as { op?: string }).op === "subscribe");
		expect(subscribe).toMatchObject({ resource: "entities", debug: false });

		// A fresh snapshot lands; status returns to ready and records
		// reflect the new server view.
		ackAndSnapshot([]);
		expect(store.get(list.status)).toBe<ReactiveListStatus>("ready");
		expect(store.get(list.records)).toEqual([]);
	});

	it("multiple lists all resubscribe independently after a socket drop", async () => {
		vi.useFakeTimers();
		const { connector } = makeConnector();
		connector.listEntities();
		connector.listServices();
		currentMockSocket().simulateOpen();

		// Two initial subscribes.
		expect(
			currentMockSocket().sentParsed.filter((m) => (m as { op?: string }).op === "subscribe"),
		).toHaveLength(2);

		currentMockSocket().simulateClose();
		await vi.advanceTimersByTimeAsync(200);
		currentMockSocket().simulateOpen();

		// Both subscriptions sent again on the fresh socket.
		const subscribes = currentMockSocket().sentParsed.filter(
			(m) => (m as { op?: string }).op === "subscribe",
		);
		expect(subscribes).toHaveLength(2);
	});
});

// --- WorkstationConnector lifecycle --------------------------------

describe("WorkstationConnector: close semantics", () => {
	it("borrowed-connection close() releases list subscriptions and leaves the connection alive", async () => {
		const { conn, connector, store } = makeConnector();
		const list = connector.listEntities();
		currentMockSocket().simulateOpen();
		ackAndSnapshot([]);

		await connector.close();
		expect(store.get(list.status)).toBe<ReactiveListStatus>("closed");
		// Each list sent an unsubscribe rather than the connection
		// being torn down.
		const unsubscribes = currentMockSocket().sentParsed.filter(
			(m) => (m as { op?: string }).op === "unsubscribe",
		);
		expect(unsubscribes).toHaveLength(1);
		// Underlying connection still usable.
		expect(store.get(conn.connectionStatus)).toBe("open");

		await conn.close();
	});

	it("listX() after close() throws ConnectorClosedError", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();
		await connector.close();
		expect(() => connector.listEntities()).toThrow(ConnectorClosedError);
		await conn.close();
	});

	it("close() is idempotent", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();
		await connector.close();
		await connector.close();
		expect(() => connector.listEntities()).toThrow(ConnectorClosedError);
		await conn.close();
	});

	it("fans out 'closed' status to all lists when the connector closes", async () => {
		const { conn, connector, store } = makeConnector();
		const entities = connector.listEntities();
		const services = connector.listServices();
		currentMockSocket().simulateOpen();
		ackAndSnapshot([]);
		ackAndSnapshot([]);

		await connector.close();
		expect(store.get(entities.status)).toBe<ReactiveListStatus>("closed");
		expect(store.get(services.status)).toBe<ReactiveListStatus>("closed");

		await conn.close();
	});

	it("self-closes when the underlying connection is closed out from under it", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();
		// Externally close the connection — the connector wasn't told,
		// but its status observer should notice and flip closed=true.
		await conn.close();
		expect(() => connector.listEntities()).toThrow(ConnectorClosedError);
	});

	it("owned-connection close() (URL form) tears down the connection too", async () => {
		// Smoke-test the URL-based constructor by injecting MockWebSocket
		// as the global, so we go through the owned-connection path
		// without touching the network.
		const realWebSocket = (globalThis as { WebSocket?: unknown }).WebSocket;
		(globalThis as { WebSocket: unknown }).WebSocket = MockWebSocket;
		try {
			const store = createStore();
			const connector = new WorkstationConnector({ url: "ws://test", store });
			currentMockSocket().simulateOpen();
			await connector.close();
			expect(store.get(connector.connectionStatus)).toBe("closed");
		} finally {
			(globalThis as { WebSocket?: unknown }).WebSocket = realWebSocket;
		}
	});
});

// --- classifyShortId (the abstract Connector method) ----------------

describe("WorkstationConnector: classifyShortId", () => {
	it("answers entity / service / null by scanning the resource lists", () => {
		const { connector } = makeConnector();
		// Populate the entities list…
		connector.listEntities();
		currentMockSocket().simulateOpen();
		const e1: EntityRecord = { short_id: "da1b2c", fxtr_id: "f1", external_names: ["thing"] };
		ackAndSnapshot([e1]);
		// …then the services list (subscribe is the new last-sent).
		connector.listServices();
		const s1: ServiceRecord = { short_id: "s1", service_type_id: "T", external_names: ["svc"] };
		ackAndSnapshot([s1]);

		expect(connector.classifyShortId("da1b2c")).toBe("entity");
		expect(connector.classifyShortId("s1")).toBe("service");
		expect(connector.classifyShortId("unknown")).toBeNull();
	});

	it("returns null before any records have loaded", () => {
		const { connector } = makeConnector();
		expect(connector.classifyShortId("da1b2c")).toBeNull();
	});
});
