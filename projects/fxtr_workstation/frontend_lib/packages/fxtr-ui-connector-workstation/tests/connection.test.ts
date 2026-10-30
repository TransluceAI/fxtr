/**
 * Unit tests for `WorkstationServerConnection`. Uses `MockWebSocket`
 * to drive socket lifecycle events synchronously, with vitest's fake
 * timers for reconnect-backoff coverage.
 *
 * Conventions:
 *
 * - Each `it` creates its own connection and (where needed) closes it
 *   to avoid leaving sockets in a half-open state across tests.
 * - We snoop on `console.warn` / `console.error` via spies so warning
 *   assertions stay tight.
 * - JSON wire messages constructed inline mirror what the workstation
 *   server actually sends; the connection should not care about field
 *   ordering or extra fields.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createStore } from "jotai";
import {
	ConnectionClosedError,
	ConnectionLostError,
	ProtocolError,
	type SubscriptionCloseReason,
	type SubscriptionFrame,
	WorkstationServerConnection,
} from "../src";
import { MockWebSocket, currentMockSocket, resetMockWebSockets } from "./_mock_websocket";

// --- Test scaffolding -----------------------------------------------

function makeConnection() {
	const store = createStore();
	const conn = new WorkstationServerConnection({
		url: "ws://test",
		store,
		webSocketCtor: MockWebSocket as unknown as new (url: string) => WebSocket,
	});
	return { conn, store };
}

function status(conn: WorkstationServerConnection, store: ReturnType<typeof createStore>) {
	return store.get(conn.connectionStatus);
}

/** Collector for a subscription's onFrame / onClose callbacks. */
function recordingHandler() {
	const frames: SubscriptionFrame[] = [];
	const closes: { reason: SubscriptionCloseReason; error?: Error }[] = [];
	const handler = {
		onFrame: (frame: SubscriptionFrame) => {
			frames.push(frame);
		},
		onClose: (reason: SubscriptionCloseReason, error?: Error) => {
			closes.push(error === undefined ? { reason } : { reason, error });
		},
	};
	return { handler, frames, closes };
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

// --- Lifecycle ------------------------------------------------------

describe("WorkstationServerConnection: lifecycle", () => {
	it("starts in 'connecting' and transitions to 'open' on socket open", () => {
		const { conn, store } = makeConnection();
		expect(status(conn, store)).toBe("connecting");
		currentMockSocket().simulateOpen();
		expect(status(conn, store)).toBe("open");
	});

	it("transitions 'open' → 'reconnecting' → 'open' on unexpected socket drop", async () => {
		vi.useFakeTimers();
		const { conn, store } = makeConnection();
		currentMockSocket().simulateOpen();
		expect(status(conn, store)).toBe("open");

		currentMockSocket().simulateClose();
		expect(status(conn, store)).toBe("reconnecting");

		await vi.advanceTimersByTimeAsync(100);
		// A second MockWebSocket was constructed for the reconnect.
		expect(status(conn, store)).toBe("connecting");
		currentMockSocket().simulateOpen();
		expect(status(conn, store)).toBe("open");
	});

	it("close() transitions to 'closed' and resolves once the socket closes", async () => {
		const { conn, store } = makeConnection();
		currentMockSocket().simulateOpen();
		const closePromise = conn.close();
		// MockWebSocket.close() synchronously fires the close event.
		await closePromise;
		expect(status(conn, store)).toBe("closed");
	});

	it("close() is idempotent and returns the same promise", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const p1 = conn.close();
		const p2 = conn.close();
		expect(p1).toBe(p2);
		await p1;
	});

	it("close() before any socket has opened resolves immediately", async () => {
		const { conn, store } = makeConnection();
		await conn.close();
		expect(status(conn, store)).toBe("closed");
	});
});

// --- sendRequest ----------------------------------------------------

describe("WorkstationServerConnection: sendRequest", () => {
	it("correlates a response by request_id and resolves with the typed frame", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();

		const promise = conn.sendRequest({ op: "create_session", metadata: { k: "v" } });

		const sent = currentMockSocket().sentParsed;
		expect(sent).toHaveLength(1);
		expect(sent[0]).toMatchObject({ op: "create_session", metadata: { k: "v" } });
		const request_id = (sent[0] as { request_id: number }).request_id;

		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "session_created", request_id, session_id: "abc" }),
		);

		const result = await promise;
		expect(result).toMatchObject({ type: "session_created", session_id: "abc" });
	});

	it("allocates monotonically increasing request_ids", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		conn.sendRequest({ op: "create_session", metadata: {} });
		conn.sendRequest({ op: "create_session", metadata: {} });
		conn.sendRequest({ op: "create_session", metadata: {} });
		const ids = currentMockSocket().sentParsed.map((m) => (m as { request_id: number }).request_id);
		expect(ids).toEqual([1, 2, 3]);
	});

	it("queues requests sent before the socket is open and drains on open", () => {
		const { conn } = makeConnection();
		conn.sendRequest({ op: "create_session", metadata: { a: 1 } });
		conn.sendRequest({ op: "create_session", metadata: { b: 2 } });

		expect(currentMockSocket().sentMessages).toHaveLength(0);
		currentMockSocket().simulateOpen();
		expect(currentMockSocket().sentMessages).toHaveLength(2);
	});

	it("rejects with ProtocolError on a server ErrorResponse", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const promise = conn.sendRequest({ op: "create_session", metadata: {} });
		const request_id = (currentMockSocket().sentParsed[0] as { request_id: number }).request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "error", request_id, message: "bad request" }),
		);
		await expect(promise).rejects.toBeInstanceOf(ProtocolError);
	});

	it("rejects with ConnectionLostError on socket drop mid-request", async () => {
		vi.useFakeTimers();
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const promise = conn.sendRequest({ op: "create_session", metadata: {} });
		currentMockSocket().simulateClose();
		await expect(promise).rejects.toBeInstanceOf(ConnectionLostError);
	});

	it("rejects with ConnectionClosedError on close() while pending", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const promise = conn.sendRequest({ op: "create_session", metadata: {} });
		conn.close();
		await expect(promise).rejects.toBeInstanceOf(ConnectionClosedError);
	});

	it("rejects new sendRequest immediately after close()", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		conn.close();
		await expect(conn.sendRequest({ op: "create_session", metadata: {} })).rejects.toBeInstanceOf(
			ConnectionClosedError,
		);
	});

	it("resolves MutationRejectedResponse rather than rejecting it", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const promise = conn.sendRequest({
			op: "mutate_session",
			subscription_id: 1,
			ack_seq: 0,
			update_id: 0,
			patch: [],
			ops: [],
		});
		const request_id = (currentMockSocket().sentParsed[0] as { request_id: number }).request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({
				type: "mutation_rejected",
				request_id,
				reason: "stale_ack",
				message: "",
			}),
		);
		const result = await promise;
		expect(result).toMatchObject({ type: "mutation_rejected", reason: "stale_ack" });
	});
});

// --- registerSubscription -------------------------------------------

describe("WorkstationServerConnection: registerSubscription", () => {
	it("sends a subscribe request and routes the snapshot to onFrame", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const { handler, frames } = recordingHandler();

		conn.registerSubscription({ kind: "resource", resource: "entities" }, handler);

		const sent = currentMockSocket().sentParsed[0] as { op: string; request_id: number };
		expect(sent.op).toBe("subscribe");
		const subscription_id = sent.request_id;

		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "resource_snapshot", subscription_id, data: [{ short_id: "s1" }] }),
		);

		expect(frames).toHaveLength(1);
		expect(frames[0]).toMatchObject({ type: "resource_snapshot" });
	});

	it("treats the subscribe ack as a silent no-op (no resolve, no warning)", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const { handler } = recordingHandler();
		conn.registerSubscription({ kind: "resource", resource: "services" }, handler);

		const subscription_id = (currentMockSocket().sentParsed[0] as { request_id: number })
			.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);

		expect(warnSpy).not.toHaveBeenCalled();
	});

	it("fires onClose('subscribe_failed') with a ProtocolError when the server rejects the subscribe", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const { handler, closes } = recordingHandler();
		conn.registerSubscription({ kind: "session", session_id: "ghost" }, handler);

		const subscription_id = (currentMockSocket().sentParsed[0] as { request_id: number })
			.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "error", request_id: subscription_id, message: "no such session" }),
		);

		expect(closes).toHaveLength(1);
		expect(closes[0]?.reason).toBe("subscribe_failed");
		expect(closes[0]?.error).toBeInstanceOf(ProtocolError);
	});

	it("fires onClose('reset') on socket drop", () => {
		vi.useFakeTimers();
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const { handler, closes } = recordingHandler();
		conn.registerSubscription({ kind: "resource", resource: "sessions" }, handler);
		currentMockSocket().simulateClose();

		expect(closes).toHaveLength(1);
		expect(closes[0]?.reason).toBe("reset");
	});

	it("fires onClose('unsubscribed') from handle.close() while open, and sends an unsubscribe request", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const { handler, closes } = recordingHandler();
		const handle = conn.registerSubscription({ kind: "resource", resource: "entities" }, handler);

		const subscription_id = (currentMockSocket().sentParsed[0] as { request_id: number })
			.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);

		handle.close();

		expect(closes).toHaveLength(1);
		expect(closes[0]?.reason).toBe("unsubscribed");

		const lastSent = currentMockSocket().sentParsed.at(-1) as {
			op: string;
			subscription_id: number;
		};
		expect(lastSent.op).toBe("unsubscribe");
		expect(lastSent.subscription_id).toBe(subscription_id);
	});

	it("handle.close() while reconnecting does not send an unsubscribe", () => {
		vi.useFakeTimers();
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const { handler } = recordingHandler();
		const handle = conn.registerSubscription({ kind: "resource", resource: "entities" }, handler);

		// Socket dropped, handler already received onClose("reset")
		currentMockSocket().simulateClose();

		// During reconnecting (no socket), close() should be a sync no-op:
		// no new wire messages, no errors.
		const sentBefore = currentMockSocket().sentMessages.length;
		handle.close();
		expect(currentMockSocket().sentMessages.length).toBe(sentBefore);
	});

	it("handle.close() before the subscribe ack still queues an unsubscribe (CONNECTING)", () => {
		const { conn } = makeConnection();
		// Socket is CONNECTING (not yet open).
		const { handler, closes } = recordingHandler();
		const handle = conn.registerSubscription({ kind: "resource", resource: "entities" }, handler);
		handle.close();

		expect(closes).toHaveLength(1);
		expect(closes[0]?.reason).toBe("unsubscribed");

		currentMockSocket().simulateOpen();
		const ops = currentMockSocket().sentParsed.map((m) => (m as { op: string }).op);
		expect(ops).toEqual(["subscribe", "unsubscribe"]);
	});

	it("connection.close() fires onClose('connector_closed') on every active subscription", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const a = recordingHandler();
		const b = recordingHandler();
		conn.registerSubscription({ kind: "resource", resource: "entities" }, a.handler);
		conn.registerSubscription({ kind: "resource", resource: "services" }, b.handler);

		await conn.close();

		expect(a.closes[0]?.reason).toBe("connector_closed");
		expect(b.closes[0]?.reason).toBe("connector_closed");
	});

	it("registerSubscription called after close() fires onClose('connector_closed') asynchronously", async () => {
		const { conn } = makeConnection();
		await conn.close();
		const { handler, closes } = recordingHandler();
		conn.registerSubscription({ kind: "resource", resource: "entities" }, handler);
		// Onclose is deferred via queueMicrotask; flush microtasks.
		await Promise.resolve();
		expect(closes[0]?.reason).toBe("connector_closed");
	});

	it("handle.close() is idempotent", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const { handler, closes } = recordingHandler();
		const handle = conn.registerSubscription({ kind: "resource", resource: "entities" }, handler);
		handle.close();
		handle.close();
		handle.close();
		expect(closes).toHaveLength(1);
	});
});

// --- Handler exception isolation ------------------------------------

describe("WorkstationServerConnection: handler exception isolation", () => {
	it("a throwing onFrame is caught; the socket keeps reading", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();

		const goodFrames: SubscriptionFrame[] = [];
		const goodHandler = {
			onFrame: (f: SubscriptionFrame) => goodFrames.push(f),
			onClose: () => {},
		};
		const badHandler = {
			onFrame: () => {
				throw new Error("kaboom");
			},
			onClose: () => {},
		};
		conn.registerSubscription({ kind: "resource", resource: "entities" }, badHandler);
		conn.registerSubscription({ kind: "resource", resource: "services" }, goodHandler);

		const badId = (currentMockSocket().sentParsed[0] as { request_id: number }).request_id;
		const goodId = (currentMockSocket().sentParsed[1] as { request_id: number }).request_id;

		currentMockSocket().simulateMessage(JSON.stringify({ type: "ack", request_id: badId }));
		currentMockSocket().simulateMessage(JSON.stringify({ type: "ack", request_id: goodId }));
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "resource_snapshot", subscription_id: badId, data: [] }),
		);
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "resource_snapshot", subscription_id: goodId, data: [] }),
		);

		expect(errorSpy).toHaveBeenCalled();
		expect(goodFrames).toHaveLength(1);
	});

	it("a throwing onClose is caught; other subscriptions still receive their close", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();

		const good = recordingHandler();
		const badHandler = {
			onFrame: () => {},
			onClose: () => {
				throw new Error("kaboom");
			},
		};
		conn.registerSubscription({ kind: "resource", resource: "entities" }, badHandler);
		conn.registerSubscription({ kind: "resource", resource: "services" }, good.handler);

		await conn.close();
		expect(errorSpy).toHaveBeenCalled();
		expect(good.closes[0]?.reason).toBe("connector_closed");
	});
});

// --- Reconnect ------------------------------------------------------

describe("WorkstationServerConnection: reconnect", () => {
	it("respects the backoff schedule and constructs a new socket on each attempt", async () => {
		vi.useFakeTimers();
		const { conn, store } = makeConnection();
		currentMockSocket().simulateOpen();

		currentMockSocket().simulateClose();
		expect(status(conn, store)).toBe("reconnecting");

		// First reconnect attempt at 100ms
		await vi.advanceTimersByTimeAsync(99);
		expect(currentMockSocket().readyState).toBe(MockWebSocket.CLOSED);
		await vi.advanceTimersByTimeAsync(1);
		expect(currentMockSocket().readyState).toBe(MockWebSocket.CONNECTING);

		// Fail again; next attempt at 250ms
		currentMockSocket().simulateClose();
		await vi.advanceTimersByTimeAsync(250);
		expect(currentMockSocket().readyState).toBe(MockWebSocket.CONNECTING);

		// Open on the third attempt
		currentMockSocket().simulateOpen();
		expect(status(conn, store)).toBe("open");
	});

	it("resets the reconnect attempt counter on successful open", async () => {
		vi.useFakeTimers();
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();

		currentMockSocket().simulateClose();
		await vi.advanceTimersByTimeAsync(100);
		currentMockSocket().simulateOpen();
		// We're back to "open"; subsequent drop should restart from
		// the first delay (100ms), not advance further into the
		// schedule.
		currentMockSocket().simulateClose();
		await vi.advanceTimersByTimeAsync(99);
		// Still waiting at 99ms — fresh schedule, not 250ms.
		expect(currentMockSocket().readyState).toBe(MockWebSocket.CLOSED);
		await vi.advanceTimersByTimeAsync(1);
		expect(currentMockSocket().readyState).toBe(MockWebSocket.CONNECTING);

		// Cleanup: close before the reconnect timer fires again
		void conn.close();
	});

	it("close() during 'reconnecting' cancels the pending reconnect", async () => {
		vi.useFakeTimers();
		const { conn, store } = makeConnection();
		currentMockSocket().simulateOpen();
		currentMockSocket().simulateClose();
		expect(status(conn, store)).toBe("reconnecting");
		await conn.close();
		// Advancing past the reconnect delay should not create a new socket.
		await vi.advanceTimersByTimeAsync(5000);
		expect(status(conn, store)).toBe("closed");
	});

	it("re-registering a subscription during onClose('reset') queues for the next socket", async () => {
		vi.useFakeTimers();
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();

		const { handler, frames } = recordingHandler();
		let reregistered = false;
		const reactiveHandler = {
			onFrame: (f: SubscriptionFrame) => handler.onFrame(f),
			onClose: (reason: SubscriptionCloseReason, error?: Error) => {
				handler.onClose(reason, error);
				if (reason === "reset" && !reregistered) {
					reregistered = true;
					conn.registerSubscription({ kind: "resource", resource: "entities" }, reactiveHandler);
				}
			},
		};
		conn.registerSubscription({ kind: "resource", resource: "entities" }, reactiveHandler);

		// Drop the socket; the handler should re-register.
		currentMockSocket().simulateClose();

		// Reconnect.
		await vi.advanceTimersByTimeAsync(100);
		currentMockSocket().simulateOpen();

		// The second subscribe should have flushed.
		const ops = currentMockSocket().sentParsed.map((m) => (m as { op: string }).op);
		expect(ops).toContain("subscribe");

		// And we can deliver a snapshot to it.
		const subscription_id = (
			currentMockSocket().sentParsed.find((m) => (m as { op: string }).op === "subscribe") as {
				request_id: number;
			}
		).request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "resource_snapshot", subscription_id, data: [] }),
		);
		expect(frames).toHaveLength(1);
	});
});

// --- Warnings on unexpected inbound ---------------------------------

describe("WorkstationServerConnection: warnings on unexpected conditions", () => {
	it("warns on a response with an unknown request_id", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "session_created", request_id: 9999, session_id: "x" }),
		);
		expect(warnSpy).toHaveBeenCalled();
		void conn.close();
	});

	it("warns on a frame with an unknown subscription_id", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "resource_snapshot", subscription_id: 9999, data: [] }),
		);
		expect(warnSpy).toHaveBeenCalled();
		void conn.close();
	});

	it("does NOT warn on the unsubscribe ack (handled by NOOP_PENDING)", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const { handler } = recordingHandler();
		const handle = conn.registerSubscription({ kind: "resource", resource: "entities" }, handler);
		const subId = (currentMockSocket().sentParsed[0] as { request_id: number }).request_id;
		currentMockSocket().simulateMessage(JSON.stringify({ type: "ack", request_id: subId }));
		handle.close();

		const unsub = currentMockSocket().sentParsed.at(-1) as { request_id: number };
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: unsub.request_id }),
		);

		expect(warnSpy).not.toHaveBeenCalled();
	});
});

// --- Concurrency ---------------------------------------------------

describe("WorkstationServerConnection: concurrency", () => {
	it("routes responses for multiple in-flight requests independently", async () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const p1 = conn.sendRequest({ op: "create_session", metadata: { tag: 1 } });
		const p2 = conn.sendRequest({ op: "create_session", metadata: { tag: 2 } });
		const id1 = (currentMockSocket().sentParsed[0] as { request_id: number }).request_id;
		const id2 = (currentMockSocket().sentParsed[1] as { request_id: number }).request_id;
		// Respond out of order.
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "session_created", request_id: id2, session_id: "s2" }),
		);
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "session_created", request_id: id1, session_id: "s1" }),
		);
		await expect(p1).resolves.toMatchObject({ session_id: "s1" });
		await expect(p2).resolves.toMatchObject({ session_id: "s2" });
	});

	it("routes frames to the right subscription when multiple are active", () => {
		const { conn } = makeConnection();
		currentMockSocket().simulateOpen();
		const a = recordingHandler();
		const b = recordingHandler();
		conn.registerSubscription({ kind: "resource", resource: "entities" }, a.handler);
		conn.registerSubscription({ kind: "resource", resource: "services" }, b.handler);
		const aId = (currentMockSocket().sentParsed[0] as { request_id: number }).request_id;
		const bId = (currentMockSocket().sentParsed[1] as { request_id: number }).request_id;
		currentMockSocket().simulateMessage(JSON.stringify({ type: "ack", request_id: aId }));
		currentMockSocket().simulateMessage(JSON.stringify({ type: "ack", request_id: bId }));
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "resource_snapshot", subscription_id: aId, data: [{ tag: "A" }] }),
		);
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "resource_snapshot", subscription_id: bId, data: [{ tag: "B" }] }),
		);
		expect((a.frames[0] as unknown as { data: { tag: string }[] }).data[0]?.tag).toBe("A");
		expect((b.frames[0] as unknown as { data: { tag: string }[] }).data[0]?.tag).toBe("B");
	});
});
