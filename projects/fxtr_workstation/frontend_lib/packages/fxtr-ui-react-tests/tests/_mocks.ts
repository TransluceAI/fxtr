/**
 * Test-side mocks for `@fxtr-ui/react`'s session + stub abstractions.
 *
 * The React-package tests don't pull in `@fxtr-ui/connector-workstation`
 * — keeping the test surface framework-only avoids dragging in the
 * websocket / wire-codec / reducer machinery just to render a hook.
 * These mocks satisfy the abstract `@fxtr-ui/core` interfaces with
 * minimum runtime behavior:
 *
 * - `makeMockSession()` builds a `Session` whose userState writes
 *   go through a single primitive atom, with the ctx-passing
 *   contract preserved (function-form updaters receive a
 *   `FxtrSessionUpdateCtx`). Slot-allocation methods on the ctx
 *   throw `NotImplementedError` unless the test wires them up
 *   explicitly — useful tests don't need them.
 * - `TestOpResult` is a concrete `OpResultStub` whose `result`
 *   atom is writable through the exposed `_resultPrivate`
 *   primitive so a test can flip pending → filled.
 */

import {
	type CallbackInboxStub,
	type EntityRefStub,
	type FxtrSessionAtom,
	type FxtrSessionUpdateCtx,
	type FxtrUpdate,
	NotImplementedError,
	OpResultStub,
	type ResultStatus,
	type Session,
	type SessionLifecycleState,
	type UserState,
	type UserStateWriteOptions,
} from "@fxtr-ui/core";
import { type Atom, type PrimitiveAtom, atom, createStore } from "jotai";

export interface MockSessionOptions {
	readonly sessionId?: string;
	readonly initial?: UserState | null;
	readonly lifecycle?: SessionLifecycleState;
}

/**
 * The internal handles for a mock session: the underlying writable
 * atoms so tests can mutate state outside the public hook surface.
 */
export interface MockSession {
	readonly session: Session;
	readonly userStatePrivate: PrimitiveAtom<UserState | null>;
	readonly lifecyclePrivate: PrimitiveAtom<SessionLifecycleState>;
}

export function makeMockSession(opts: MockSessionOptions = {}): MockSession {
	const store = createStore();
	const userStatePrivate = atom<UserState | null>(opts.initial ?? null);
	const lifecyclePrivate = atom<SessionLifecycleState>(opts.lifecycle ?? "connected");

	// Forward-declared so the ctx's `get session()` can return the
	// final session reference after both are constructed. Same
	// pattern `WorkstationSession` uses. (`let` is needed for the
	// closure capture; biome flags single-assignment lets, so opt
	// out here.)
	// biome-ignore lint/style/useConst: closure-capture forward declaration
	let sessionRef: Session;

	const ctx: FxtrSessionUpdateCtx = {
		allocateLoad: <T>(_ref: EntityRefStub<T>) => {
			throw new NotImplementedError("mock session: allocateLoad not wired");
		},
		allocateInvoke: () => {
			throw new NotImplementedError("mock session: allocateInvoke not wired");
		},
		allocateGetEntityMetadata: () => {
			throw new NotImplementedError("mock session: allocateGetEntityMetadata not wired");
		},
		allocateGetServiceMetadata: () => {
			throw new NotImplementedError("mock session: allocateGetServiceMetadata not wired");
		},
		allocateCallback: <R>(): CallbackInboxStub<R> => {
			throw new NotImplementedError("mock session: allocateCallback not wired");
		},
		get session() {
			return sessionRef;
		},
	};

	const userState: FxtrSessionAtom<UserState | null> = atom(
		(get) => get(userStatePrivate),
		(get, set, update: FxtrUpdate<UserState | null>, _options?: UserStateWriteOptions) => {
			const prev = get(userStatePrivate);
			const next =
				typeof update === "function"
					? (update as (p: UserState | null, c: FxtrSessionUpdateCtx) => UserState | null)(
							prev,
							ctx,
						)
					: update;
			set(userStatePrivate, next);
		},
	);

	const session: Session = {
		sessionId: opts.sessionId ?? "mock",
		store,
		// Read-only derived; consumers can't write through it.
		lifecycle: atom((get) => get(lifecyclePrivate)),
		userState,
		detach: async () => {},
		terminate: async () => {},
	};
	sessionRef = session;

	return { session, userStatePrivate, lifecyclePrivate };
}

/**
 * Concrete `OpResultStub` whose status is writable through
 * `_resultPrivate`. The public `result` atom is a read-only view —
 * matching the production stub classes' shape.
 */
export class TestOpResult<T = unknown> extends OpResultStub<T> {
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
