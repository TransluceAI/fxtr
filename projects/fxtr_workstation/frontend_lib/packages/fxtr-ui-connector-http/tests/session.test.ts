import type { CallbackInboxStub, EntityRefStub, OpResultStub, ServiceRefStub } from "@fxtr-ui/core";
import { memoryStorage } from "@fxtr-ui/core";
import { type FxtrCbor, fxtrCborDumps } from "@fxtr/core";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HttpConnector } from "../src/connector";
import type { FetchFn } from "../src/entity_data";
import type { HttpSession } from "../src/session";
import type { WebSocketCtor } from "../src/ws_pool";
import { MockWebSocket, currentMockSocket, resetMockWebSockets } from "./_mock_websocket";

const BASE = "http://host:8000";
const tick = () => new Promise<void>((r) => setTimeout(r, 0));
async function waitFor(pred: () => boolean, label = "condition"): Promise<void> {
	for (let i = 0; i < 50; i++) {
		if (pred()) return;
		await tick();
	}
	throw new Error(`waitFor timed out: ${label}`);
}

const DISCOVERY = {
	entities: { "e/root": "did-root" },
	services: {
		echo: { kind: "http", url: "/svc/echo", method: "POST", service_type_id: "!echo-type" },
		feed: { kind: "ws", url: "ws://host:8000/ws", name: "feed", service_type_id: "!feed-type" },
		hang: { kind: "http", url: "/svc/hang", method: "POST", service_type_id: "!hang-type" },
	},
};

function cborResponse(envelope: FxtrCbor): Response {
	const bytes = fxtrCborDumps(envelope);
	return {
		status: 200,
		ok: true,
		arrayBuffer: async () => bytes.slice().buffer,
	} as unknown as Response;
}
function jsonResponse(body: unknown): Response {
	return { status: 200, ok: true, json: async () => body } as unknown as Response;
}

/** Per-test fetch counters (reset in `beforeEach`). */
const fetchStats = { dataLoads: 0 };

/** Serves discovery, the root entity, and a POST echo service. */
function makeFetch(): FetchFn {
	return async (url, init) => {
		if (url === `${BASE}/discovery`) return jsonResponse(DISCOVERY);
		if (url === `${BASE}/data/did-root`) {
			fetchStats.dataLoads++;
			return cborResponse({ bytes: fxtrCborDumps({ label: "hi" }) });
		}
		if (url === `${BASE}/svc/echo`) {
			const body = JSON.parse(String(init?.body)) as { method: string; args: unknown[] };
			// An http (GET/POST) service has no live feed: answer a
			// `subscribe` with null (no transient unsubscribe handle).
			if (body.method === "subscribe") return jsonResponse(null);
			return jsonResponse(body.args[0]); // echo arg0 (already resolved-mode wire)
		}
		// A service whose invoke never resolves (for the in-flight-at-save case).
		if (url === `${BASE}/svc/hang`) return new Promise<Response>(() => {});
		throw new Error(`unexpected fetch ${url}`);
	};
}

const CTOR = MockWebSocket as unknown as WebSocketCtor;

let connector: HttpConnector;
let entityRef: EntityRefStub;
let echoRef: ServiceRefStub;
let feedRef: ServiceRefStub;

beforeEach(async () => {
	resetMockWebSockets();
	fetchStats.dataLoads = 0;
	connector = new HttpConnector({
		baseUrl: BASE,
		storage: memoryStorage(),
		fetchFn: makeFetch(),
		webSocketCtor: CTOR,
	});
	await connector.refresh();
	entityRef = connector.store.get(connector.listEntities().records)[0]?.ref as EntityRefStub;
	const svcs = connector.store.get(connector.listServices().records);
	echoRef = svcs.find((s) => s.name === "echo")?.ref as ServiceRefStub;
	feedRef = svcs.find((s) => s.name === "feed")?.ref as ServiceRefStub;
});

/** Read the current result status of an op-result stub. */
function status(
	s: HttpSession,
	stub: OpResultStub,
): { status: string; value?: unknown; message?: string } {
	return s.store.get(stub.result);
}

describe("HttpSession: load / invoke / metadata", () => {
	it("allocateLoad fills the slot with the decoded entity", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let load!: OpResultStub;
		s.store.set(s.userState, (_prev, ctx) => {
			load = ctx.allocateLoad(entityRef);
			return { x: load };
		});
		await waitFor(() => status(s, load).status === "filled", "load filled");
		expect(status(s, load)).toEqual({ status: "filled", value: { label: "hi" } });
	});

	it("allocateInvoke against an http service echoes the arg", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let inv!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			inv = ctx.allocateInvoke(echoRef, "echo", [42]);
			return { r: inv };
		});
		await waitFor(() => status(s, inv).status === "filled", "invoke filled");
		expect(status(s, inv).value).toBe(42);
	});

	it("allocateGetServiceMetadata resolves type_id_string locally", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let meta!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			meta = ctx.allocateGetServiceMetadata(echoRef);
			return { m: meta };
		});
		await waitFor(() => status(s, meta).status === "filled", "metadata filled");
		expect(status(s, meta).value).toEqual({ type_id_string: "!echo-type" });
	});

	it("dedups loads of the same ref within an updater", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let a!: OpResultStub;
		let b!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			a = ctx.allocateLoad(entityRef);
			b = ctx.allocateLoad(entityRef);
			return { a, b };
		});
		expect(a).toBe(b);
	});

	// Regression: behavioral-audit I1 — committed dedup must survive
	// intervening flushes, like the workstation's wire-visible dedup map.
	it("dedups loads of the same ref ACROSS ticks (no second fetch)", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let a!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			a = ctx.allocateLoad(entityRef);
			return { a };
		});
		await waitFor(() => status(s, a).status === "filled", "a filled");
		expect(fetchStats.dataLoads).toBe(1);

		// An unrelated write flushes without re-allocating the load.
		s.store.set(s.userState, (prev) => ({ ...(prev as object), z: 1 }));
		await tick();

		// Loading the same entity in a later tick reuses the slot — the
		// stub is the same instance, already filled, and no refetch.
		let b!: OpResultStub;
		s.store.set(s.userState, (prev, ctx) => {
			b = ctx.allocateLoad(entityRef);
			return { ...(prev as object), b };
		});
		await tick();
		expect(b).toBe(a);
		expect(status(s, b)).toEqual({ status: "filled", value: { label: "hi" } });
		expect(fetchStats.dataLoads).toBe(1); // no second fetch

		// Once the stub leaves committed state, dedup is dropped (a later
		// load mints a fresh slot + refetches) — matching the workstation.
		s.store.set(s.userState, () => ({}));
		await waitFor(() => s.store.get(s.committedSlotSetAtom).size === 0, "GC'd");
		let c!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			c = ctx.allocateLoad(entityRef);
			return { c };
		});
		await waitFor(() => status(s, c).status === "filled", "c filled");
		expect(c).not.toBe(a);
		expect(fetchStats.dataLoads).toBe(2);
	});

	it("GCs slot fills once the stub leaves committed state", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let load!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			load = ctx.allocateLoad(entityRef);
			return { x: load };
		});
		await waitFor(() => status(s, load).status === "filled", "load filled");
		// Drop the stub from committed state → its fill is GC'd. A dangling
		// reference then reads `pending` (no fill, no longer referenced).
		expect(s.store.get(s.committedSlotSetAtom).size).toBe(1);
		s.store.set(s.userState, () => ({}));
		await waitFor(() => status(s, load).status !== "filled", "fill GC'd");
		expect(status(s, load).status).toBe("pending");
		expect(s.store.get(s.committedSlotSetAtom).size).toBe(0);
	});
});

describe("HttpSession: callbacks over ws (subscribe_to + notify + drop)", () => {
	it("subscribe_to connects, notifies route to the inbox, and a drop breaks it", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let inbox!: CallbackInboxStub;
		// Subscribe the inbox to the ws `feed` source (handle-targeted
		// subscribe_to → reinterpreted as feed.subscribe(handle)).
		s.store.set(s.userState, (_p, ctx) => {
			inbox = ctx.allocateCallback();
			// Store the invoke result too — the same-tick contract requires
			// the slot to be referenced from committed state to fire.
			const sub = ctx.allocateInvoke(inbox.handle, "subscribe_to", [feedRef]);
			return { inbox, sub };
		});
		// The ws connects on demand; drive it open, then answer the subscribe.
		await waitFor(() => MockWebSocket.instances.length > 0, "ws opened");
		const sock = currentMockSocket();
		sock.simulateOpen();
		await waitFor(() => sock.sentParsed.some((f) => f.method === "subscribe"), "subscribe sent");
		const subFrame = sock.sentParsed.find((f) => f.method === "subscribe");
		// Reply with a transient unsubscribe handle.
		sock.simulateMessage({
			type: "result",
			id: subFrame?.id,
			value: ["service", { kind: "transient", id: "unsub-1" }],
		});
		await waitFor(() => s.store.get(inbox.connectionState) === "connected", "connected");

		// A notify routes to the inbox by uuid.
		const notifyFrame = sock.sentParsed.find((f) => f.method === "subscribe");
		const inboxUuid = (notifyFrame?.args as unknown[][])?.[0]; // ["callback", uuid]
		const uuid = Array.isArray(inboxUuid) ? inboxUuid[1] : undefined;
		sock.simulateMessage({ type: "notify", inbox: uuid, payload: "ping" });
		await waitFor(() => s.store.get(inbox.events).length === 1, "notify delivered");
		expect(s.store.get(inbox.events)).toEqual(["ping"]);

		// A drop breaks the inbox's connection state.
		sock.simulateClose();
		await waitFor(() => s.store.get(inbox.connectionState) === "broken", "broken on drop");
		expect(s.store.get(inbox.subscriptionBreakReason)).toMatch(/dropped/);
	});

	// Regression: behavioral-audit I2 — the subscribe_to op-result fills
	// with null (matching the workstation's `subscribe_to -> None`), not
	// the transient unsubscribe handle the ws source returned. The
	// subscription itself is still established (recorded internally).
	it("subscribe_to op-result fills with null, not the unsubscribe handle", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let inbox!: CallbackInboxStub;
		let sub!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			inbox = ctx.allocateCallback();
			sub = ctx.allocateInvoke(inbox.handle, "subscribe_to", [feedRef]);
			return { inbox, sub };
		});
		await waitFor(() => MockWebSocket.instances.length > 0, "ws opened");
		const sock = currentMockSocket();
		sock.simulateOpen();
		await waitFor(() => sock.sentParsed.some((f) => f.method === "subscribe"), "subscribe sent");
		const subFrame = sock.sentParsed.find((f) => f.method === "subscribe");
		sock.simulateMessage({
			type: "result",
			id: subFrame?.id,
			value: ["service", { kind: "transient", id: "unsub-1" }],
		});
		await waitFor(() => status(s, sub).status === "filled", "sub filled");
		expect(status(s, sub)).toEqual({ status: "filled", value: null });
		await waitFor(() => s.store.get(inbox.connectionState) === "connected", "connected");
	});

	// Regression: behavioral-audit I3 — subscribing to a non-pushing
	// (http/GET) source leaves connectionState idle (no live channel),
	// uniform with the documented base-class behavior. The op-result still
	// fills with null and no ws connection is opened.
	it("subscribe_to against an http source leaves connectionState idle", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let inbox!: CallbackInboxStub;
		let sub!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			inbox = ctx.allocateCallback();
			sub = ctx.allocateInvoke(inbox.handle, "subscribe_to", [echoRef]);
			return { inbox, sub };
		});
		await waitFor(() => status(s, sub).status === "filled", "sub filled");
		expect(status(s, sub)).toEqual({ status: "filled", value: null });
		expect(s.store.get(inbox.connectionState)).toBe("idle");
		expect(s.store.get(inbox.subscriptionBreakReason)).toBeNull();
		expect(MockWebSocket.instances.length).toBe(0); // no ws for an http source
	});
});

describe("HttpSession: lifecycle", () => {
	it("starts connected and detaches to closed; attachSession interns one instance", async () => {
		const s = (await connector.createSession()) as HttpSession;
		expect(s.store.get(s.lifecycle)).toBe("connected");
		expect(connector.attachSession(s.sessionId)).toBe(s); // interned
		await s.detach();
		expect(s.store.get(s.lifecycle)).toBe("closed");
	});

	it("terminate applies finalState, closes, and drops the session from the connector", async () => {
		const s = (await connector.createSession()) as HttpSession;
		await s.terminate({ closed: "bye" });
		// finalState is observable on userState before/at close.
		expect(s.store.get(s.userState)).toEqual({ closed: "bye" });
		expect(s.store.get(s.lifecycle)).toBe("closed");
		// Storage was deleted, so re-attaching now finds nothing: a fresh
		// (terminal `unavailable`) instance, distinct from the old one.
		const reattached = connector.attachSession(s.sessionId);
		expect(reattached).not.toBe(s);
		expect(reattached.store.get(reattached.lifecycle)).toBe("unavailable");
		// Repeat terminate is a no-op; writes after terminate reject.
		await s.terminate();
		expect(() => s.store.set(s.userState, {})).toThrow();
	});

	it("attachSession to an unknown id yields a terminal `unavailable` session (not a silent create)", async () => {
		const before = connector.listPersistedSessions().length;
		const s = connector.attachSession("00000000-0000-0000-0000-000000000000") as HttpSession;
		expect(s.store.get(s.lifecycle)).toBe("unavailable");
		// Not registered / persisted, and not interned.
		expect(connector.listPersistedSessions().length).toBe(before);
		expect(connector.attachSession("00000000-0000-0000-0000-000000000000")).not.toBe(s);
		// Terminal: writes reject, detach/terminate are no-ops that don't
		// resurrect it or touch storage.
		expect(() => s.store.set(s.userState, {})).toThrow();
		await s.detach();
		await s.terminate();
		expect(s.store.get(s.lifecycle)).toBe("unavailable");
		expect(connector.listPersistedSessions().length).toBe(before);
	});

	it("terminate tears down a live ws inbox (best-effort unsubscribe; notify stops routing)", async () => {
		const s = (await connector.createSession()) as HttpSession;
		let inbox!: CallbackInboxStub;
		s.store.set(s.userState, (_p, ctx) => {
			inbox = ctx.allocateCallback();
			const sub = ctx.allocateInvoke(inbox.handle, "subscribe_to", [feedRef]);
			return { inbox, sub };
		});
		await waitFor(() => MockWebSocket.instances.length > 0, "ws opened");
		const sock = currentMockSocket();
		sock.simulateOpen();
		await waitFor(() => sock.sentParsed.some((f) => f.method === "subscribe"), "subscribe sent");
		const subFrame = sock.sentParsed.find((f) => f.method === "subscribe");
		const uuidArg = (subFrame?.args as unknown[][])?.[0]; // ["callback", uuid]
		const uuid = Array.isArray(uuidArg) ? uuidArg[1] : undefined;
		sock.simulateMessage({
			type: "result",
			id: subFrame?.id,
			value: ["service", { kind: "transient", id: "unsub-1" }],
		});
		await waitFor(() => s.store.get(inbox.connectionState) === "connected", "connected");

		await s.terminate();
		// Best-effort unsubscribe was attempted over the transient handle.
		await waitFor(
			() => sock.sentParsed.some((f) => f.method === "unsubscribe"),
			"unsubscribe sent",
		);
		// The notify route is gone — a later notify is dropped, not delivered.
		const before = s.store.get(inbox.events).length;
		sock.simulateMessage({ type: "notify", inbox: uuid, payload: "late" });
		await tick();
		expect(s.store.get(inbox.events).length).toBe(before);
		expect(s.store.get(s.lifecycle)).toBe("closed");
	});
});

describe("HttpSession: persistence + rehydration", () => {
	const makeConn = (storage: ReturnType<typeof memoryStorage>): HttpConnector =>
		new HttpConnector({
			baseUrl: BASE,
			storage,
			fetchFn: makeFetch(),
			webSocketCtor: CTOR,
			namespace: "p",
		});
	const refsOf = (c: HttpConnector) => {
		const e = c.store.get(c.listEntities().records)[0]?.ref as EntityRefStub;
		const svcs = c.store.get(c.listServices().records);
		return {
			e,
			echo: svcs.find((s) => s.name === "echo")?.ref as ServiceRefStub,
			hang: svcs.find((s) => s.name === "hang")?.ref as ServiceRefStub,
		};
	};

	it("round-trips a committed load + invoke across a reload", async () => {
		const storage = memoryStorage();
		const c1 = makeConn(storage);
		await c1.refresh();
		const r1 = refsOf(c1);
		const s1 = (await c1.createSession({ title: "t" })) as HttpSession;
		let load!: OpResultStub;
		let inv!: OpResultStub;
		s1.store.set(s1.userState, (_p, ctx) => {
			load = ctx.allocateLoad(r1.e);
			inv = ctx.allocateInvoke(r1.echo, "echo", [7]);
			return { load, inv };
		});
		await waitFor(
			() => status(s1, load).status === "filled" && status(s1, inv).status === "filled",
			"s1 filled",
		);
		c1.close(); // persists sessions + shortId table synchronously

		const c2 = makeConn(storage);
		await c2.refresh();
		const s2 = c2.attachSession(s1.sessionId) as HttpSession;
		expect(s2).not.toBe(s1);
		const user2 = s2.store.get(s2.userState) as { load: OpResultStub; inv: OpResultStub };
		// Invoke fill restored verbatim.
		expect(status(s2, user2.inv)).toEqual({ status: "filled", value: 7 });
		// Load payload not persisted → rehydrates pending, then refetches.
		await waitFor(() => status(s2, user2.load).status === "filled", "load refetched");
		expect(status(s2, user2.load).value).toEqual({ label: "hi" });
	});

	it('rehydrates an in-flight invoke as an errored "interrupted" fill', async () => {
		const storage = memoryStorage();
		const c1 = makeConn(storage);
		await c1.refresh();
		const r1 = refsOf(c1);
		const s1 = (await c1.createSession()) as HttpSession;
		let inv!: OpResultStub;
		s1.store.set(s1.userState, (_p, ctx) => {
			inv = ctx.allocateInvoke(r1.hang, "go", []);
			return { inv };
		});
		await tick(); // commit + fire; the hang fetch never resolves
		expect(status(s1, inv).status).toBe("pending");
		c1.close();

		const c2 = makeConn(storage);
		await c2.refresh();
		const s2 = c2.attachSession(s1.sessionId) as HttpSession;
		const inv2 = (s2.store.get(s2.userState) as { inv: OpResultStub }).inv;
		expect(status(s2, inv2).status).toBe("errored");
		expect(status(s2, inv2).message).toMatch(/interrupted/);
	});

	it("rehydrates a transient service stub born-broken", async () => {
		const storage = memoryStorage();
		const c1 = makeConn(storage);
		await c1.refresh();
		const s1 = (await c1.createSession()) as HttpSession;
		// A transient service in committed state (e.g. an unsubscribe handle
		// the user stored) — its ws generation can't survive a reload.
		const tref = c1.internServiceByDescriptor(
			{ kind: "transient", id: "unsub-x" },
			{ url: "ws://host:8000/ws", generation: 1 },
		);
		s1.store.set(s1.userState, () => ({ t: tref }));
		await tick();
		c1.close();

		const c2 = makeConn(storage);
		await c2.refresh();
		const s2 = c2.attachSession(s1.sessionId) as HttpSession;
		const tref2 = (s2.store.get(s2.userState) as { t: ServiceRefStub }).t;
		expect(c2.store.get(c2.brokenServices).has(tref2.shortId)).toBe(true);
	});

	it("detach retains storage; reattach rehydrates committed state", async () => {
		const storage = memoryStorage();
		const c1 = makeConn(storage);
		await c1.refresh();
		const r1 = refsOf(c1);
		const s1 = (await c1.createSession()) as HttpSession;
		s1.store.set(s1.userState, () => ({ count: 5, e: r1.e }));
		await tick();
		await s1.detach(); // retains storage (unlike terminate)
		const s1b = c1.attachSession(s1.sessionId) as HttpSession; // new instance, rehydrated
		expect(s1b).not.toBe(s1);
		const user = s1b.store.get(s1b.userState) as { count: number; e: EntityRefStub };
		expect(user.count).toBe(5);
		expect(user.e.shortId).toBe(r1.e.shortId);
	});

	it("terminate deletes the persisted blob + registry entry", async () => {
		const storage = memoryStorage();
		const c1 = makeConn(storage);
		await c1.refresh();
		const s1 = (await c1.createSession({ title: "doomed" })) as HttpSession;
		expect(c1.listPersistedSessions().some((e) => e.sessionId === s1.sessionId)).toBe(true);
		await s1.terminate();
		expect(c1.listPersistedSessions().some((e) => e.sessionId === s1.sessionId)).toBe(false);
		// A fresh connector sees no registry entry + no blob to rehydrate.
		const c2 = makeConn(storage);
		await c2.refresh();
		expect(c2.listPersistedSessions().some((e) => e.sessionId === s1.sessionId)).toBe(false);
	});

	it("a corrupt or unsupported blob is unavailable, not crash nor a silent fresh session", async () => {
		const storage = memoryStorage();
		const c1 = makeConn(storage);
		await c1.refresh();
		const id = ((await c1.createSession()) as HttpSession).sessionId;
		c1.close();
		const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});

		// Invalid JSON → loadSessionBlob returns undefined → can't restore.
		storage.setItem(`fxtrhttp:p:session:${id}`, "{ not json");
		const c2 = makeConn(storage);
		await c2.refresh();
		const s2 = c2.attachSession(id) as HttpSession;
		expect(s2.store.get(s2.lifecycle)).toBe("unavailable");

		// Valid JSON, unsupported version → restoreFrom returns false → can't restore.
		storage.setItem(`fxtrhttp:p:session:${id}`, JSON.stringify({ version: 999 }));
		const c3 = makeConn(storage);
		await c3.refresh();
		const s3 = c3.attachSession(id) as HttpSession;
		expect(s3.store.get(s3.lifecycle)).toBe("unavailable");
		// Loud about the failure, but didn't crash.
		expect(errSpy).toHaveBeenCalled();
		errSpy.mockRestore();
	});
});
