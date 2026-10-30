import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { describe, expect, it } from "vitest";
import type { AnyKeyFallbacks, EntityKeyFallbacks, ServiceKeyFallbacks } from "../src/fallbacks";
import {
	declareDispatchKey,
	declareEntityDispatchKey,
	declareServiceDispatchKey,
} from "../src/keys";
import type { DispatchKeyRegistryEntry } from "../src/registry_types";
import {
	DuplicateDispatchKeyError,
	createDispatchRegistry,
	entityEntry,
	serviceEntry,
} from "../src/static_registry";

// Sentinel fallback bundles for the key declarations below.
const entityFallbacks: EntityKeyFallbacks<object> = {
	Loading: () => null,
	NoMatch: () => null,
	Error: () => null,
};
const serviceFallbacks: ServiceKeyFallbacks<object> = {
	Loading: () => null,
	NoMatch: () => null,
	Error: () => null,
};
const anyFallbacks: AnyKeyFallbacks<object> = {
	Loading: () => null,
	NoMatch: () => null,
	Error: () => null,
};

// Helper: build a minimally-valid entry. The state-machine
// behavior isn't under test here — just the registry routing
// + duplicate detection. After the dispatch-key fallback refactor,
// the registry entry shape no longer carries `Fallback` /
// `ErrorFallback` — those live on the key.
function dummyEntry<Stub, Props extends object>(): DispatchKeyRegistryEntry<Stub, Props, unknown> {
	return {
		initialState: undefined,
		effectFn: () => () => {},
	};
}

describe("entityEntry / serviceEntry", () => {
	it("entityEntry stores the key reference (not a derived copy)", () => {
		const key = declareEntityDispatchKey({
			uuid: "k1",
			description: "K1",
			fallbacks: entityFallbacks,
		});
		const reg = entityEntry(key, dummyEntry<EntityRefStub, object>());
		expect(reg.key).toBe(key);
		expect(reg.key.uuid).toBe("k1");
	});

	it("serviceEntry stores the key reference (not a derived copy)", () => {
		const key = declareServiceDispatchKey({
			uuid: "k2",
			description: "K2",
			fallbacks: serviceFallbacks,
		});
		const reg = serviceEntry(key, dummyEntry<ServiceRefStub, object>());
		expect(reg.key).toBe(key);
	});
});

describe("createDispatchRegistry", () => {
	it("looks up entity entries by key UUID", () => {
		const key = declareEntityDispatchKey({
			uuid: "k",
			description: "K",
			fallbacks: entityFallbacks,
		});
		const entry = dummyEntry<EntityRefStub, object>();
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(key, entry)],
			serviceEntries: [],
		});
		expect(registry.forEntity(key)).toBe(entry);
	});

	it("looks up service entries by key UUID", () => {
		const key = declareServiceDispatchKey({
			uuid: "k",
			description: "K",
			fallbacks: serviceFallbacks,
		});
		const entry = dummyEntry<ServiceRefStub, object>();
		const registry = createDispatchRegistry({
			entityEntries: [],
			serviceEntries: [serviceEntry(key, entry)],
		});
		expect(registry.forService(key)).toBe(entry);
	});

	it("returns undefined on a miss", () => {
		const knownKey = declareEntityDispatchKey({
			uuid: "known",
			description: "Known",
			fallbacks: entityFallbacks,
		});
		const unknownKey = declareEntityDispatchKey({
			uuid: "unknown",
			description: "Unknown",
			fallbacks: entityFallbacks,
		});
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(knownKey, dummyEntry<EntityRefStub, object>())],
			serviceEntries: [],
		});
		expect(registry.forEntity(unknownKey)).toBeUndefined();
	});

	it("entity and service tables are independent — same key can appear in both", () => {
		const key = declareDispatchKey({
			uuid: "dual",
			description: "Dual",
			fallbacks: anyFallbacks,
		});
		const eEntry = dummyEntry<EntityRefStub, object>();
		const sEntry = dummyEntry<ServiceRefStub, object>();
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(key, eEntry)],
			serviceEntries: [serviceEntry(key, sEntry)],
		});
		expect(registry.forEntity(key)).toBe(eEntry);
		expect(registry.forService(key)).toBe(sEntry);
	});

	it("throws DuplicateDispatchKeyError when two pairs share a UUID in the entity table", () => {
		const key = declareEntityDispatchKey({
			uuid: "k",
			description: "K",
			fallbacks: entityFallbacks,
		});
		expect(() =>
			createDispatchRegistry({
				entityEntries: [
					entityEntry(key, dummyEntry<EntityRefStub, object>()),
					entityEntry(key, dummyEntry<EntityRefStub, object>()),
				],
				serviceEntries: [],
			}),
		).toThrow(DuplicateDispatchKeyError);
	});

	it("throws DuplicateDispatchKeyError when two pairs share a UUID in the service table", () => {
		const key = declareServiceDispatchKey({
			uuid: "k",
			description: "K",
			fallbacks: serviceFallbacks,
		});
		expect(() =>
			createDispatchRegistry({
				entityEntries: [],
				serviceEntries: [
					serviceEntry(key, dummyEntry<ServiceRefStub, object>()),
					serviceEntry(key, dummyEntry<ServiceRefStub, object>()),
				],
			}),
		).toThrow(DuplicateDispatchKeyError);
	});

	it("DuplicateDispatchKeyError carries the conflicting key + table", () => {
		const key = declareEntityDispatchKey({
			uuid: "k",
			description: "Conflicted",
			fallbacks: entityFallbacks,
		});
		try {
			createDispatchRegistry({
				entityEntries: [
					entityEntry(key, dummyEntry<EntityRefStub, object>()),
					entityEntry(key, dummyEntry<EntityRefStub, object>()),
				],
				serviceEntries: [],
			});
			expect.fail("expected DuplicateDispatchKeyError");
		} catch (err) {
			expect(err).toBeInstanceOf(DuplicateDispatchKeyError);
			const e = err as DuplicateDispatchKeyError;
			expect(e.table).toBe("entity");
			expect(e.key).toBe(key);
			expect(e.message).toContain("Conflicted");
			expect(e.message).toContain("k");
		}
	});
});
