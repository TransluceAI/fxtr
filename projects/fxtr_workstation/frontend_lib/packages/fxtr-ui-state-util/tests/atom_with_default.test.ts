/**
 * Tests for `atomWithDefault`.
 *
 * Builds a mock `FxtrSessionAtom<T | null>` backed by a primitive
 * jotai atom + a hand-rolled write fn that simulates the ctx-passing
 * contract. Verifies read fallback, write passthrough (static +
 * function-form), ctx threading, composition with `subAtom`, and
 * the no-phantom-write invariant.
 */

import type {
	FxtrSessionAtom,
	FxtrSessionUpdateCtx,
	FxtrUpdate,
	Session,
	UserStateWriteOptions,
} from "@fxtr-ui/core";
import { atom, createStore } from "jotai";
import { describe, expect, it, vi } from "vitest";

import { atomWithDefault } from "../src/atom_with_default";
import { subAtom } from "../src/sub_atom";

/** Stub `FxtrSessionUpdateCtx` for tests. The allocate methods aren't
 * exercised here; we just need a stable identity to pass through. */
function makeCtx(): FxtrSessionUpdateCtx {
	const session: Session = {} as Session;
	return {
		allocateLoad: () => {
			throw new Error("not used");
		},
		allocateInvoke: () => {
			throw new Error("not used");
		},
		allocateGetEntityMetadata: () => {
			throw new Error("not used");
		},
		allocateGetServiceMetadata: () => {
			throw new Error("not used");
		},
		allocateCallback: () => {
			throw new Error("not used");
		},
		session,
	};
}

/** Build a mock `FxtrSessionAtom<T | null>` from a primitive atom. The
 * write fn dispatches function-form updates with `(prev, ctx)`, same as
 * `WorkstationSession.handleWrite` does. */
function makeMockSessionAtom<T>(
	initial: T | null,
	ctx: FxtrSessionUpdateCtx,
): FxtrSessionAtom<T | null> {
	const inner = atom<T | null>(initial);
	return atom(
		(get) => get(inner),
		(_get, set, update: FxtrUpdate<T | null>, _options?: UserStateWriteOptions) => {
			set(inner, (prev) =>
				typeof update === "function"
					? (update as (p: T | null, c: FxtrSessionUpdateCtx) => T | null)(prev, ctx)
					: update,
			);
		},
	);
}

interface PanelState {
	readonly message: string;
	readonly count: number;
}

const defaults: PanelState = { message: "", count: 0 };

describe("atomWithDefault: read fallback", () => {
	it("returns the default when the parent reads null", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>(null, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		expect(store.get(lifted)).toEqual(defaults);
	});

	it("returns the parent's value when it's non-null", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>({ message: "hi", count: 5 }, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		expect(store.get(lifted)).toEqual({ message: "hi", count: 5 });
	});

	it("read fallback is non-destructive — the parent's value stays null", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>(null, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		void store.get(lifted); // triggers a read
		expect(store.get(parent)).toBeNull();
	});
});

describe("atomWithDefault: static writes", () => {
	it("static write passes through to the parent unchanged", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>(null, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		store.set(lifted, { message: "foo", count: 1 });
		expect(store.get(parent)).toEqual({ message: "foo", count: 1 });
	});

	it("can read back through the wrapper after a write", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>(null, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		store.set(lifted, { message: "foo", count: 1 });
		expect(store.get(lifted)).toEqual({ message: "foo", count: 1 });
	});
});

describe("atomWithDefault: function-form writes", () => {
	it("function-form write sees the default as prev when parent is null", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>(null, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		const inner = vi.fn<(prev: PanelState, c: FxtrSessionUpdateCtx) => PanelState>((prev) => ({
			...prev,
			count: prev.count + 1,
		}));
		store.set(lifted, inner);
		expect(inner).toHaveBeenCalledTimes(1);
		expect(inner.mock.calls[0]?.[0]).toEqual(defaults);
		expect(store.get(parent)).toEqual({ ...defaults, count: 1 });
	});

	it("function-form write sees the parent's value as prev when parent is non-null", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>({ message: "hi", count: 5 }, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		const inner = vi.fn<(prev: PanelState, c: FxtrSessionUpdateCtx) => PanelState>((prev) => ({
			...prev,
			count: prev.count + 1,
		}));
		store.set(lifted, inner);
		expect(inner.mock.calls[0]?.[0]).toEqual({ message: "hi", count: 5 });
		expect(store.get(parent)).toEqual({ message: "hi", count: 6 });
	});

	it("function-form write threads the ctx through to the inner updater", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>(null, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		const inner = vi.fn<(prev: PanelState, c: FxtrSessionUpdateCtx) => PanelState>((p) => p);
		store.set(lifted, inner);
		// ctx identity matches the mock session's ctx — proof that
		// atomWithDefault doesn't synthesize its own ctx.
		expect(inner.mock.calls[0]?.[1]).toBe(ctx);
	});
});

describe("atomWithDefault: composition with subAtom", () => {
	it("subAtom drills into a key of the lifted atom", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>(null, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const messageAtom = subAtom(lifted, "message");
		const store = createStore();
		expect(store.get(messageAtom)).toBe(""); // from defaults

		store.set(messageAtom, "hello");
		expect(store.get(parent)).toEqual({ ...defaults, message: "hello" });
		expect(store.get(messageAtom)).toBe("hello");
	});

	it("subAtom write through lifted atom seeds with default for null parent", () => {
		const ctx = makeCtx();
		const parent = makeMockSessionAtom<PanelState>(null, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const countAtom = subAtom(lifted, "count");
		const store = createStore();
		store.set(countAtom, 42);
		// Parent now has defaults + count=42, written by the chain
		// subAtom -> atomWithDefault -> parent.
		expect(store.get(parent)).toEqual({ ...defaults, count: 42 });
	});
});

describe("atomWithDefault: undefined fallback", () => {
	/**
	 * `atomWithDefault` accepts `FxtrSessionAtom<T | null | undefined>`
	 * — substitutes the default for both `null` and `undefined` parent
	 * reads. Symmetry matters when composing with `subAtom`: a
	 * subAtom drilling into a `Record<string, V>` with a missing key
	 * reads as `undefined`, and lifting through `atomWithDefault`
	 * should treat that the same as a `null` parent.
	 */

	function makeMockNullableSessionAtom<T>(
		initial: T | null | undefined,
		ctx: FxtrSessionUpdateCtx,
	): FxtrSessionAtom<T | null | undefined> {
		const inner = atom<T | null | undefined>(initial);
		return atom(
			(get) => get(inner),
			(_get, set, update: FxtrUpdate<T | null | undefined>, _options?: UserStateWriteOptions) => {
				set(inner, (prev) =>
					typeof update === "function"
						? (
								update as (p: T | null | undefined, c: FxtrSessionUpdateCtx) => T | null | undefined
							)(prev, ctx)
						: update,
				);
			},
		);
	}

	it("returns the default when the parent reads undefined", () => {
		const ctx = makeCtx();
		const parent = makeMockNullableSessionAtom<PanelState>(undefined, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		expect(store.get(lifted)).toEqual(defaults);
	});

	it("function-form write sees default as prev when parent is undefined", () => {
		const ctx = makeCtx();
		const parent = makeMockNullableSessionAtom<PanelState>(undefined, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		const inner = vi.fn<(prev: PanelState, c: FxtrSessionUpdateCtx) => PanelState>((prev) => ({
			...prev,
			count: prev.count + 1,
		}));
		store.set(lifted, inner);
		expect(inner.mock.calls[0]?.[0]).toEqual(defaults);
		expect(store.get(parent)).toEqual({ ...defaults, count: 1 });
	});

	it("read fallback is non-destructive — parent stays undefined", () => {
		const ctx = makeCtx();
		const parent = makeMockNullableSessionAtom<PanelState>(undefined, ctx);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		void store.get(lifted);
		expect(store.get(parent)).toBeUndefined();
	});
});

describe("atomWithDefault: composition through Record-keyed projection", () => {
	/**
	 * End-to-end exercise of the dispatch package's intended use:
	 * `atomWithDefault` + nested `subAtom` chain over a `Record`-typed
	 * shape, where intermediate keys may be missing and `subAtom`'s
	 * `T[K]` projection (with `noUncheckedIndexedAccess`) widens to
	 * `V | undefined`. Each level's `atomWithDefault` must substitute
	 * defaults symmetrically for null and undefined.
	 */

	interface EntryData {
		readonly state: { phase: string };
		readonly shared: number | null;
	}
	type Root = { readonly entries: Record<string, Record<string, EntryData>> };
	const ROOT_DEFAULT: Root = { entries: {} };
	const ENTRY_DEFAULT: EntryData = { state: { phase: "init" }, shared: null };

	it("nested chain writes through to a fresh path when nothing exists", () => {
		const ctx = makeCtx();
		const inner = atom<Root | null>(null);
		const session: FxtrSessionAtom<Root | null> = atom(
			(get) => get(inner),
			(_get, set, update: FxtrUpdate<Root | null>, _options?: UserStateWriteOptions) => {
				set(inner, (prev) =>
					typeof update === "function"
						? (update as (p: Root | null, c: FxtrSessionUpdateCtx) => Root | null)(prev, ctx)
						: update,
				);
			},
		);

		const root = atomWithDefault(session, ROOT_DEFAULT);
		const entries = subAtom(root, "entries");
		const keyEntriesUndef = subAtom(entries, "k1");
		const keyEntries = atomWithDefault(keyEntriesUndef, {} as Record<string, EntryData>);
		const entryUndef = subAtom(keyEntries, "s1");
		const entry = atomWithDefault(entryUndef, ENTRY_DEFAULT);
		const stateAtom = subAtom(entry, "state");

		const store = createStore();
		// Cold read at the leaf: every intermediate level substitutes
		// its default, so we get ENTRY_DEFAULT.state.
		expect(store.get(stateAtom)).toEqual({ phase: "init" });

		// Write a new state value; the chain should materialize the
		// whole path.
		store.set(stateAtom, { phase: "resolved" });
		expect(store.get(session)).toEqual({
			entries: { k1: { s1: { state: { phase: "resolved" }, shared: null } } },
		});

		// Read back: the leaf reflects the write.
		expect(store.get(stateAtom)).toEqual({ phase: "resolved" });
	});

	it("subsequent writes at the leaf preserve sibling values", () => {
		const ctx = makeCtx();
		const inner = atom<Root | null>({
			entries: { k1: { s1: { state: { phase: "loading" }, shared: 7 } } },
		});
		const session: FxtrSessionAtom<Root | null> = atom(
			(get) => get(inner),
			(_get, set, update: FxtrUpdate<Root | null>) => {
				set(inner, (prev) =>
					typeof update === "function"
						? (update as (p: Root | null, c: FxtrSessionUpdateCtx) => Root | null)(prev, ctx)
						: update,
				);
			},
		);
		const root = atomWithDefault(session, ROOT_DEFAULT);
		const entries = subAtom(root, "entries");
		const keyEntries = atomWithDefault(subAtom(entries, "k1"), {} as Record<string, EntryData>);
		const entry = atomWithDefault(subAtom(keyEntries, "s1"), ENTRY_DEFAULT);
		const stateAtom = subAtom(entry, "state");

		const store = createStore();
		store.set(stateAtom, { phase: "resolved" });
		// `shared: 7` survives the write — only `state` was overwritten.
		expect(store.get(session)).toEqual({
			entries: { k1: { s1: { state: { phase: "resolved" }, shared: 7 } } },
		});
	});
});

describe("atomWithDefault: validityCheck", () => {
	/**
	 * The optional third argument guards against stale persisted
	 * state (e.g. an `ownStorage` slot written by an older renderer
	 * build): a non-null parent value failing the check is treated
	 * exactly like null — the default substitutes on read and as
	 * the function-updater's `prev`.
	 */

	interface Versioned {
		readonly kind: "panel/v2";
		readonly count: number;
	}
	const V2_DEFAULT: Versioned = { kind: "panel/v2", count: 0 };
	const isV2 = (v: Versioned): boolean => v?.kind === "panel/v2";

	it("substitutes the default when a non-null value fails the check", () => {
		const ctx = makeCtx();
		// Simulate a stale v1 shape persisted in the slot.
		const stale = { kind: "panel/v1", message: "old" } as unknown as Versioned;
		const parent = makeMockSessionAtom<Versioned>(stale, ctx);
		const lifted = atomWithDefault(parent, V2_DEFAULT, isV2);
		const store = createStore();
		expect(store.get(lifted)).toEqual(V2_DEFAULT);
		// Non-destructive: the parent still holds the stale value.
		expect(store.get(parent)).toBe(stale);
	});

	it("keeps a non-null value that passes the check", () => {
		const ctx = makeCtx();
		const good: Versioned = { kind: "panel/v2", count: 5 };
		const parent = makeMockSessionAtom<Versioned>(good, ctx);
		const lifted = atomWithDefault(parent, V2_DEFAULT, isV2);
		const store = createStore();
		expect(store.get(lifted)).toBe(good);
	});

	it("function-form write sees the default as prev when the value is stale", () => {
		const ctx = makeCtx();
		const stale = { kind: "panel/v1", message: "old" } as unknown as Versioned;
		const parent = makeMockSessionAtom<Versioned>(stale, ctx);
		const lifted = atomWithDefault(parent, V2_DEFAULT, isV2);
		const store = createStore();
		const inner = vi.fn<(prev: Versioned, c: FxtrSessionUpdateCtx) => Versioned>((prev) => ({
			...prev,
			count: prev.count + 1,
		}));
		store.set(lifted, inner);
		expect(inner.mock.calls[0]?.[0]).toEqual(V2_DEFAULT);
		expect(store.get(parent)).toEqual({ ...V2_DEFAULT, count: 1 });
	});

	it("null parent still substitutes the default without consulting the check", () => {
		const ctx = makeCtx();
		const check = vi.fn(isV2);
		const parent = makeMockSessionAtom<Versioned>(null, ctx);
		const lifted = atomWithDefault(parent, V2_DEFAULT, check);
		const store = createStore();
		expect(store.get(lifted)).toEqual(V2_DEFAULT);
		expect(check).not.toHaveBeenCalled();
	});
});

describe("atomWithDefault: options pass-through", () => {
	it("UserStateWriteOptions ride along to the parent's write", () => {
		const onRollback = vi.fn();
		const ctx = makeCtx();
		const seen: Array<NonNullable<UserStateWriteOptions["onRollback"]>> = [];
		const inner = atom<PanelState | null>(null);
		const parent: FxtrSessionAtom<PanelState | null> = atom(
			(get) => get(inner),
			(_get, set, update: FxtrUpdate<PanelState | null>, options?: UserStateWriteOptions) => {
				set(inner, (prev) =>
					typeof update === "function"
						? (update as (p: PanelState | null, c: FxtrSessionUpdateCtx) => PanelState | null)(
								prev,
								ctx,
							)
						: update,
				);
				if (options?.onRollback) seen.push(options.onRollback);
			},
		);
		const lifted = atomWithDefault(parent, defaults);
		const store = createStore();
		store.set(lifted, defaults, { onRollback });
		expect(seen).toHaveLength(1);
		expect(seen[0]).toBe(onRollback);
	});
});
