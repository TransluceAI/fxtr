/**
 * Test-side mocks for `@fxtr-ui/rd-types-channel`.
 *
 * Modeled on `@fxtr-ui/react-dispatch`'s `tests/_mocks.ts` (test
 * files aren't importable across packages), narrowed to what the
 * channel tests need: stub refs with controllable identities, a
 * controllable callback inbox, and a `FxtrSessionAtom` whose
 * function-form-updater ctx dispenses queued inbox / invoke stubs —
 * so `useChannelSubscription`'s allocate-and-store mount effect runs
 * against scripted slots.
 */

import {
	CallbackHandleStub,
	CallbackInboxStub,
	EntityRefStub,
	type FxtrSessionAtom,
	type FxtrSessionUpdateCtx,
	type FxtrUpdate,
	NotImplementedError,
	OpResultStub,
	type ResultStatus,
	ServiceRefStub,
	type UserStateWriteOptions,
} from "@fxtr-ui/core";
import { type Atom, type PrimitiveAtom, atom } from "jotai";

export class TestEntityRefStub<T = unknown> extends EntityRefStub<T> {
	readonly #shortId: string;
	constructor(shortId: string) {
		super();
		this.#shortId = shortId;
	}
	override get shortId(): string {
		return this.#shortId;
	}
}

export class TestServiceRefStub<T = unknown> extends ServiceRefStub<T> {
	readonly #shortId: string;
	constructor(shortId: string) {
		super();
		this.#shortId = shortId;
	}
	override get shortId(): string {
		return this.#shortId;
	}
}

/** Concrete `OpResultStub`, controllable via `_resultPrivate`. */
export class TestOpResultStub<T = unknown> extends OpResultStub<T> {
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

export class TestCallbackHandleStub<R = unknown> extends CallbackHandleStub<R> {}

/**
 * Concrete `CallbackInboxStub` whose `events` atom is driven by the
 * test through `_eventsPrivate` (push the scripted event sequence,
 * the subscribed component re-renders).
 */
export class TestCallbackInboxStub<R = unknown> extends CallbackInboxStub<R> {
	readonly _eventsPrivate: PrimitiveAtom<readonly R[]>;
	readonly #events: Atom<readonly R[]>;
	readonly #state: Atom<"opening" | "open" | "closed">;
	readonly #committed: Atom<boolean>;
	readonly #handle: CallbackHandleStub<R>;

	constructor(events: readonly R[] = []) {
		super();
		this._eventsPrivate = atom<readonly R[]>(events);
		this.#events = atom((get) => get(this._eventsPrivate));
		this.#state = atom<"opening" | "open" | "closed">("open");
		this.#committed = atom(true);
		this.#handle = new TestCallbackHandleStub<R>();
	}

	override get offset(): number {
		return 0;
	}
	override get events(): Atom<readonly R[]> {
		return this.#events;
	}
	override get state(): Atom<"opening" | "open" | "closed"> {
		return this.#state;
	}
	override get connectionState(): Atom<"idle" | "connected" | "broken"> {
		return atom<"idle" | "connected" | "broken">("connected");
	}
	override get subscriptionBreakReason(): Atom<string | null> {
		return atom<string | null>(null);
	}
	override get committed(): Atom<boolean> {
		return this.#committed;
	}
	override get handle(): CallbackHandleStub<R> {
		return this.#handle;
	}
	override sliced(_n: number): CallbackInboxStub<R> {
		throw new NotImplementedError("TestCallbackInboxStub.sliced not wired");
	}
}

export interface MockInvokeCall {
	readonly target: unknown;
	readonly method: string;
	readonly args: ReadonlyArray<unknown>;
}

export interface MockSessionAtomOptions<S> {
	readonly initial: S;
	/** `ctx.allocateCallback()` pops from here; throws when dry. */
	readonly callbackQueue?: Array<TestCallbackInboxStub<unknown>>;
	/** `ctx.allocateInvoke(...)` pops from here; throws when dry. */
	readonly invokeQueue?: Array<TestOpResultStub<unknown>>;
	/** Captured `allocateInvoke` calls, for assertions. */
	readonly invokeCalls?: Array<MockInvokeCall>;
}

/**
 * Build a `FxtrSessionAtom<S>` backed by a primitive jotai atom,
 * preserving the ctx-passing contract for function-form updaters.
 */
export function makeMockSessionAtom<S>(opts: MockSessionAtomOptions<S>): {
	readonly atom: FxtrSessionAtom<S>;
	readonly privateAtom: PrimitiveAtom<S>;
} {
	const privateAtom = atom<S>(opts.initial);
	const callbackQueue = opts.callbackQueue ?? [];
	const invokeQueue = opts.invokeQueue ?? [];
	const invokeCalls = opts.invokeCalls;

	const ctx: FxtrSessionUpdateCtx = {
		allocateLoad: () => {
			throw new NotImplementedError("makeMockSessionAtom: allocateLoad not wired");
		},
		allocateInvoke: ((target: unknown, method: string, args: unknown[]) => {
			const next = invokeQueue.shift();
			if (next === undefined) {
				throw new Error("makeMockSessionAtom: invoke queue exhausted");
			}
			invokeCalls?.push({ target, method, args });
			return next;
			// biome-ignore lint/suspicious/noExplicitAny: matches `FxtrSessionUpdateCtx.allocateInvoke`'s overloaded signature
		}) as any,
		allocateGetEntityMetadata: () => {
			throw new NotImplementedError("makeMockSessionAtom: allocateGetEntityMetadata not wired");
		},
		allocateGetServiceMetadata: () => {
			throw new NotImplementedError("makeMockSessionAtom: allocateGetServiceMetadata not wired");
		},
		allocateCallback: <R>() => {
			const next = callbackQueue.shift();
			if (next === undefined) {
				throw new Error("makeMockSessionAtom: callback queue exhausted");
			}
			return next as unknown as CallbackInboxStub<R>;
		},
		get session(): never {
			throw new NotImplementedError("makeMockSessionAtom: no session ref attached");
		},
	};

	const publicAtom: FxtrSessionAtom<S> = atom(
		(get) => get(privateAtom),
		(get, set, update: FxtrUpdate<S>, _options?: UserStateWriteOptions) => {
			const prev = get(privateAtom);
			const next =
				typeof update === "function"
					? (update as (p: S, c: FxtrSessionUpdateCtx) => S)(prev, ctx)
					: update;
			set(privateAtom, next);
		},
	);

	return { atom: publicAtom, privateAtom };
}
