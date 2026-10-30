import { type EntityRefStub, memoryStorage } from "@fxtr-ui/core";
import { type FxtrCbor, fxtrCborDumps } from "@fxtr/core";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HttpConnector } from "../src/connector";
import type { FetchFn } from "../src/entity_data";

const BASE = "http://host:8000";

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

const DISCOVERY = {
	entities: { "demo/root": "did-root" },
	services: {
		echo: { kind: "http", url: "/svc/echo", method: "GET", service_type_id: "!echo-type" },
	},
};

/** A fetch serving /discovery + /data/{id}. */
function makeFetch(): FetchFn {
	return async (url) => {
		if (url === `${BASE}/discovery`) return jsonResponse(DISCOVERY);
		if (url === `${BASE}/data/did-root`) {
			return cborResponse({ bytes: fxtrCborDumps({ label: "hi" }) });
		}
		throw new Error(`unexpected fetch ${url}`);
	};
}

let storage: ReturnType<typeof memoryStorage>;
beforeEach(() => {
	storage = memoryStorage();
});

describe("HttpConnector: discovery + identity", () => {
	it("fetches discovery, builds records, and classifies shortIds", async () => {
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c.refresh();
		const entities = c.store.get(c.listEntities().records);
		const services = c.store.get(c.listServices().records);
		expect(entities.map((e) => e.name)).toEqual(["demo/root"]);
		expect(entities[0]?.idString).toBe("did-root");
		expect(services[0]?.name).toBe("echo");
		expect(services[0]?.typeIdString).toBe("!echo-type");

		const eRef = entities[0]?.ref as EntityRefStub;
		const sRef = services[0]?.ref;
		expect(c.classifyShortId(eRef.shortId)).toBe("entity");
		expect(sRef && c.classifyShortId(sRef.shortId)).toBe("service");
		expect(c.classifyShortId("nope")).toBeNull();
		// entityRef interning is idempotent.
		expect(c.entityRef(eRef.shortId)).toBe(eRef);
	});

	it("loads + decodes entity data and resolves service metadata", async () => {
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c.refresh();
		const eRef = c.store.get(c.listEntities().records)[0]?.ref as EntityRefStub;
		const sRef = c.store.get(c.listServices().records)[0]?.ref;

		expect(await c.loadEntityData(eRef)).toEqual({ label: "hi" });
		if (sRef) expect(c.getServiceMetadata(sRef)).toEqual({ type_id_string: "!echo-type" });
	});

	it("persists the shortId table and rehydrates it", async () => {
		const c1 = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c1.refresh();
		const eShort = c1.store.get(c1.listEntities().records)[0]?.ref.shortId;
		await Promise.resolve(); // let the debounced persist flush
		await new Promise((r) => setTimeout(r, 0));

		// A fresh connector over the same storage classifies the persisted shortId.
		const c2 = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		expect(eShort && c2.classifyShortId(eShort)).toBe("entity");
	});

	it("close() flips connectionStatus and closes the lists", async () => {
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c.refresh();
		expect(c.store.get(c.connectionStatus)).toBe("open");
		c.close();
		expect(c.store.get(c.connectionStatus)).toBe("closed");
		expect(c.store.get(c.listEntities().status)).toBe("closed");
	});

	it("connectionStatus tracks discovery reachability: connecting → open; failure → reconnecting → recover", async () => {
		const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
		let fail = true;
		const fetchFn: FetchFn = async (url) => {
			if (url === `${BASE}/discovery`) {
				if (fail) throw new Error("network down");
				return jsonResponse(DISCOVERY);
			}
			throw new Error(`unexpected fetch ${url}`);
		};
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn });
		// Before the first discovery resolves (the ctor's fire-and-forget
		// refresh hasn't awaited yet), status is `connecting`.
		expect(c.store.get(c.connectionStatus)).toBe("connecting");

		// A failed /discovery → reconnecting + both lists `error`.
		await c.refresh();
		expect(c.store.get(c.connectionStatus)).toBe("reconnecting");
		expect(c.store.get(c.listEntities().status)).toBe("error");
		expect(c.store.get(c.listServices().status)).toBe("error");

		// A later successful refresh recovers to open + ready.
		fail = false;
		await c.refresh();
		expect(c.store.get(c.connectionStatus)).toBe("open");
		expect(c.store.get(c.listEntities().status)).toBe("ready");
		expect(c.store.get(c.listEntities().records).map((e) => e.name)).toEqual(["demo/root"]);
		errSpy.mockRestore();
		c.close();
	});
});
