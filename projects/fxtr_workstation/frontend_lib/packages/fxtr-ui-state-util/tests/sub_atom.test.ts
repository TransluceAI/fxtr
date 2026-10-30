/**
 * Runtime tests for `subAtom`.
 *
 * The package is framework-free, so tests build a minimal mock
 * `FxtrSessionAtom<T>` from `jotai.atom` + a hand-rolled write fn
 * that simulates the ctx-passing contract a real
 * `WorkstationSession` would supply. We don't need an actual session
 * here — `subAtom`'s job is purely structural (project + propagate),
 * not session-aware.
 */

import { describe, expect, it, vi } from "vitest";

import type {
	CallbackInboxStub,
	EntityRefStub,
	FxtrSessionAtom,
	FxtrSessionUpdateCtx,
	FxtrUpdate,
	OpResultStub,
	Session,
	UserStateWriteOptions,
} from "@fxtr-ui/core";
import { atom, createStore } from "jotai";

import { subAtom } from "../src/sub_atom";

/**
 * Build a stand-in for the per-session ctx an updater would receive.
 * The `allocate*` methods are not exercised by `subAtom` itself; the
 * tests just thread a captured reference through to confirm identity.
 */
function makeCtx(): FxtrSessionUpdateCtx {
	const session: Session = {} as Session;
	return {
		allocateLoad: <T>(_ref: EntityRefStub<T>): OpResultStub<T> => {
			throw new Error("not used in subAtom tests");
		},
		allocateInvoke: ((..._args: unknown[]) => {
			throw new Error("not used in subAtom tests");
		}) as FxtrSessionUpdateCtx["allocateInvoke"],
		allocateGetEntityMetadata: () => {
			throw new Error("not used in subAtom tests");
		},
		allocateGetServiceMetadata: () => {
			throw new Error("not used in subAtom tests");
		},
		allocateCallback: <R>(): CallbackInboxStub<R> => {
			throw new Error("not used in subAtom tests");
		},
		session,
	};
}

/**
 * Build a mock `FxtrSessionAtom<T>` backed by a regular primitive
 * atom and a captured ctx. Writes resolve function-form updaters
 * with `(prev, ctx)` exactly the way `WorkstationSession.handleWrite`
 * does. The `onRollback` (if any) is captured into `rollbacks` so
 * tests can assert pass-through.
 */
function makeMockSessionAtom<T>(
	initial: T,
	ctx: FxtrSessionUpdateCtx,
): {
	atom: FxtrSessionAtom<T>;
	rollbacks: Array<NonNullable<UserStateWriteOptions["onRollback"]>>;
} {
	const inner = atom<T>(initial);
	const rollbacks: Array<NonNullable<UserStateWriteOptions["onRollback"]>> = [];
	const writable: FxtrSessionAtom<T> = atom(
		(get) => get(inner),
		(_get, set, update: FxtrUpdate<T>, options?: UserStateWriteOptions) => {
			set(inner, (prev) =>
				typeof update === "function"
					? (update as (p: T, c: FxtrSessionUpdateCtx) => T)(prev, ctx)
					: update,
			);
			if (options?.onRollback) rollbacks.push(options.onRollback);
		},
	);
	return { atom: writable, rollbacks };
}

describe("subAtom: read projection", () => {
	it("reads project from the parent", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ a: 1, b: "hello" }, ctx);
		const store = createStore();
		const aAtom = subAtom(parent, "a");
		const bAtom = subAtom(parent, "b");
		expect(store.get(aAtom)).toBe(1);
		expect(store.get(bAtom)).toBe("hello");
	});

	it("read updates when the parent updates", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ a: 1 }, ctx);
		const store = createStore();
		const aAtom = subAtom(parent, "a");
		expect(store.get(aAtom)).toBe(1);
		store.set(parent, { a: 42 });
		expect(store.get(aAtom)).toBe(42);
	});
});

describe("subAtom: object key writes", () => {
	it("static write replaces the field, leaves siblings", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ a: 1, b: "x" }, ctx);
		const store = createStore();
		const aAtom = subAtom(parent, "a");

		store.set(aAtom, 99);
		expect(store.get(parent)).toEqual({ a: 99, b: "x" });
	});

	it("function-form write threads the ctx through to the inner updater", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ a: 1, b: "x" }, ctx);
		const store = createStore();
		const aAtom = subAtom(parent, "a");

		const inner = vi.fn<(prev: number, c: FxtrSessionUpdateCtx) => number>(
			(prev, _ctx) => prev + 10,
		);
		store.set(aAtom, inner);
		expect(store.get(parent)).toEqual({ a: 11, b: "x" });
		expect(inner).toHaveBeenCalledTimes(1);
		expect(inner.mock.calls[0]?.[0]).toBe(1);
		// The ctx identity matches what the mock session would have
		// supplied — proving subAtom doesn't synthesize its own ctx.
		expect(inner.mock.calls[0]?.[1]).toBe(ctx);
	});
});

describe("subAtom: array indices", () => {
	it("read projects an element by index", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom([10, 20, 30] as number[], ctx);
		const store = createStore();
		const first = subAtom(parent, 0);
		const second = subAtom(parent, 1);
		expect(store.get(first)).toBe(10);
		expect(store.get(second)).toBe(20);
	});

	it("write replaces the element while preserving array shape", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom([10, 20, 30] as number[], ctx);
		const store = createStore();
		const second = subAtom(parent, 1);

		store.set(second, 99);
		const result = store.get(parent);
		expect(Array.isArray(result)).toBe(true);
		expect(result).toEqual([10, 99, 30]);
	});

	it("function-form write on an array index threads ctx through", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom([10, 20, 30] as number[], ctx);
		const store = createStore();
		const second = subAtom(parent, 1);

		const inner = vi.fn<(prev: number, c: FxtrSessionUpdateCtx) => number>((p) => p * 2);
		store.set(second, inner);
		expect(store.get(parent)).toEqual([10, 40, 30]);
		expect(inner.mock.calls[0]?.[1]).toBe(ctx);
	});
});

describe("subAtom: nested projection", () => {
	it("subAtom of a subAtom drills further", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ c: { x: 1, y: 2 } }, ctx);
		const store = createStore();
		const cAtom = subAtom(parent, "c");
		const xAtom = subAtom(cAtom, "x");

		expect(store.get(xAtom)).toBe(1);

		store.set(xAtom, 42);
		expect(store.get(parent)).toEqual({ c: { x: 42, y: 2 } });
	});

	it("nested function-form write threads ctx all the way down", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ c: { x: 1 } }, ctx);
		const store = createStore();
		const cAtom = subAtom(parent, "c");
		const xAtom = subAtom(cAtom, "x");

		const inner = vi.fn<(prev: number, c: FxtrSessionUpdateCtx) => number>((p) => p + 100);
		store.set(xAtom, inner);
		expect(store.get(parent)).toEqual({ c: { x: 101 } });
		expect(inner.mock.calls[0]?.[1]).toBe(ctx);
	});
});

describe("subAtom: options pass-through", () => {
	it("onRollback rides along to the parent's write call", () => {
		const ctx = makeCtx();
		const { atom: parent, rollbacks } = makeMockSessionAtom({ a: 1 }, ctx);
		const store = createStore();
		const aAtom = subAtom(parent, "a");

		const onRollback = vi.fn();
		store.set(aAtom, 7, { onRollback });
		expect(rollbacks).toHaveLength(1);
		expect(rollbacks[0]).toBe(onRollback);
	});
});

// --- 9b extras: edge cases worth pinning down ---

describe("subAtom: writing to a key not present on the parent adds it", () => {
	it("static write adds the field to the parent dict", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom<{ a: number; b?: number }>({ a: 1 }, ctx);
		const store = createStore();
		const bAtom = subAtom(parent, "b");
		expect(store.get(bAtom)).toBeUndefined();
		store.set(bAtom, 42);
		expect(store.get(parent)).toEqual({ a: 1, b: 42 });
	});

	it("function-form write sees prev = undefined when the key is absent", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom<{ a: number; b?: number }>({ a: 1 }, ctx);
		const store = createStore();
		const bAtom = subAtom(parent, "b");
		const updater = vi.fn<(p: number | undefined, c: FxtrSessionUpdateCtx) => number>(
			(p) => (p ?? 0) + 7,
		);
		store.set(bAtom, updater);
		expect(updater.mock.calls[0]?.[0]).toBeUndefined();
		expect(store.get(parent)).toEqual({ a: 1, b: 7 });
	});
});

describe("subAtom: identity / referential stability", () => {
	it("returns a fresh atom instance on each call (callers must memoize)", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ a: 1 }, ctx);
		const a1 = subAtom(parent, "a");
		const a2 = subAtom(parent, "a");
		// Different instances but functionally equivalent — react
		// callers wrap in `useMemo` (i.e. `useSubAtom`) to keep
		// rendering stable.
		expect(a1).not.toBe(a2);
		const store = createStore();
		expect(store.get(a1)).toBe(store.get(a2));
	});

	it("a deeply chained subAtom write threads ctx through all layers", () => {
		// Three-deep nesting to verify the recursive ctx threading
		// holds beyond the two-layer case in the earlier test.
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ a: { b: { c: 1 } } }, ctx);
		const store = createStore();
		const aAtom = subAtom(parent, "a");
		const bAtom = subAtom(aAtom, "b");
		const cAtom = subAtom(bAtom, "c");

		const updater = vi.fn<(p: number, c: FxtrSessionUpdateCtx) => number>((p) => p + 10);
		store.set(cAtom, updater);
		expect(updater.mock.calls[0]?.[1]).toBe(ctx);
		expect(store.get(parent)).toEqual({ a: { b: { c: 11 } } });
	});
});

describe("subAtom: read tracks parent updates", () => {
	it("changing the parent's value at the projected key updates the child read", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ a: 1, b: 2 }, ctx);
		const store = createStore();
		const aAtom = subAtom(parent, "a");
		expect(store.get(aAtom)).toBe(1);
		store.set(parent, { a: 99, b: 2 });
		expect(store.get(aAtom)).toBe(99);
	});

	it("changing the parent's value at a different key leaves the projected read unchanged", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom({ a: 1, b: 2 }, ctx);
		const store = createStore();
		const aAtom = subAtom(parent, "a");
		store.set(parent, { a: 1, b: 999 });
		expect(store.get(aAtom)).toBe(1);
	});
});

describe("subAtom: undefined-write semantics", () => {
	/**
	 * `undefined` has well-defined semantics on the wire (object fields
	 * with explicit-`undefined` values are treated as absent by
	 * `escapeWire` / `diffStateToPatch` / `collectStubsInState`), but
	 * the same convention doesn't extend to array elements — sparse-
	 * undefined slots crash `escapeWire`. So `subAtom`'s write path
	 * normalizes:
	 *   - Object parent + `undefined` write → key deleted entirely
	 *     (matches the wire's "absent" semantics, no phantom undefined
	 *     sitting in memory).
	 *   - Array parent + `undefined` write → synchronous throw (the
	 *     wire codec would crash on the next flush; failing here
	 *     points the stack at the offending write).
	 */

	it("write-undefined on an object parent deletes the key", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom<{ a?: number; b: string }>(
			{ a: 1, b: "keep" },
			ctx,
		);
		const store = createStore();
		const aAtom = subAtom(parent, "a");
		store.set(aAtom, undefined);
		const after = store.get(parent);
		// `a` is *absent* (not present-with-undefined) and `b` is preserved.
		expect("a" in after).toBe(false);
		expect(after).toEqual({ b: "keep" });
	});

	it("write-undefined on a missing object key is a no-op (key stays absent)", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom<{ a?: number; b: string }>({ b: "keep" }, ctx);
		const store = createStore();
		const aAtom = subAtom(parent, "a");
		store.set(aAtom, undefined);
		const after = store.get(parent);
		expect("a" in after).toBe(false);
		expect(after).toEqual({ b: "keep" });
	});

	it("function-form update returning undefined deletes the key", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom<{ a?: number; b: string }>(
			{ a: 7, b: "keep" },
			ctx,
		);
		const store = createStore();
		const aAtom = subAtom(parent, "a");
		store.set(aAtom, (_prev) => undefined);
		expect("a" in store.get(parent)).toBe(false);
	});

	it("write-undefined to an array index throws synchronously", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom([10, 20, 30] as ReadonlyArray<number>, ctx);
		const store = createStore();
		const i1 = subAtom(parent, 1);
		expect(() => store.set(i1, undefined as unknown as number)).toThrow(/array index/);
		// Parent state is unchanged on throw.
		expect(store.get(parent)).toEqual([10, 20, 30]);
	});

	it("function-form update on array returning undefined throws", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom([10, 20, 30] as ReadonlyArray<number>, ctx);
		const store = createStore();
		const i0 = subAtom(parent, 0);
		expect(() => store.set(i0, (_p) => undefined as unknown as number)).toThrow(/array index/);
		expect(store.get(parent)).toEqual([10, 20, 30]);
	});

	it("nested subAtom: undefined-write at the leaf deletes only that leaf's key", () => {
		const ctx = makeCtx();
		const { atom: parent } = makeMockSessionAtom<{
			outer: { a?: number; b: string };
		}>({ outer: { a: 1, b: "keep" } }, ctx);
		const store = createStore();
		const outerAtom = subAtom(parent, "outer");
		const aAtom = subAtom(outerAtom, "a");
		store.set(aAtom, undefined);
		expect(store.get(parent)).toEqual({ outer: { b: "keep" } });
		expect("a" in store.get(outerAtom)).toBe(false);
	});
});
