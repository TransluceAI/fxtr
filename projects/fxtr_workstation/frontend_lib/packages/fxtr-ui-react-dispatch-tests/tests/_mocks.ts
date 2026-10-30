/**
 * Test mocks for `@fxtr-ui/react-dispatch` React-layer tests.
 *
 * Shape mirrors `@fxtr-ui/react-tests/tests/_mocks.ts` — minimal
 * Session impl plus stub helpers — narrowed to what the
 * RenderDispatch tests need.
 */

import {
	EntityRefStub,
	type FxtrSessionAtom,
	type FxtrSessionUpdateCtx,
	type FxtrUpdate,
	NotImplementedError,
	ServiceRefStub,
	type Session,
	type UserState,
	type UserStateWriteOptions,
} from "@fxtr-ui/core";
import { type PrimitiveAtom, atom, createStore } from "jotai";

export function makeMockSession(
	opts: {
		sessionId?: string;
		/**
		 * Initial lifecycle. Defaults to `"connected"`. Tests that
		 * want to keep the dynamic dispatcher's reload subscription
		 * from firing on construct can pass `"connecting"` (or
		 * anything other than `"connected"`) so the effect's
		 * lifecycle gate short-circuits.
		 */
		lifecycle?: "connecting" | "connected" | "stale" | "closed";
	} = {},
): {
	readonly session: Session;
	readonly userStatePrivate: PrimitiveAtom<UserState | null>;
} {
	const store = createStore();
	const userStatePrivate = atom<UserState | null>(null);
	const initialLifecycle = opts.lifecycle ?? "connected";

	// biome-ignore lint/style/useConst: closure-capture forward declaration
	let sessionRef: Session;
	const ctx: FxtrSessionUpdateCtx = {
		allocateLoad: () => {
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
		allocateCallback: () => {
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
		lifecycle: atom(() => initialLifecycle),
		userState,
		detach: async () => {},
		terminate: async () => {},
	};
	sessionRef = session;

	return { session, userStatePrivate };
}

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
