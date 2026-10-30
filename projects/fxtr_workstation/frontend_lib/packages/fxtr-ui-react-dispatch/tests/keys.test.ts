import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { describe, expect, it } from "vitest";
import type { AnyKeyFallbacks, EntityKeyFallbacks, ServiceKeyFallbacks } from "../src/fallbacks";
import {
	declareDispatchKey,
	declareEntityDispatchKey,
	declareServiceDispatchKey,
} from "../src/keys";
import type { DispatchedComponent, ErrorFallbackComponent } from "../src/registry_types";

// Trivial sentinel fallback components reused across cases. The
// declare* signatures require a fallback bundle now; the tests below
// don't exercise behaviour, just the runtime shape of the returned key.
const entityFallbacks: EntityKeyFallbacks<object> = {
	Loading: (() => null) as unknown as DispatchedComponent<EntityRefStub, object>,
	NoMatch: (() => null) as unknown as DispatchedComponent<EntityRefStub, object>,
	Error: (() => null) as unknown as ErrorFallbackComponent<EntityRefStub, object>,
};
const serviceFallbacks: ServiceKeyFallbacks<object> = {
	Loading: (() => null) as unknown as DispatchedComponent<ServiceRefStub, object>,
	NoMatch: (() => null) as unknown as DispatchedComponent<ServiceRefStub, object>,
	Error: (() => null) as unknown as ErrorFallbackComponent<ServiceRefStub, object>,
};
const anyFallbacks: AnyKeyFallbacks<object> = {
	Loading: (() => null) as unknown as DispatchedComponent<EntityRefStub | ServiceRefStub, object>,
	NoMatch: (() => null) as unknown as DispatchedComponent<EntityRefStub | ServiceRefStub, object>,
	Error: (() => null) as unknown as ErrorFallbackComponent<EntityRefStub | ServiceRefStub, object>,
};

describe("declareDispatchKey", () => {
	it("returns an object carrying the supplied UUID, description, and fallbacks", () => {
		const key = declareDispatchKey({
			uuid: "abc-123",
			description: "MyKey",
			fallbacks: anyFallbacks,
		});
		expect(key.uuid).toBe("abc-123");
		expect(key.description).toBe("MyKey");
		expect(key.fallbacks).toBe(anyFallbacks);
	});

	it("freezes the returned object so callers can't desync the UUID", () => {
		const key = declareDispatchKey({
			uuid: "abc-123",
			description: "MyKey",
			fallbacks: anyFallbacks,
		});
		expect(Object.isFrozen(key)).toBe(true);
	});

	it("returns distinct objects per call even when args repeat", () => {
		// UUID uniqueness is the consumer's responsibility, but two
		// calls with the same args still produce *different runtime
		// objects* — there's no interning.
		const a = declareDispatchKey({ uuid: "x", description: "K", fallbacks: anyFallbacks });
		const b = declareDispatchKey({ uuid: "x", description: "K", fallbacks: anyFallbacks });
		expect(a).not.toBe(b);
	});
});

describe("declareEntityDispatchKey / declareServiceDispatchKey", () => {
	it("each returns the same runtime shape as declareDispatchKey", () => {
		const e = declareEntityDispatchKey({
			uuid: "e-uuid",
			description: "EntityOnly",
			fallbacks: entityFallbacks,
		});
		const s = declareServiceDispatchKey({
			uuid: "s-uuid",
			description: "ServiceOnly",
			fallbacks: serviceFallbacks,
		});
		expect(e.uuid).toBe("e-uuid");
		expect(e.description).toBe("EntityOnly");
		expect(e.fallbacks).toBe(entityFallbacks);
		expect(s.uuid).toBe("s-uuid");
		expect(s.description).toBe("ServiceOnly");
		expect(s.fallbacks).toBe(serviceFallbacks);
	});
});
