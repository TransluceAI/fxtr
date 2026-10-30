/**
 * End-to-end integration: dispatch system driven against the
 * `@fxtr-ui/dispatch-demo` Python workstation. Boots the
 * workstation once in `beforeAll`, then for each registered
 * entity / service, builds a `StaticDispatcher` around a real
 * session, asks it for the entry via `dispatcher.getEntry`,
 * subscribes to drive the state machine, and waits for the
 * `componentAtom` to transition off the Loading fallback.
 *
 * Skips JSX rendering — keeps the integration test focused on
 * the wire + state-machine plumbing without a React event
 * loop. The static-helper React tests in
 * `v2/ts/packages/fxtr-ui-react-dispatch-tests/` cover the
 * render path separately.
 *
 * Each test builds its own per-test dispatcher with the
 * specific lookup map it needs (separate registries). All
 * dispatchers wrap the same shared session; cleanup happens
 * once in `afterAll` via `session.detach()` + `connector.close()`,
 * so per-test dispatchers are not individually detached. This
 * sidesteps the contract that "dispatcher owns its session" —
 * which is the right idiom for application code but awkward for
 * tests that need many short-lived dispatchers over one
 * long-lived session.
 *
 * Requires `uv` and the workstation Python package on the
 * machine; spawns one Python subprocess per test run.
 */

import {
	type EntityRecord,
	type ServiceRecord,
	WorkstationConnector,
	type WorkstationSession,
} from "@fxtr-ui/connector-workstation";
import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { AnyDispatchEntry, Dispatcher } from "../src/dispatcher";
import type { AnyKeyFallbacks } from "../src/fallbacks";
import { declareDispatchKey } from "../src/keys";
import type { DispatchKeyRegistryEntry, DispatchedComponent } from "../src/registry_types";
import { createStaticDispatcher } from "../src/static_dispatcher";
import { staticEntityTypeDispatch } from "../src/static_entity_registry";
import { createDispatchRegistry, entityEntry, serviceEntry } from "../src/static_registry";
import { staticServiceTypeDispatch } from "../src/static_service_registry";
import {
	type DispatchDemoTypeIds,
	type DispatchDemoWorkstation,
	startDispatchDemoWorkstation,
} from "./_integration_harness";

// Sentinel component identities — we compare by reference.
const NoteCard = function NoteCard() {
	return null;
} as unknown as DispatchedComponent<EntityRefStub, object>;
const PhotoCard = function PhotoCard() {
	return null;
} as unknown as DispatchedComponent<EntityRefStub, object>;
const EchoCard = function EchoCard() {
	return null;
} as unknown as DispatchedComponent<ServiceRefStub, object>;
const CounterCard = function CounterCard() {
	return null;
} as unknown as DispatchedComponent<ServiceRefStub, object>;
const EntityFallback = function EntityFallback() {
	return null;
} as unknown as DispatchedComponent<EntityRefStub, object>;
const ServiceFallback = function ServiceFallback() {
	return null;
} as unknown as DispatchedComponent<ServiceRefStub, object>;
const SharedFallback = function SharedFallback() {
	return null;
} as unknown as DispatchedComponent<EntityRefStub | ServiceRefStub, object>;
const SharedErrorFallback = function SharedErrorFallback() {
	return null;
} as unknown as AnyKeyFallbacks<object>["Error"];

// The shared CardKey accepts both entity and service stubs.
// Its fallback bundle covers both — `SharedFallback` /
// `SharedErrorFallback` are used by the dispatcher's
// instance-default render whenever an entry can't reach a
// terminal renderer. The per-entity / per-service helpers
// still write the right concrete card on a lookup hit.
const cardKeyFallbacks: AnyKeyFallbacks<object> = {
	Loading: SharedFallback,
	NoMatch: SharedFallback,
	Error: SharedErrorFallback,
};
const CardKey = declareDispatchKey({
	uuid: "integration-card-key",
	description: "Card",
	fallbacks: cardKeyFallbacks,
});
// A separate key for the no-match fallback test, so the
// dispatcher's entry cache and userState slot don't collide
// with the lookup-populated tests above.
const EmptyCardKey = declareDispatchKey({
	uuid: "integration-empty-card-key",
	description: "EmptyCard",
	fallbacks: cardKeyFallbacks,
});

const POLL_INTERVAL_MS = 25;
const RESOLVE_TIMEOUT_MS = 5_000;

async function waitForComponent(
	entry: AnyDispatchEntry,
	session: WorkstationSession,
): Promise<unknown> {
	// Subscribe to the entry's componentAtom; the subscription is
	// what mounts the effect (drives the state machine). Poll
	// until the component transitions away from the shared
	// fallback (the initial Loading value the dispatcher derives
	// from `key.fallbacks.Loading`).
	const unsub = session.store.sub(entry.componentAtom, () => {});
	try {
		const deadline = Date.now() + RESOLVE_TIMEOUT_MS;
		while (Date.now() < deadline) {
			const current = session.store.get(entry.componentAtom);
			if (current !== SharedFallback && current !== EntityFallback && current !== ServiceFallback) {
				return current;
			}
			await new Promise<void>((r) => setTimeout(r, POLL_INTERVAL_MS));
		}
		throw new Error(
			`dispatch entry ${entry.id} did not transition off Fallback within ${RESOLVE_TIMEOUT_MS}ms`,
		);
	} finally {
		unsub();
	}
}

describe("integration: dispatch end-to-end against the demo workstation", () => {
	let ws: DispatchDemoWorkstation;
	let connector: WorkstationConnector;
	let session: WorkstationSession;
	let typeIds: DispatchDemoTypeIds;

	beforeAll(async () => {
		ws = await startDispatchDemoWorkstation();
		typeIds = ws.typeIds;
		connector = new WorkstationConnector({ url: ws.clientUrl });
		session = await connector.createSession({});
	}, 30_000);

	afterAll(async () => {
		await session?.detach();
		await connector?.close();
		await ws?.close();
	});

	async function findEntityByName(name: string): Promise<EntityRecord> {
		const list = connector.listEntities();
		const deadline = Date.now() + 5_000;
		while (Date.now() < deadline) {
			const records = session.store.get(list.records) as readonly EntityRecord[];
			const found = records.find((r) => r.external_names.includes(name));
			if (found) return found;
			await new Promise<void>((r) => setTimeout(r, POLL_INTERVAL_MS));
		}
		throw new Error(`entity with external_name ${name} not found in connector.listEntities()`);
	}

	async function findServiceByName(name: string): Promise<ServiceRecord> {
		const list = connector.listServices();
		const deadline = Date.now() + 5_000;
		while (Date.now() < deadline) {
			const records = session.store.get(list.records) as readonly ServiceRecord[];
			const found = records.find((r) => r.external_names.includes(name));
			if (found) return found;
			await new Promise<void>((r) => setTimeout(r, POLL_INTERVAL_MS));
		}
		throw new Error(`service with external_name ${name} not found in connector.listServices()`);
	}

	function makeEntityEntry(
		lookupMap: ReadonlyMap<string, DispatchedComponent<EntityRefStub, object>>,
	): DispatchKeyRegistryEntry<EntityRefStub, object, unknown> {
		return staticEntityTypeDispatch<object>({
			lookupMap,
		}) as unknown as DispatchKeyRegistryEntry<EntityRefStub, object, unknown>;
	}
	function makeServiceEntry(
		lookupMap: ReadonlyMap<string, DispatchedComponent<ServiceRefStub, object>>,
	): DispatchKeyRegistryEntry<ServiceRefStub, object, unknown> {
		return staticServiceTypeDispatch<object>({
			lookupMap,
		}) as unknown as DispatchKeyRegistryEntry<ServiceRefStub, object, unknown>;
	}

	// Shared full-coverage registry: maps all four demo type
	// ids to their sentinel components. Each test builds its
	// own dispatcher around the shared session and asks for the
	// entry it cares about.
	function makeFullRegistry() {
		return createDispatchRegistry({
			entityEntries: [
				entityEntry(
					CardKey,
					makeEntityEntry(
						new Map([
							[typeIds.note, NoteCard],
							[typeIds.photo, PhotoCard],
						]),
					),
				),
			],
			serviceEntries: [
				serviceEntry(
					CardKey,
					makeServiceEntry(
						new Map([
							[typeIds.echo, EchoCard],
							[typeIds.counter, CounterCard],
						]),
					),
				),
			],
		});
	}

	it("resolves a Note entity to NoteCard via the static entity dispatcher", async () => {
		const record = await findEntityByName("demo-note-1");
		const stub = connector.entityRef(record.short_id);

		const dispatcher: Dispatcher = createStaticDispatcher({
			session,
			registry: makeFullRegistry(),
		});
		const entry = dispatcher.getEntry(CardKey, stub);

		const component = await waitForComponent(entry, session);
		expect(component).toBe(NoteCard);
	});

	it("resolves a Photo entity to PhotoCard via the static entity dispatcher", async () => {
		const record = await findEntityByName("demo-photo-1");
		const stub = connector.entityRef(record.short_id);

		const dispatcher: Dispatcher = createStaticDispatcher({
			session,
			registry: makeFullRegistry(),
		});
		const entry = dispatcher.getEntry(CardKey, stub);

		const component = await waitForComponent(entry, session);
		expect(component).toBe(PhotoCard);
	});

	it("resolves an Echo service to EchoCard via the static service dispatcher", async () => {
		const record = await findServiceByName("demo-echo");
		const stub = connector.serviceRef(record.short_id);

		const dispatcher: Dispatcher = createStaticDispatcher({
			session,
			registry: makeFullRegistry(),
		});
		const entry = dispatcher.getEntry(CardKey, stub);

		const component = await waitForComponent(entry, session);
		expect(component).toBe(EchoCard);
	});

	it("resolves a Counter service to CounterCard via the static service dispatcher", async () => {
		const record = await findServiceByName("demo-counter");
		const stub = connector.serviceRef(record.short_id);

		const dispatcher: Dispatcher = createStaticDispatcher({
			session,
			registry: makeFullRegistry(),
		});
		const entry = dispatcher.getEntry(CardKey, stub);

		const component = await waitForComponent(entry, session);
		expect(component).toBe(CounterCard);
	});

	it("falls back to the key's NoMatch fallback when the resolved entity type isn't in the lookup map", async () => {
		const record = await findEntityByName("demo-note-1");
		const stub = connector.entityRef(record.short_id);

		// Separate `EmptyCardKey` keeps this entry on its own
		// userState slot — independent of the
		// lookup-populated `CardKey` slots written by tests
		// above.
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(EmptyCardKey, makeEntityEntry(new Map()))],
			serviceEntries: [],
		});
		const dispatcher: Dispatcher = createStaticDispatcher({ session, registry });
		const entry = dispatcher.getEntry(EmptyCardKey, stub);

		// The state machine reaches `resolved` and then re-syncs
		// the terminal phase to the key's NoMatch fallback (which
		// is `SharedFallback` for `EmptyCardKey`). `waitForComponent`
		// would time out waiting for a non-Fallback value, so drive
		// the subscription + sleep and read the entry's
		// `dispatchInternalStateAtom` directly.
		const unsub = session.store.sub(entry.componentAtom, () => {});
		try {
			const deadline = Date.now() + RESOLVE_TIMEOUT_MS;
			let state: unknown = session.store.get(entry.dispatchInternalStateAtom);
			while (Date.now() < deadline) {
				state = session.store.get(entry.dispatchInternalStateAtom);
				if ((state as { phase?: string }).phase === "resolved") break;
				await new Promise<void>((r) => setTimeout(r, POLL_INTERVAL_MS));
			}
			expect((state as { phase: string }).phase).toBe("resolved");
			expect(session.store.get(entry.componentAtom)).toBe(SharedFallback);
		} finally {
			unsub();
		}
	});
});
