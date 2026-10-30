/**
 * Test-side mocks for `@fxtr-ui/react-dispatch` non-React tests.
 *
 * The dispatch package's behavior is driven through abstract
 * `@fxtr-ui/core` interfaces (Session, EntityRefStub, OpResultStub).
 * For state-machine tests we need to:
 *
 *   - Mint refs + stubs with controllable identities.
 *   - Build a `FxtrSessionAtom`-shaped atom whose function-form
 *     updaters get a `ctx` with controllable `allocateLoad` /
 *     `allocateGetEntityMetadata` / `allocateGetServiceMetadata`
 *     so the state machine's `ctx.allocate*(ref)` calls produce
 *     `TestOpResultStub`s the test can drive externally.
 *
 * Patterned after `@fxtr-ui/react-tests`'s `_mocks.ts`, narrowed
 * to what the dispatch state-machine tests need.
 */

import {
	CallbackHandleStub,
	CallbackInboxStub,
	type EntityMetadata,
	EntityRefStub,
	type FxtrSessionAtom,
	type FxtrSessionUpdateCtx,
	type FxtrUpdate,
	NotImplementedError,
	OpResultStub,
	type ResultStatus,
	type ServiceMetadata,
	ServiceRefStub,
	type UserStateWriteOptions,
} from "@fxtr-ui/core";
import { type Atom, type PrimitiveAtom, atom } from "jotai";

// --- Stub helpers ----------------------------------------------------

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

/**
 * Concrete `OpResultStub` with a write-through `_resultPrivate`
 * primitive atom so tests can flip pending → filled / errored.
 */
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

/**
 * Concrete `CallbackHandleStub` for tests. Carries no behaviour
 * beyond identity — the dispatcher passes it as an invoke arg
 * (where the test layer's `escapeInvokeArgs` would normally
 * encode it as `["callback", slot]`), and tests assert against
 * the stub identity if they care.
 */
export class TestCallbackHandleStub<R = unknown> extends CallbackHandleStub<R> {}

/**
 * Concrete `CallbackInboxStub` with controllable atoms so tests
 * can drive event arrival + state transitions. ``state`` defaults
 * to ``"open"``; tests can flip it to ``"closed"`` via
 * ``_statePrivate``.
 */
export class TestCallbackInboxStub<R = unknown> extends CallbackInboxStub<R> {
	readonly #offset: number;
	readonly _eventsPrivate: PrimitiveAtom<readonly R[]>;
	readonly _statePrivate: PrimitiveAtom<"opening" | "open" | "closed">;
	// Controllable connection-state (stable atoms, so an effect that
	// `get()`s `connectionState` observes flips). Tests drive
	// reload-on-break by setting `_connectionStatePrivate`.
	readonly _connectionStatePrivate: PrimitiveAtom<"idle" | "connected" | "broken">;
	readonly _subscriptionBreakReasonPrivate: PrimitiveAtom<string | null>;
	readonly _committedPrivate: PrimitiveAtom<boolean>;
	readonly #events: Atom<readonly R[]>;
	readonly #state: Atom<"opening" | "open" | "closed">;
	readonly #connectionState: Atom<"idle" | "connected" | "broken">;
	readonly #subscriptionBreakReason: Atom<string | null>;
	readonly #committed: Atom<boolean>;
	readonly #handle: CallbackHandleStub<R>;

	constructor(
		opts: {
			readonly offset?: number;
			readonly events?: readonly R[];
			readonly state?: "opening" | "open" | "closed";
			readonly connectionState?: "idle" | "connected" | "broken";
			readonly subscriptionBreakReason?: string | null;
		} = {},
	) {
		super();
		this.#offset = opts.offset ?? 0;
		this._eventsPrivate = atom<readonly R[]>(opts.events ?? []);
		this._statePrivate = atom<"opening" | "open" | "closed">(opts.state ?? "open");
		this._connectionStatePrivate = atom<"idle" | "connected" | "broken">(
			opts.connectionState ?? "connected",
		);
		this._subscriptionBreakReasonPrivate = atom<string | null>(
			opts.subscriptionBreakReason ?? null,
		);
		this._committedPrivate = atom(true);
		this.#events = atom((get) => get(this._eventsPrivate));
		this.#state = atom((get) => get(this._statePrivate));
		this.#connectionState = atom((get) => get(this._connectionStatePrivate));
		this.#subscriptionBreakReason = atom((get) => get(this._subscriptionBreakReasonPrivate));
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
	override get connectionState(): Atom<"idle" | "connected" | "broken"> {
		return this.#connectionState;
	}
	override get subscriptionBreakReason(): Atom<string | null> {
		return this.#subscriptionBreakReason;
	}
	override get committed(): Atom<boolean> {
		return this.#committed;
	}
	override get handle(): CallbackHandleStub<R> {
		return this.#handle;
	}

	override sliced(n: number): CallbackInboxStub<R> {
		// Sliced view: same underlying events private atom, with the
		// `offset` cursor advanced by `n`. Tests that want to observe
		// the slice-vs-original distinction can compare offsets.
		const slicedEvents = atom<readonly R[]>((get) => get(this._eventsPrivate).slice(n));
		const sliced = new TestCallbackInboxStub<R>({
			offset: this.#offset + n,
			state: "open",
		});
		// Replace the sliced view's events atom with the
		// after-the-cursor view, and share the underlying state /
		// connection-state private atoms so the slice reflects the
		// same lifecycle as the original. Cast for the override:
		// tests don't write through the slice's events atom directly.
		(sliced as unknown as { _eventsPrivate: Atom<readonly R[]> })._eventsPrivate = slicedEvents;
		(
			sliced as unknown as { _statePrivate: PrimitiveAtom<"opening" | "open" | "closed"> }
		)._statePrivate = this._statePrivate;
		(
			sliced as unknown as {
				_connectionStatePrivate: PrimitiveAtom<"idle" | "connected" | "broken">;
			}
		)._connectionStatePrivate = this._connectionStatePrivate;
		return sliced;
	}
}

// --- FxtrSessionAtom with controllable ctx ---------------------------

export interface MockInvokeCall {
	readonly stub: ServiceRefStub<unknown>;
	readonly method: string;
	readonly args: ReadonlyArray<unknown>;
	readonly kwargs?: Record<string, unknown>;
}

export interface MockSessionAtomOptions<S> {
	readonly initial: S;
	/**
	 * Each call to `ctx.allocateLoad(ref)` returns the next stub
	 * popped from this queue. Tests typically push one
	 * pre-controlled `TestOpResultStub` per expected allocate
	 * call. Throws if the queue runs dry.
	 */
	readonly loadQueue?: Array<TestOpResultStub<unknown>>;
	readonly entityMetadataQueue?: Array<TestOpResultStub<EntityMetadata>>;
	readonly serviceMetadataQueue?: Array<TestOpResultStub<ServiceMetadata>>;
	/**
	 * Each call to `ctx.allocateInvoke(...)` returns the next stub
	 * popped from this queue. The matching `MockInvokeCall`
	 * describing the call (stub, method, args, kwargs) is pushed
	 * into `invokeCalls` so the test can assert what was invoked.
	 */
	readonly invokeQueue?: Array<TestOpResultStub<unknown>>;
	/**
	 * Captured side-channel: tests inspect this to assert the
	 * sequence and shape of allocateInvoke calls observed.
	 */
	readonly invokeCalls?: Array<MockInvokeCall>;
}

/**
 * Build a `FxtrSessionAtom<S>` backed by a primitive jotai atom
 * with the ctx-passing contract preserved. Function-form
 * updaters receive a `FxtrSessionUpdateCtx` whose allocate
 * methods dispense queued `TestOpResultStub`s.
 *
 * Returns the public `FxtrSessionAtom` plus the underlying
 * primitive so tests can read its value directly.
 */
export function makeMockSessionAtom<S>(opts: MockSessionAtomOptions<S>): {
	readonly atom: FxtrSessionAtom<S>;
	readonly privateAtom: PrimitiveAtom<S>;
} {
	const privateAtom = atom<S>(opts.initial);
	const loadQueue = opts.loadQueue ?? [];
	const entityMetadataQueue = opts.entityMetadataQueue ?? [];
	const serviceMetadataQueue = opts.serviceMetadataQueue ?? [];
	const invokeQueue = opts.invokeQueue ?? [];
	const invokeCalls = opts.invokeCalls;

	const ctx: FxtrSessionUpdateCtx = {
		allocateLoad: <T>(_ref: EntityRefStub<T>) => {
			const stub = loadQueue.shift();
			if (stub === undefined) {
				throw new Error("makeMockSessionAtom: load queue exhausted");
			}
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
			// Build the captured call record without an explicit
			// `kwargs: undefined` when no kwargs were supplied;
			// matches `exactOptionalPropertyTypes`.
			const call: MockInvokeCall =
				kwargs === undefined ? { stub: svc, method, args } : { stub: svc, method, args, kwargs };
			invokeCalls?.push(call);
			return next as unknown as OpResultStub<unknown>;
			// biome-ignore lint/suspicious/noExplicitAny: matches `FxtrSessionUpdateCtx.allocateInvoke`'s overloaded signature
		}) as any,
		allocateGetEntityMetadata: (_ref: EntityRefStub) => {
			const stub = entityMetadataQueue.shift();
			if (stub === undefined) {
				throw new Error("makeMockSessionAtom: entity-metadata queue exhausted");
			}
			return stub;
		},
		allocateGetServiceMetadata: (_ref: ServiceRefStub<unknown>) => {
			const stub = serviceMetadataQueue.shift();
			if (stub === undefined) {
				throw new Error("makeMockSessionAtom: service-metadata queue exhausted");
			}
			return stub;
		},
		allocateCallback: () => {
			throw new NotImplementedError("makeMockSessionAtom: allocateCallback not wired");
		},
		// The session backref isn't used by the dispatch state
		// machines themselves; throw if anything reaches for it.
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
