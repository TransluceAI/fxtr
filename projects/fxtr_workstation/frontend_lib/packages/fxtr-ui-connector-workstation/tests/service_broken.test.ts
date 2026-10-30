/**
 * Service-break handling on the frontend connector (6.4).
 *
 * Brokenness is a connector-level fact: the connector coalesces breaks
 * into a single `brokenServices` set that every interned
 * `WorkstationServiceRefStub`'s `state` atom reads. Two feeds populate
 * it — `ServiceBrokenNotification` frames on a session subscription
 * (primary) and the `services` resource list's `broken` field
 * (secondary, for list-only connectors).
 */

import { createStore } from "jotai";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkstationServerConnection } from "../src/connection";
import { WorkstationConnector } from "../src/connector";
import type { ResourceSnapshotEvent, ServiceBrokenNotification } from "../src/protocol";
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

beforeEach(() => {
	resetMockWebSockets();
	vi.spyOn(console, "warn").mockImplementation(() => {});
	vi.spyOn(console, "error").mockImplementation(() => {});
});

describe("connector broken-services set", () => {
	it("markServiceBroken flips an interned stub's state alive -> broken", () => {
		const { connector, store } = makeConnector();
		const stub = connector.serviceRef("s3");

		expect(store.get(stub.state)).toBe("alive");

		connector.markServiceBroken("s3");
		expect(store.get(stub.state)).toBe("broken");

		// A stub minted after the break also reads broken (same set).
		expect(store.get(connector.serviceRef("s3").state)).toBe("broken");
		// Unrelated services stay alive.
		expect(store.get(connector.serviceRef("s4").state)).toBe("alive");
	});

	it("markServiceBroken is idempotent (no spurious set churn)", () => {
		const { connector, store } = makeConnector();
		connector.markServiceBroken("s3");
		const first = store.get(connector.brokenServices);
		connector.markServiceBroken("s3");
		// Same set instance — a repeat is a no-op write.
		expect(store.get(connector.brokenServices)).toBe(first);
		expect(first.has("s3")).toBe(true);
	});

	it("one fact across sessions: the shared interned stub reflects breakage", () => {
		const { connector, store } = makeConnector();
		const stub = connector.serviceRef("s9");
		expect(store.get(stub.state)).toBe("alive");
		// Two different sessions reporting the same break coalesce.
		connector.markServiceBroken("s9");
		connector.markServiceBroken("s9");
		expect(store.get(stub.state)).toBe("broken");
	});
});

describe("session ServiceBrokenNotification feed", () => {
	async function makeConnectedSession() {
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
		// Drain the subscribe ack + snapshot.
		const subscribe = lastSent();
		const subscription_id = subscribe.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		currentMockSocket().simulateMessage(
			JSON.stringify({
				subscription_id,
				type: "session_snapshot",
				session_id: "sess",
				last_state: {
					kind: "state_update",
					seq: 0,
					ack_seq: 0,
					state: {},
					closed: false,
					tracked_entities: [],
					tracked_services: [],
					slot_counter: 0,
				},
				preserved_events: [],
				subsequent_events: [],
			}),
		);
		return { connector, store, session, subscription_id };
	}

	it("a service_broken frame marks the connector set and flips the stub", async () => {
		const { connector, store, subscription_id } = await makeConnectedSession();
		const stub = connector.serviceRef("s7");
		expect(store.get(stub.state)).toBe("alive");

		const frame: ServiceBrokenNotification = {
			subscription_id,
			type: "service_broken",
			service_short_id: "s7",
		};
		currentMockSocket().simulateMessage(JSON.stringify(frame));

		expect(store.get(stub.state)).toBe("broken");
	});
});

describe("services resource-list feed", () => {
	it("a broken record in the services list marks the connector set", () => {
		const { connector, store } = makeConnector();
		const list = connector.listServices();
		currentMockSocket().simulateOpen();

		const subscribe = lastSent();
		expect(subscribe.op).toBe("subscribe");
		const subscription_id = subscribe.request_id;
		currentMockSocket().simulateMessage(
			JSON.stringify({ type: "ack", request_id: subscription_id }),
		);
		const snapshot: ResourceSnapshotEvent = {
			subscription_id,
			type: "resource_snapshot",
			data: [
				{ short_id: "s1", service_type_id: "t", external_names: [], broken: false },
				{ short_id: "s2", service_type_id: "t", external_names: [], broken: true },
			],
		};
		currentMockSocket().simulateMessage(JSON.stringify(snapshot));

		// The broken record fed the connector set; the alive one didn't.
		expect(store.get(list.records)).toHaveLength(2);
		expect(store.get(connector.serviceRef("s2").state)).toBe("broken");
		expect(store.get(connector.serviceRef("s1").state)).toBe("alive");
	});
});
