/**
 * Unit tests for `WorkstationConnector.loadEntityData` — the stateless
 * out-of-band entity-data load. Drives the underlying
 * `WorkstationServerConnection` via `MockWebSocket`; live-server
 * coverage lives in `integration.test.ts`.
 */

import { createStore } from "jotai";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
	ConnectionClosedError,
	ConnectorClosedError,
	LoadEntityError,
	type LoadEntityErrorResponse,
	type LoadEntityResponse,
	WireEscapeError,
	WorkstationConnector,
	WorkstationEntityRefStub,
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

function lastSent(): { request_id: number; op: string; [k: string]: unknown } {
	const sent = currentMockSocket().sentParsed;
	const last = sent[sent.length - 1];
	if (!last) throw new Error("no message has been sent");
	return last as { request_id: number; op: string; [k: string]: unknown };
}

let errorSpy: ReturnType<typeof vi.spyOn>;
let warnSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
	resetMockWebSockets();
	warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
	errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
	warnSpy.mockRestore();
	errorSpy.mockRestore();
});

describe("WorkstationConnector.loadEntityData", () => {
	it("ships a load_entity request carrying the stub's short_id", () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();

		const stub = connector.entityRef("e-a");
		void connector.loadEntityData(stub);

		expect(lastSent()).toMatchObject({ op: "load_entity", short_id: "e-a" });
	});

	it("resolves with the decoded payload on load_entity_response", async () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();

		const stub = connector.entityRef("e-a");
		const promise = connector.loadEntityData(stub);
		const { request_id } = lastSent();

		const response: LoadEntityResponse = {
			type: "load_entity_response",
			request_id,
			payload: { title: "hello", count: 7 },
		};
		currentMockSocket().simulateMessage(JSON.stringify(response));

		await expect(promise).resolves.toEqual({ title: "hello", count: 7 });
	});

	it("interns entity ref stubs in the decoded payload via the connector's intern map", async () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();

		const stub = connector.entityRef("parent");
		const promise = connector.loadEntityData(stub);
		const { request_id } = lastSent();

		const response: LoadEntityResponse = {
			type: "load_entity_response",
			request_id,
			payload: { child: ["entity", "child-1"], sibling: ["entity", "child-2"] },
		};
		currentMockSocket().simulateMessage(JSON.stringify(response));

		const result = (await promise) as { child: unknown; sibling: unknown };
		expect(result.child).toBeInstanceOf(WorkstationEntityRefStub);
		// Reference-equal to a separately-minted `entityRef` for the same shortId.
		expect(result.child).toBe(connector.entityRef("child-1"));
		expect(result.sibling).toBe(connector.entityRef("child-2"));
		// And, because of interning, a fresh call also collapses to the same instance.
		expect(connector.entityRef("child-1")).toBe(result.child);
	});

	it("rejects with LoadEntityError on load_entity_error, carrying shortId + serverMessage", async () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();

		const stub = connector.entityRef("e-gone");
		const promise = connector.loadEntityData(stub);
		const { request_id } = lastSent();

		const errResponse: LoadEntityErrorResponse = {
			type: "load_entity_error",
			request_id,
			message: "unknown or inactive entity short id",
		};
		currentMockSocket().simulateMessage(JSON.stringify(errResponse));

		await expect(promise).rejects.toBeInstanceOf(LoadEntityError);
		await expect(promise).rejects.toMatchObject({
			shortId: "e-gone",
			serverMessage: "unknown or inactive entity short id",
		});
	});

	it("rejects with ConnectorClosedError when called on a closed connector", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();
		await connector.close();

		const stub = connector.entityRef("e-a");
		// `loadEntityData` is async, so the "closed" gate surfaces as a
		// rejected promise rather than a synchronous throw.
		await expect(connector.loadEntityData(stub)).rejects.toBeInstanceOf(ConnectorClosedError);

		await conn.close();
	});

	it("rejects pending loadEntityData with ConnectionClosedError when the connection closes mid-flight", async () => {
		const { conn, connector } = makeConnector();
		currentMockSocket().simulateOpen();

		const stub = connector.entityRef("e-a");
		const promise = connector.loadEntityData(stub);
		expect(lastSent().op).toBe("load_entity");

		conn.close();

		await expect(promise).rejects.toBeInstanceOf(ConnectionClosedError);
	});

	it("rejects with WireEscapeError when the payload is malformed", async () => {
		const { connector } = makeConnector();
		currentMockSocket().simulateOpen();

		const stub = connector.entityRef("e-a");
		const promise = connector.loadEntityData(stub);
		const { request_id } = lastSent();

		// `["entity"]` has the reserved tag but wrong arity — `unescapeWire`
		// rejects it as malformed.
		const response: LoadEntityResponse = {
			type: "load_entity_response",
			request_id,
			payload: { broken: ["entity"] },
		};
		currentMockSocket().simulateMessage(JSON.stringify(response));

		await expect(promise).rejects.toBeInstanceOf(WireEscapeError);
	});
});
