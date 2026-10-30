/**
 * Tests for `makeFigureStorage`.
 *
 * Confirms that the per-figure storage lookup:
 *
 * - returns the same atom instance across calls with the same key
 *   (identity-stable for React memoization),
 * - returns distinct atoms for distinct keys,
 * - reads project the right slot out of the parent `figures` object,
 * - writes mutate only the corresponding slot, leaving siblings intact.
 */

import { describe, expect, it } from "vitest";

import type {
	CallbackInboxStub,
	EntityRefStub,
	FxtrSessionAtom,
	FxtrSessionUpdateCtx,
	FxtrUpdate,
	OpResultStub,
	Session,
} from "@fxtr-ui/core";
import { atom, createStore } from "jotai";

import { makeFigureStorage } from "../src/figure_storage";

function makeCtx(): FxtrSessionUpdateCtx {
	const session: Session = {} as Session;
	return {
		allocateLoad: <T>(_ref: EntityRefStub<T>): OpResultStub<T> => {
			throw new Error("not used");
		},
		allocateInvoke: ((..._args: unknown[]) => {
			throw new Error("not used");
		}) as FxtrSessionUpdateCtx["allocateInvoke"],
		allocateGetEntityMetadata: () => {
			throw new Error("not used");
		},
		allocateGetServiceMetadata: () => {
			throw new Error("not used");
		},
		allocateCallback: <R>(): CallbackInboxStub<R> => {
			throw new Error("not used");
		},
		session,
	};
}

function makeFiguresAtom(
	initial: Record<string, unknown>,
	ctx: FxtrSessionUpdateCtx,
): FxtrSessionAtom<Record<string, unknown>> {
	const inner = atom<Record<string, unknown>>(initial);
	return atom(
		(get) => get(inner),
		(_get, set, update: FxtrUpdate<Record<string, unknown>>) => {
			set(inner, (prev) =>
				typeof update === "function"
					? (
							update as (
								p: Record<string, unknown>,
								c: FxtrSessionUpdateCtx,
							) => Record<string, unknown>
						)(prev, ctx)
					: update,
			);
		},
	);
}

describe("makeFigureStorage", () => {
	it("returns the same atom instance for repeated calls with the same key", () => {
		const ctx = makeCtx();
		const figures = makeFiguresAtom({}, ctx);
		const lookup = makeFigureStorage(figures);

		const a = lookup("figure-0");
		const b = lookup("figure-0");
		expect(a).toBe(b);
	});

	it("returns distinct atoms for distinct keys", () => {
		const ctx = makeCtx();
		const figures = makeFiguresAtom({}, ctx);
		const lookup = makeFigureStorage(figures);

		const a = lookup("figure-0");
		const b = lookup("figure-1");
		expect(a).not.toBe(b);
	});

	it("reads project the matching slot", () => {
		const ctx = makeCtx();
		const figures = makeFiguresAtom({ "figure-0": "alpha", "figure-1": 42 }, ctx);
		const lookup = makeFigureStorage(figures);
		const store = createStore();

		expect(store.get(lookup("figure-0"))).toBe("alpha");
		expect(store.get(lookup("figure-1"))).toBe(42);
	});

	it("writes mutate only the named slot", () => {
		const ctx = makeCtx();
		const figures = makeFiguresAtom({ "figure-0": "alpha", "figure-1": "beta" }, ctx);
		const lookup = makeFigureStorage(figures);
		const store = createStore();

		store.set(lookup("figure-0"), "ALPHA");

		expect(store.get(figures)).toEqual({ "figure-0": "ALPHA", "figure-1": "beta" });
	});

	it("reads return undefined for un-populated slots", () => {
		const ctx = makeCtx();
		const figures = makeFiguresAtom({}, ctx);
		const lookup = makeFigureStorage(figures);
		const store = createStore();

		expect(store.get(lookup("figure-0"))).toBeUndefined();
	});
});
