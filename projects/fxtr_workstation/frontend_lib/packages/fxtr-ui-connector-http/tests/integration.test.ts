/**
 * End-to-end integration tests: the real `HttpConnector` (global fetch +
 * global WebSocket) driving the kitchen-sink Python backend
 * (`_demo_backend.py`) spawned over the wire via `_integration_harness.ts`.
 *
 * Covers connect/discovery, entity load + child-ref decode, GET/POST/WS
 * invoke, an undiscoverable returned-descriptor service, the callback
 * inbox happy path (ordered notifies + cursor advance), invoke errors,
 * service + entity metadata, and reload→rehydrate. Dynamic dispatch is
 * exercised transitively (there's no dispatch backend).
 *
 * Like `connector-workstation/tests/integration.test.ts`, these run by
 * default in `vitest run` and require local `uv` + the workstation env
 * (which carries `fxtr2`, where the backend library lives).
 */

import type { CallbackInboxStub, EntityRefStub, OpResultStub, ServiceRefStub } from "@fxtr-ui/core";
import { memoryStorage } from "@fxtr-ui/core";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { HttpConnector } from "../src/connector";
import type { HttpServiceRecord } from "../src/connector";
import type { HttpSession } from "../src/session";
import { type DemoBackend, startDemoBackend } from "./_integration_harness";

const tick = () => new Promise<void>((r) => setTimeout(r, 10));
async function waitFor(pred: () => boolean, label = "condition"): Promise<void> {
	for (let i = 0; i < 200; i++) {
		if (pred()) return;
		await tick();
	}
	throw new Error(`waitFor timed out: ${label}`);
}

function status(
	s: HttpSession,
	stub: OpResultStub,
): { status: string; value?: unknown; message?: string } {
	return s.store.get(stub.result);
}

let backend: DemoBackend;

beforeAll(async () => {
	backend = await startDemoBackend();
}, 40_000);

afterAll(async () => {
	await backend?.close();
}, 30_000);

// Safety net: close every connector a test made, even if the test threw
// before its own `c.close()`. A leaked connector keeps a ws socket open,
// which would park a server read loop and stall the backend's graceful
// shutdown at `afterAll`. `close()` is idempotent, so the explicit
// in-test closes (some of which must run mid-test, e.g. to persist before
// rehydrate) still work.
const liveConnectors: HttpConnector[] = [];
afterEach(() => {
	for (const c of liveConnectors.splice(0)) c.close();
});

function makeConnector(storage = memoryStorage()): HttpConnector {
	const c = new HttpConnector({ baseUrl: backend.baseUrl, storage });
	liveConnectors.push(c);
	return c;
}

function refs(c: HttpConnector): {
	root: EntityRefStub;
	echo: HttpServiceRecord;
	data: HttpServiceRecord;
	feed: HttpServiceRecord;
} {
	const ents = c.store.get(c.listEntities().records);
	const svcs = c.store.get(c.listServices().records);
	const find = (n: string) => svcs.find((s) => s.name === n) as HttpServiceRecord;
	return {
		root: ents.find((e) => e.name === "root")?.ref as EntityRefStub,
		echo: find("echo"),
		data: find("data"),
		feed: find("feed"),
	};
}

describe("connector-http integration (real backend over the wire)", () => {
	it("connects + serves discovery (entities + services with type ids)", async () => {
		const c = makeConnector();
		await c.refresh();
		const ents = c.store
			.get(c.listEntities().records)
			.map((e) => e.name)
			.sort();
		expect(ents).toEqual(["child", "prov_root", "root"]);
		const r = refs(c);
		expect(r.echo.typeIdString).toBe("!demo_http_echo");
		expect(r.data.descriptor.kind).toBe("http");
		expect(r.feed.descriptor.kind).toBe("ws");
		c.close();
	});

	it("loads an entity and decodes its child ref (traversable)", async () => {
		const c = makeConnector();
		await c.refresh();
		const r = refs(c);
		const s = (await c.createSession()) as HttpSession;
		let load!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			load = ctx.allocateLoad(r.root);
			return { load };
		});
		await waitFor(() => status(s, load).status === "filled", "root loaded");
		const parent = status(s, load).value as { title: string; child: EntityRefStub };
		expect(parent.title).toBe("root");
		expect(parent.child).toBeTruthy();

		// Traverse to the child.
		let childLoad!: OpResultStub;
		s.store.set(s.userState, (prev, ctx) => {
			childLoad = ctx.allocateLoad(parent.child);
			return { ...(prev as object), childLoad };
		});
		await waitFor(() => status(s, childLoad).status === "filled", "child loaded");
		const child = status(s, childLoad).value as { label: string; value: number };
		expect(child.label).toBe("kid");
		expect(child.value).toBe(42);
		c.close();
	});

	it("invokes a GET service (echo) and a POST service (entity-returning)", async () => {
		const c = makeConnector();
		await c.refresh();
		const r = refs(c);
		const s = (await c.createSession()) as HttpSession;
		let echoOp!: OpResultStub;
		let rootOp!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			echoOp = ctx.allocateInvoke(r.echo.ref, "echo", [{ hi: 5 }]);
			rootOp = ctx.allocateInvoke(r.data.ref, "get_root", []);
			return { echoOp, rootOp };
		});
		await waitFor(
			() => status(s, echoOp).status === "filled" && status(s, rootOp).status === "filled",
			"invokes filled",
		);
		expect(status(s, echoOp).value).toEqual({ hi: 5 });
		// get_root returns an entity ref; load it to confirm it's the root.
		const rootRef = status(s, rootOp).value as EntityRefStub<{ title: string }>;
		const loaded = await c.loadEntityData(rootRef);
		expect(loaded.title).toBe("root");
		c.close();
	});

	it("invokes a service that returns an undiscoverable service descriptor", async () => {
		const c = makeConnector();
		await c.refresh();
		const r = refs(c);
		const s = (await c.createSession()) as HttpSession;
		let mk!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			mk = ctx.allocateInvoke(r.data.ref, "make_service", []);
			return { mk };
		});
		await waitFor(() => status(s, mk).status === "filled", "make_service filled");
		const hidden = status(s, mk).value as ServiceRefStub;
		expect(hidden).toBeTruthy();
		// Invoke the returned (undiscoverable) service.
		let ping!: OpResultStub;
		s.store.set(s.userState, (prev, ctx) => {
			ping = ctx.allocateInvoke(hidden, "ping", []);
			return { ...(prev as object), ping };
		});
		await waitFor(() => status(s, ping).status === "filled", "hidden ping filled");
		expect(status(s, ping).value).toBe("hidden-pong");
		c.close();
	});

	it("invokes over ws ($fxtr.type) and surfaces invoke errors as errored fills", async () => {
		const c = makeConnector();
		await c.refresh();
		const r = refs(c);
		const s = (await c.createSession()) as HttpSession;
		let typeOp!: OpResultStub;
		let boomOp!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			typeOp = ctx.allocateInvoke(r.feed.ref, "$fxtr.type", []); // ws invoke
			boomOp = ctx.allocateInvoke(r.data.ref, "boom", []); // http error
			return { typeOp, boomOp };
		});
		await waitFor(() => status(s, typeOp).status === "filled", "ws $fxtr.type filled");
		// A ws invoke returning an entity ref (the type entity).
		expect(status(s, typeOp).value).toBeTruthy();
		await waitFor(() => status(s, boomOp).status === "errored", "boom errored");
		expect(status(s, boomOp).message).toMatch(/boom/);
		c.close();
	});

	it("callback inbox over ws: ordered notifies, connected, cursor advance", async () => {
		const c = makeConnector();
		await c.refresh();
		const r = refs(c);
		const s = (await c.createSession()) as HttpSession;
		let inbox!: CallbackInboxStub;
		let sub!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			inbox = ctx.allocateCallback();
			sub = ctx.allocateInvoke(inbox.handle, "subscribe_to", [r.feed.ref]);
			return { inbox, sub };
		});
		// Three ordered notifies arrive (emitted by `subscribe` before its result).
		await waitFor(() => s.store.get(inbox.events).length === 3, "3 notifies");
		expect(s.store.get(inbox.events)).toEqual(["n0", "n1", "n2"]);
		// subscribe_to fills the op-result with null and flips connectionState.
		await waitFor(() => status(s, sub).status === "filled", "subscribe filled");
		expect(status(s, sub).value).toBeNull();
		expect(s.store.get(inbox.connectionState)).toBe("connected");

		// Cursor advance: a sliced view drops consumed events.
		const advanced = inbox.sliced(s.store.get(inbox.events).length);
		expect(s.store.get(advanced.events)).toEqual([]);
		c.close();
	});

	it("resolves service + entity metadata", async () => {
		const c = makeConnector();
		await c.refresh();
		const r = refs(c);
		const s = (await c.createSession()) as HttpSession;
		let svcMeta!: OpResultStub;
		let entMeta!: OpResultStub;
		s.store.set(s.userState, (_p, ctx) => {
			svcMeta = ctx.allocateGetServiceMetadata(r.echo.ref);
			entMeta = ctx.allocateGetEntityMetadata(r.root);
			return { svcMeta, entMeta };
		});
		await waitFor(
			() => status(s, svcMeta).status === "filled" && status(s, entMeta).status === "filled",
			"metadata filled",
		);
		expect(status(s, svcMeta).value).toEqual({ type_id_string: "!demo_http_echo" });
		const em = status(s, entMeta).value as { id_string: string; unprovenanced?: unknown };
		expect(em.id_string).toBe(
			c.store.get(c.listEntities().records).find((e) => e.name === "root")?.idString,
		);
		expect(em.unprovenanced).toBeTruthy();
		c.close();
	});

	it("surfaces provenance for a layered entity, and its loaded child is layered (nested)", async () => {
		const c = makeConnector();
		await c.refresh();
		const provRoot = c.store.get(c.listEntities().records).find((e) => e.name === "prov_root")
			?.ref as EntityRefStub;
		expect(provRoot).toBeTruthy();

		// Top-level provenance: the layered entity's metadata carries a
		// provenance_parent (the workstation parity target).
		const meta = await c.getEntityMetadata(provRoot);
		expect(meta.provenance_parent).toBeTruthy();
		expect(meta.provenance_parent_id_string).toBeTruthy();
		expect(meta.unprovenanced).toBeTruthy();

		// The invariant: loading a layered entity yields LAYERED children. The
		// child decoded from the parent's prov form must itself carry provenance.
		const parent = (await c.loadEntityData(provRoot)) as {
			title: string;
			child: EntityRefStub;
		};
		expect(parent.title).toBe("prov-root");
		const childMeta = await c.getEntityMetadata(parent.child);
		expect(childMeta.provenance_parent).toBeTruthy();
		c.close();
	});

	it("rehydrates a session across a reload (shared storage)", async () => {
		const storage = memoryStorage();
		const c1 = makeConnector(storage);
		await c1.refresh();
		const r1 = refs(c1);
		const s1 = (await c1.createSession()) as HttpSession;
		let load!: OpResultStub;
		let echoOp!: OpResultStub;
		s1.store.set(s1.userState, (_p, ctx) => {
			load = ctx.allocateLoad(r1.root);
			echoOp = ctx.allocateInvoke(r1.echo.ref, "echo", [99]);
			return { load, echoOp };
		});
		await waitFor(
			() => status(s1, load).status === "filled" && status(s1, echoOp).status === "filled",
			"s1 filled",
		);
		c1.close(); // persists sessions + shortId table

		const c2 = makeConnector(storage);
		await c2.refresh();
		const s2 = c2.attachSession(s1.sessionId) as HttpSession;
		const user = s2.store.get(s2.userState) as { load: OpResultStub; echoOp: OpResultStub };
		// Invoke fill restored verbatim; load refetches over the wire.
		expect(status(s2, user.echoOp)).toEqual({ status: "filled", value: 99 });
		await waitFor(() => status(s2, user.load).status === "filled", "load refetched");
		expect((status(s2, user.load).value as { title: string }).title).toBe("root");
		c2.close();
	});
});
