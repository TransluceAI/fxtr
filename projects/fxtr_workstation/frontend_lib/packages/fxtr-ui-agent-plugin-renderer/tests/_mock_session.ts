/**
 * Test-side stubs + a controllable :data:`FxtrSessionAtom` for
 * state-machine tests. Mirrors the dispatch package's
 * ``_mocks.ts`` but extends it with :func:`allocateCallback` (the
 * agent panel needs an inbox) and slims out the metadata
 * queues (we never call those).
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

export class TestOpResultStub<T = unknown> extends OpResultStub<T> {
	readonly _resultPrivate: PrimitiveAtom<ResultStatus<T>>;
	readonly _committedPrivate: PrimitiveAtom<boolean>;
	readonly #result: Atom<ResultStatus<T>>;
	readonly #committed: Atom<boolean>;
	readonly #done: Atom<boolean>;

	constructor(initial: ResultStatus<T> = { status: "pending" }) {
		super();
		this._resultPrivate = atom<ResultStatus<T>>(initial);
		this._committedPrivate = atom(false);
		this.#result = atom((get) => get(this._resultPrivate));
		this.#committed = atom((get) => get(this._committedPrivate));
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

export class TestCallbackInboxStub<R = unknown> extends CallbackInboxStub<R> {
	readonly #offset: number;
	readonly _eventsPrivate: PrimitiveAtom<readonly R[]>;
	readonly _statePrivate: PrimitiveAtom<"opening" | "open" | "closed">;
	readonly _committedPrivate: PrimitiveAtom<boolean>;
	readonly #events: Atom<readonly R[]>;
	readonly #state: Atom<"opening" | "open" | "closed">;
	readonly #committed: Atom<boolean>;
	readonly #handle: CallbackHandleStub<R>;
	// Static — these tests don't exercise subscription breaks.
	readonly #connectionState: Atom<"idle" | "connected" | "broken"> = atom<
		"idle" | "connected" | "broken"
	>("idle");
	readonly #subscriptionBreakReason: Atom<string | null> = atom<string | null>(null);
	// Tracks how many times `sliced(n)` has been called on this
	// stub, summed by n — the state machine should call this
	// once per drain pass with n = events.length, so this gives
	// us a deterministic check on cursor advance.
	slicedTotal = 0;

	constructor(
		opts: {
			readonly offset?: number;
			readonly events?: readonly R[];
			readonly state?: "opening" | "open" | "closed";
		} = {},
	) {
		super();
		this.#offset = opts.offset ?? 0;
		this._eventsPrivate = atom<readonly R[]>(opts.events ?? []);
		this._statePrivate = atom<"opening" | "open" | "closed">(opts.state ?? "open");
		this._committedPrivate = atom(true);
		this.#events = atom((get) => get(this._eventsPrivate));
		this.#state = atom((get) => get(this._statePrivate));
		this.#committed = atom((get) => get(this._committedPrivate));
		this.#handle = new TestCallbackHandleStub<R>();
	}

	override get offset(): number {
		return this.#offset;
	}
	override get events(): Atom<readonly R[]> {
		return this.#events;
	}
	override get state(): Atom<"opening" | "open" | "closed"> {
		return this.#state;
	}
	override get committed(): Atom<boolean> {
		return this.#committed;
	}
	override get connectionState(): Atom<"idle" | "connected" | "broken"> {
		return this.#connectionState;
	}
	override get subscriptionBreakReason(): Atom<string | null> {
		return this.#subscriptionBreakReason;
	}
	override get handle(): CallbackHandleStub<R> {
		return this.#handle;
	}

	override sliced(n: number): CallbackInboxStub<R> {
		this.slicedTotal += n;
		// Sliced view: a fresh stub at offset + n with the
		// remaining events visible. Tests typically check `offset`
		// + `slicedTotal` to confirm drains happen as expected.
		const remaining = (() => {
			const events = this._eventsPrivate;
			return atom<readonly R[]>((get) => get(events).slice(n));
		})();
		const sliced = new TestCallbackInboxStub<R>({
			offset: this.#offset + n,
			state: "open",
		});
		(sliced as unknown as { _eventsPrivate: Atom<readonly R[]> })._eventsPrivate = remaining;
		(
			sliced as unknown as { _statePrivate: PrimitiveAtom<"opening" | "open" | "closed"> }
		)._statePrivate = this._statePrivate;
		return sliced;
	}
}

export interface MockInvokeCall {
	/** The invoke target — a service ref, or (for callback-target
	 * invokes like the panel's `subscribe_to`) an inbox's handle. */
	readonly stub: ServiceRefStub<unknown> | CallbackHandleStub<unknown>;
	readonly method: string;
	readonly args: ReadonlyArray<unknown>;
	readonly kwargs?: Record<string, unknown>;
}

export interface MockSessionAtomOptions<S> {
	readonly initial: S;
	/** Queue of stubs returned by ctx.allocateLoad in call order. */
	readonly loadQueue?: Array<TestOpResultStub<unknown>>;
	/** Queue of stubs returned by ctx.allocateInvoke in call order.
	 *  Holds stubs of heterogeneous slot params (the stubs are invariant
	 *  in their param), so the element type is intentionally loose. */
	// biome-ignore lint/suspicious/noExplicitAny: heterogeneous slot params
	readonly invokeQueue?: Array<TestOpResultStub<any>>;
	/** Queue of stubs returned by ctx.allocateCallback in call order. */
	// biome-ignore lint/suspicious/noExplicitAny: heterogeneous slot params
	readonly callbackQueue?: Array<TestCallbackInboxStub<any>>;
	/** Side-channel capture of allocateInvoke calls observed. */
	readonly invokeCalls?: Array<MockInvokeCall>;
	/** Side-channel capture of allocateLoad calls observed (the
	 *  ref each call was given). */
	readonly loadCalls?: Array<EntityRefStub<unknown>>;
}

export function makeMockSessionAtom<S>(opts: MockSessionAtomOptions<S>): {
	readonly atom: FxtrSessionAtom<S>;
	readonly privateAtom: PrimitiveAtom<S>;
} {
	const privateAtom = atom<S>(opts.initial);
	const loadQueue = opts.loadQueue ?? [];
	const invokeQueue = opts.invokeQueue ?? [];
	const callbackQueue = opts.callbackQueue ?? [];
	const invokeCalls = opts.invokeCalls;
	const loadCalls = opts.loadCalls;

	const ctx: FxtrSessionUpdateCtx = {
		allocateLoad: <T>(ref: EntityRefStub<T>) => {
			const stub = loadQueue.shift();
			if (stub === undefined) {
				throw new Error("makeMockSessionAtom: load queue exhausted");
			}
			loadCalls?.push(ref as EntityRefStub<unknown>);
			return stub as unknown as OpResultStub<T>;
		},
		allocateInvoke: ((
			svc: ServiceRefStub<unknown>,
			method: string,
			args: unknown[],
			kwargs?: Record<string, unknown>,
		) => {
			const next = invokeQueue.shift();
			if (next === undefined) {
				throw new Error("makeMockSessionAtom: invoke queue exhausted");
			}
			const call: MockInvokeCall =
				kwargs === undefined ? { stub: svc, method, args } : { stub: svc, method, args, kwargs };
			invokeCalls?.push(call);
			return next as unknown as OpResultStub<unknown>;
			// biome-ignore lint/suspicious/noExplicitAny: matches the overloaded signature
		}) as any,
		allocateCallback: <R>() => {
			const stub = callbackQueue.shift();
			if (stub === undefined) {
				throw new Error("makeMockSessionAtom: callback queue exhausted");
			}
			return stub as unknown as CallbackInboxStub<R>;
		},
		allocateGetEntityMetadata: () => {
			throw new NotImplementedError("makeMockSessionAtom: entity-metadata not wired");
		},
		allocateGetServiceMetadata: () => {
			throw new NotImplementedError("makeMockSessionAtom: service-metadata not wired");
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
