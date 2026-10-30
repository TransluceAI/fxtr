/**
 * Tests for `makeEntryWrapper` + `getStaleAwareInner`.
 *
 * Non-React: covers the WeakMap memoisation contract + the inner-
 * component round-trip via the symbol-stamped property. Render
 * behaviour (the `display: contents` ↔ `display: block; opacity: 0.5`
 * toggle when the staleAtom flips) lives in the sibling
 * jsdom-enabled test under
 * `v2/ts/packages/fxtr-ui-react-dispatch-tests/`.
 */

import type { EntityRefStub } from "@fxtr-ui/core";
import { atom } from "jotai";
import { describe, expect, it } from "vitest";
import type { DispatchedComponent } from "../src/registry_types";
import { getStaleAwareInner, makeEntryWrapper } from "../src/staleness_wrapper";

// Two sentinel inner components — bare functions never actually
// rendered here; we only inspect their identities through the wrap.
const InnerA = function InnerA() {
	return null;
} as unknown as DispatchedComponent<EntityRefStub, object>;
const InnerB = function InnerB() {
	return null;
} as unknown as DispatchedComponent<EntityRefStub, object>;

describe("makeEntryWrapper", () => {
	it("returns identity-stable wrappers per inner component", () => {
		const wrap = makeEntryWrapper(atom(false));
		const wrappedA1 = wrap(InnerA);
		const wrappedA2 = wrap(InnerA);
		expect(wrappedA1).toBe(wrappedA2);
	});

	it("returns distinct wrappers for distinct inner components", () => {
		const wrap = makeEntryWrapper(atom(false));
		const wrappedA = wrap(InnerA);
		const wrappedB = wrap(InnerB);
		expect(wrappedA).not.toBe(wrappedB);
	});

	it("wrappers from different entries do not collide", () => {
		// Each entry mints its own wrap helper closing over its own
		// staleAtom — so even for the same inner component, two
		// entries see different wrap identities. This is fine: each
		// entry's componentAtom only ever sees its own wrap.
		const wrap1 = makeEntryWrapper(atom(false));
		const wrap2 = makeEntryWrapper(atom(false));
		expect(wrap1(InnerA)).not.toBe(wrap2(InnerA));
	});

	it("sets a displayName documenting the inner", () => {
		const wrap = makeEntryWrapper(atom(false));
		const wrapped = wrap(InnerA);
		// Don't pin the exact inner name — vitest's transform may
		// rename top-level function expressions during compile (so
		// `function InnerA() {}` ends up `.name === "InnerA2"` etc).
		// What matters is that the displayName follows the
		// `StaleAware(<something>)` pattern, surfacing the wrap in
		// React devtools.
		expect((wrapped as { displayName?: string }).displayName).toMatch(/^StaleAware\(.+\)$/);
	});
});

describe("getStaleAwareInner", () => {
	it("recovers the inner component identity from a wrap", () => {
		const wrap = makeEntryWrapper(atom(false));
		const wrapped = wrap(InnerA);
		expect(getStaleAwareInner(wrapped)).toBe(InnerA);
	});

	it("returns undefined for an un-wrapped component", () => {
		// Bare components don't carry the stale-aware symbol so the
		// helper returns `undefined` — tests can assert this to catch
		// regressions where a state-machine path forgets to wrap.
		expect(getStaleAwareInner(InnerA)).toBeUndefined();
	});

	it("composes through the WeakMap (repeat wrap recovers same inner)", () => {
		const wrap = makeEntryWrapper(atom(false));
		const wrapped1 = wrap(InnerA);
		const wrapped2 = wrap(InnerA);
		expect(getStaleAwareInner(wrapped1)).toBe(InnerA);
		expect(getStaleAwareInner(wrapped2)).toBe(InnerA);
		// And — same wrap identity, so this is really one object.
		expect(wrapped1).toBe(wrapped2);
	});
});
