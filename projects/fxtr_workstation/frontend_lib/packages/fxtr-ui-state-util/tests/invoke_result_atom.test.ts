/**
 * Runtime tests for `makeInvokeResultAtom`.
 *
 * Framework-free: a minimal mock `FxtrSessionAtom<T>` whose
 * function-form-updater ctx dispenses a scripted `OpResultStub` from
 * `allocateInvoke` (and records the call), plus a controllable
 * `OpResultStub` mock whose result atom the test drives. The returned
 * atom's `atomEffect` driver only runs once mounted, so each test
 * subscribes (`store.sub`) and flushes a macrotask before asserting.
 */

import { describe, expect, it } from "vitest";

import {
	type FxtrSessionAtom,
	type FxtrSessionUpdateCtx,
	type FxtrUpdate,
	NotImplementedError,
	OpResultStub,
	type ResultStatus,
	ServiceRefStub,
	type UserStateWriteOptions,
} from "@fxtr-ui/core";
import { type Atom, type PrimitiveAtom, atom, createStore } from "jotai";

import { makeInvokeResultAtom } from "../src/invoke_result_atom";

class TestServiceRefStub<T = unknown> extends ServiceRefStub<T> {
	readonly #shortId: string;
	constructor(shortId: string) {
		super();
		this.#shortId = shortId;
	}
	override get shortId(): string {
		return this.#shortId;
	}
}

class TestOpResultStub<T = unknown> extends OpResultStub<T> {
	readonly _resultPrivate: PrimitiveAtom<ResultStatus<T>>;
	readonly #result: Atom<ResultStatus<T>>;
	readonly #committed: Atom<boolean>;
	readonly #done: Atom<boolean>;
	constructor(initial: ResultStatus<T> = { status: "pending" }) {
		super();
		this._resultPrivate = atom<ResultStatus<T>>(initial);
		this.#result = atom((get) => get(this._resultPrivate));
		this.#committed = atom((get) => get(this._resultPrivate).status !== "pending");
		this.#done = atom((get) => get(this._resultPrivate).status !== "pending");
	}
	override get result(): Atom<ResultStatus<T>> {
		return this.#result;
	}
	override get committed(): Atom<boolean> {
		return this.#committed;
	}
	override get done(): Atom<boolean> {
		return this.#done;
	}
}

interface InvokeCall {
	readonly target: unknown;
	readonly method: string;
	readonly args: ReadonlyArray<unknown>;
}

/**
 * Mock `FxtrSessionAtom<OpResultStub<T> | null>` whose updater ctx pops
 * the next scripted stub from `invokeQueue` and records the call.
 */
function makeSlot<T>(invokeQueue: Array<TestOpResultStub<T>>, calls: InvokeCall[]) {
	const inner = atom<OpResultStub<T> | null>(null);
	const ctx: FxtrSessionUpdateCtx = {
		allocateLoad: () => {
			throw new NotImplementedError("not used");
		},
		allocateInvoke: ((target: unknown, method: string, args: unknown[]) => {
			const next = invokeQueue.shift();
			if (next === undefined) throw new Error("invoke queue exhausted");
			calls.push({ target, method, args });
			return next;
			// biome-ignore lint/suspicious/noExplicitAny: matches the overloaded ctx signature
		}) as any,
		allocateGetEntityMetadata: () => {
			throw new NotImplementedError("not used");
		},
		allocateGetServiceMetadata: () => {
			throw new NotImplementedError("not used");
		},
		allocateCallback: () => {
			throw new NotImplementedError("not used");
		},
		get session(): never {
			throw new NotImplementedError("no session");
		},
	};
	const slot: FxtrSessionAtom<OpResultStub<T> | null> = atom(
		(get) => get(inner),
		(get, set, update: FxtrUpdate<OpResultStub<T> | null>, _o?: UserStateWriteOptions) => {
			const prev = get(inner);
			set(
				inner,
				typeof update === "function"
					? (
							update as (
								p: OpResultStub<T> | null,
								c: FxtrSessionUpdateCtx,
							) => OpResultStub<T> | null
						)(prev, ctx)
					: update,
			);
		},
	);
	return slot;
}

/** Flush the macrotask the atomEffect driver schedules on mount. */
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

describe("makeInvokeResultAtom", () => {
	it("issues the invoke once on first subscribe and reports loading while pending", async () => {
		const calls: InvokeCall[] = [];
		const stub = new TestOpResultStub<string>();
		const slot = makeSlot([stub], calls);
		const svc = new TestServiceRefStub("reader-1");
		const store = createStore();
		const resultAtom = makeInvokeResultAtom<string>(slot, svc, "open_subchannel_reader", ["k1"]);

		store.sub(resultAtom, () => {});
		await flush();

		expect(calls).toHaveLength(1);
		expect(calls[0]).toMatchObject({ target: svc, method: "open_subchannel_reader", args: ["k1"] });
		expect(store.get(resultAtom)).toEqual({ kind: "loading" });
		// The slot now holds the allocated stub.
		expect(store.get(slot)).toBe(stub);
	});

	it("projects a filled result", async () => {
		const calls: InvokeCall[] = [];
		const stub = new TestOpResultStub<string>();
		const slot = makeSlot([stub], calls);
		const store = createStore();
		const resultAtom = makeInvokeResultAtom<string>(slot, new TestServiceRefStub("r"), "m", []);

		store.sub(resultAtom, () => {});
		await flush();
		store.set(stub._resultPrivate, { status: "filled", value: "child-reader" });

		expect(store.get(resultAtom)).toEqual({ kind: "filled", value: "child-reader" });
	});

	it("projects an errored result", async () => {
		const calls: InvokeCall[] = [];
		const stub = new TestOpResultStub<string>();
		const slot = makeSlot([stub], calls);
		const store = createStore();
		const resultAtom = makeInvokeResultAtom<string>(slot, new TestServiceRefStub("r"), "m", []);

		store.sub(resultAtom, () => {});
		await flush();
		store.set(stub._resultPrivate, { status: "errored", message: "channel has finished" });

		expect(store.get(resultAtom)).toEqual({ kind: "errored", message: "channel has finished" });
	});

	it("does not re-allocate once the slot is filled (single invoke per slot)", async () => {
		const calls: InvokeCall[] = [];
		// Only one stub queued — a second allocate would throw "queue exhausted".
		const stub = new TestOpResultStub<string>();
		const slot = makeSlot([stub], calls);
		const store = createStore();
		const resultAtom = makeInvokeResultAtom<string>(slot, new TestServiceRefStub("r"), "m", []);

		store.sub(resultAtom, () => {});
		await flush();
		// Drive a result change → the driver effect re-runs but must guard on the
		// non-null slot and not allocate again.
		store.set(stub._resultPrivate, { status: "filled", value: "x" });
		await flush();

		expect(calls).toHaveLength(1);
	});
});
