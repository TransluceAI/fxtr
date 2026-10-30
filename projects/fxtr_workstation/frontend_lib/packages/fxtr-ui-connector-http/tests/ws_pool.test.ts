import { beforeEach, describe, expect, it } from "vitest";

import { type WebSocketCtor, WsInvokeError, WsPool } from "../src/ws_pool";
import { MockWebSocket, currentMockSocket, resetMockWebSockets } from "./_mock_websocket";

const URL = "ws://host/ws";
const CTOR = MockWebSocket as unknown as WebSocketCtor;

/** Drain pending microtasks (lets an awaited `ensureOpen` continue to `send`). */
const tick = () => new Promise<void>((r) => setTimeout(r, 0));

function makePool() {
	const notifies: Array<{ inbox: string; payload: unknown }> = [];
	const drops: Array<{ url: string; generation: number }> = [];
	const pool = new WsPool({
		webSocketCtor: CTOR,
		onNotify: (inbox, payload) => notifies.push({ inbox, payload }),
		onDrop: (url, generation) => drops.push({ url, generation }),
	});
	return { pool, notifies, drops };
}

/** The id the pool stamped on the last frame it sent (asserts one exists). */
function lastSentId(sock: MockWebSocket): string {
	const frames = sock.sentParsed;
	const last = frames[frames.length - 1];
	if (!last || typeof last.id !== "string") throw new Error("no invoke frame sent");
	return last.id;
}

beforeEach(resetMockWebSockets);

const REQ = { service: "sched", method: "ping", args: [], kwargs: {} };

describe("WsPool: invoke", () => {
	it("opens on demand, sends an invoke frame, resolves the result", async () => {
		const { pool } = makePool();
		const p = pool.invoke(URL, REQ);
		const sock = currentMockSocket();
		expect(sock.url).toBe(URL);
		sock.simulateOpen();
		await tick();
		expect(sock.sentParsed[0]).toMatchObject({
			type: "invoke",
			service: "sched",
			method: "ping",
			args: [],
			kwargs: {},
		});
		sock.simulateMessage({ type: "result", id: lastSentId(sock), value: "pong" });
		await expect(p).resolves.toBe("pong");
	});

	it("rejects with WsInvokeError carrying the server error message", async () => {
		const { pool } = makePool();
		const p = pool.invoke(URL, { service: "x", method: "fail", args: [], kwargs: {} });
		const sock = currentMockSocket();
		sock.simulateOpen();
		await tick();
		sock.simulateMessage({ type: "error", id: lastSentId(sock), message: "boom" });
		await expect(p).rejects.toThrow(WsInvokeError);
		await expect(p).rejects.toThrow("boom");
	});

	it("reuses an open connection for later invokes (no new socket)", async () => {
		const { pool } = makePool();
		const p1 = pool.invoke(URL, { service: "s", method: "a", args: [], kwargs: {} });
		const sock = currentMockSocket();
		sock.simulateOpen();
		await tick();
		const p2 = pool.invoke(URL, { service: "s", method: "b", args: [], kwargs: {} });
		await tick();
		expect(MockWebSocket.instances.length).toBe(1);
		expect(sock.sentParsed.map((f) => f.method)).toEqual(["a", "b"]);
		for (const f of sock.sentParsed) {
			sock.simulateMessage({ type: "result", id: f.id, value: f.method });
		}
		await expect(p1).resolves.toBe("a");
		await expect(p2).resolves.toBe("b");
	});
});

describe("WsPool: notify routing", () => {
	it("routes notify frames to onNotify by inbox uuid", async () => {
		const { pool, notifies } = makePool();
		const p = pool.invoke(URL, { service: "s", method: "subscribe", args: [], kwargs: {} });
		const sock = currentMockSocket();
		sock.simulateOpen();
		await tick();
		sock.simulateMessage({ type: "notify", inbox: "inbox-1", payload: { hello: 1 } });
		expect(notifies).toEqual([{ inbox: "inbox-1", payload: { hello: 1 } }]);
		sock.simulateMessage({ type: "result", id: lastSentId(sock), value: null });
		await p;
	});
});

describe("WsPool: disconnect + generations", () => {
	it("on drop: rejects in-flight invokes and fires onDrop with the dead generation", async () => {
		const { pool, drops } = makePool();
		const p = pool.invoke(URL, REQ);
		const sock = currentMockSocket();
		sock.simulateOpen();
		await tick();
		expect(pool.liveGeneration(URL)).toBe(1);
		sock.simulateClose();
		await expect(p).rejects.toThrow(WsInvokeError);
		expect(drops).toEqual([{ url: URL, generation: 1 }]);
		expect(pool.liveGeneration(URL)).toBe(0); // dropped → no live generation
	});

	it("reconnects on the next invoke with a bumped generation", async () => {
		const { pool } = makePool();
		const p1 = pool.invoke(URL, REQ);
		currentMockSocket().simulateOpen();
		await tick();
		currentMockSocket().simulateClose();
		await expect(p1).rejects.toThrow();
		expect(MockWebSocket.instances.length).toBe(1);

		const p2 = pool.invoke(URL, REQ);
		expect(MockWebSocket.instances.length).toBe(2); // fresh socket
		const sock2 = currentMockSocket();
		sock2.simulateOpen();
		await tick();
		expect(pool.liveGeneration(URL)).toBe(2);
		sock2.simulateMessage({ type: "result", id: lastSentId(sock2), value: "ok" });
		await expect(p2).resolves.toBe("ok");
	});

	it("rejects a stale transient invoke without reconnecting", async () => {
		const { pool } = makePool();
		const p1 = pool.invoke(URL, { service: "s", method: "subscribe", args: [], kwargs: {} });
		currentMockSocket().simulateOpen();
		await tick();
		currentMockSocket().simulateClose();
		await expect(p1).rejects.toThrow();
		expect(MockWebSocket.instances.length).toBe(1);

		await expect(
			pool.invoke(
				URL,
				{ service: "transient-x", method: "unsubscribe", args: [], kwargs: {} },
				{ expectedGeneration: 1 },
			),
		).rejects.toThrow(/transient service broken/);
		expect(MockWebSocket.instances.length).toBe(1); // did NOT open a new socket
	});

	it("routes a live transient invoke when the generation matches", async () => {
		const { pool } = makePool();
		const p1 = pool.invoke(URL, { service: "s", method: "subscribe", args: [], kwargs: {} });
		const sock = currentMockSocket();
		sock.simulateOpen();
		await tick();
		expect(pool.isGenerationLive(URL, 1)).toBe(true);
		const p2 = pool.invoke(
			URL,
			{ service: "transient-x", method: "unsubscribe", args: [], kwargs: {} },
			{ expectedGeneration: 1 },
		);
		await tick();
		const transientFrame = sock.sentParsed.find((f) => f.service === "transient-x");
		expect(transientFrame).toBeDefined();
		for (const f of sock.sentParsed) {
			sock.simulateMessage({ type: "result", id: f.id, value: null });
		}
		await p1;
		await expect(p2).resolves.toBeNull();
	});
});

describe("WsPool: close", () => {
	it("rejects in-flight invokes and refuses new ones", async () => {
		const { pool } = makePool();
		const p = pool.invoke(URL, REQ);
		currentMockSocket().simulateOpen();
		await tick();
		pool.close();
		await expect(p).rejects.toThrow(WsInvokeError);
		await expect(pool.invoke(URL, REQ)).rejects.toThrow("closed");
	});
});
