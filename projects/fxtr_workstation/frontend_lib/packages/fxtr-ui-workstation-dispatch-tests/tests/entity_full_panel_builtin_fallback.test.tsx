/**
 * React-render tests for
 * `EntityFullPanelBuiltinFallback` — the FullPanel key's
 * `Builtin` slot, rendered by `staticEntityTypeDispatch`'s
 * `resolved-builtin` arm for stubs with shortIds starting
 * with `c` / `!` / `.`.
 *
 * The fallback issues a `ctx.allocateGetEntityMetadata` call
 * (no load) and surfaces the populated fields via the shared
 * `MetadataFields` component — most importantly,
 * `context_data` for c-tag nodes renders as an inline
 * `<RefLink>` the user can click.
 *
 * Covered:
 *   - Metadata is allocated; no load is allocated.
 *   - Pending → filled with `context_data` ref renders an
 *     inline `<RefLink>` (visible as the linked stub's
 *     shortId in the rendered tree).
 *   - The kind label matches the stub's tag (`c…` → "context
 *     provenance node", `u…` → "untracked provenance node",
 *     `!…` → "builtin", `.…` → "anonymous node").
 *
 * @vitest-environment jsdom
 */

import {
	type EntityMetadata,
	EntityRefStub,
	type FxtrSessionAtom,
	type FxtrSessionUpdateCtx,
	type FxtrUpdate,
	NotImplementedError,
	OpResultStub,
	type ResultStatus,
	type UserStateWriteOptions,
} from "@fxtr-ui/core";
import {
	DispatcherProvider,
	createDispatchRegistry,
	createStaticDispatcher,
} from "@fxtr-ui/react-dispatch";
import { EntityFullPanelBuiltinFallback } from "@fxtr-ui/workstation-dispatch";
import { cleanup, render, screen } from "@testing-library/react";
import { type Atom, type PrimitiveAtom, atom, createStore } from "jotai";
import { Provider as JotaiProvider } from "jotai";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

// ----- Test stubs -------------------------------------------------

class TestEntityStub extends EntityRefStub {
	constructor(public readonly shortId: string) {
		super();
	}
}

class TestOpResultStub<T> extends OpResultStub<T> {
	readonly _resultPrivate: PrimitiveAtom<ResultStatus<T>>;
	readonly #result: Atom<ResultStatus<T>>;
	readonly #committed: Atom<boolean>;
	readonly #done: Atom<boolean>;
	constructor(initial: ResultStatus<T> = { status: "pending" }) {
		super();
		this._resultPrivate = atom<ResultStatus<T>>(initial);
		this.#result = atom((get) => get(this._resultPrivate));
		this.#committed = atom(false);
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

// ----- Mock FxtrSessionAtom that records ctx calls ---------------

interface MockSessionAtomOpts {
	readonly metadataQueue: Array<TestOpResultStub<EntityMetadata>>;
	readonly calls: {
		loadCount: number;
		metadataCount: number;
	};
}

function mockOwnStorage<S>(initial: S, opts: MockSessionAtomOpts): FxtrSessionAtom<S> {
	const privateAtom = atom<S>(initial);
	const ctx: FxtrSessionUpdateCtx = {
		allocateLoad: () => {
			opts.calls.loadCount += 1;
			throw new Error("allocateLoad should not be called by the builtin fallback");
		},
		allocateInvoke: (() => {
			throw new NotImplementedError("invoke not wired");
		}) as FxtrSessionUpdateCtx["allocateInvoke"],
		allocateGetEntityMetadata: () => {
			opts.calls.metadataCount += 1;
			const next = opts.metadataQueue.shift();
			if (next === undefined) throw new Error("metadata queue exhausted");
			return next;
		},
		allocateGetServiceMetadata: () => {
			throw new NotImplementedError("service-metadata not wired");
		},
		allocateCallback: () => {
			throw new NotImplementedError("callback not wired");
		},
		get session(): never {
			throw new NotImplementedError("session backref not wired");
		},
	};
	return atom(
		(get) => get(privateAtom),
		(get, set, update: FxtrUpdate<S>, _opts?: UserStateWriteOptions) => {
			const prev = get(privateAtom);
			const next =
				typeof update === "function"
					? (update as (p: S, c: FxtrSessionUpdateCtx) => S)(prev, ctx)
					: update;
			set(privateAtom, next);
		},
	);
}

// ----- Dispatcher provider wrapper -------------------------------
//
// The metadata fields' ref-typed values render via `<RefLink>`,
// which dispatches through `<RenderDispatch>` — that requires a
// `<DispatcherProvider>` in scope. We use an empty static
// dispatcher so all dispatched refs land on the canonical
// no-match fallback (which renders the stub's shortId, so the
// test can still assert on the linked-to id).

function mockSession(): Parameters<typeof createStaticDispatcher>[0]["session"] {
	const lifecycle = atom<"connecting" | "connected" | "stale" | "closed">("connected");
	const userState = atom<unknown>(null);
	return {
		sessionId: "test-session",
		lifecycle,
		userState,
		store: createStore(),
		detach: async () => {},
	} as unknown as Parameters<typeof createStaticDispatcher>[0]["session"];
}

function withDispatcher(node: ReactNode): ReactNode {
	const dispatcher = createStaticDispatcher({
		session: mockSession(),
		registry: createDispatchRegistry({ entityEntries: [], serviceEntries: [] }),
	});
	return <DispatcherProvider dispatcher={dispatcher}>{node}</DispatcherProvider>;
}

// ----- Tests ------------------------------------------------------

afterEach(() => {
	cleanup();
});

describe("EntityFullPanelBuiltinFallback", () => {
	it("allocates a metadata call (no load) and surfaces context_data as a RefLink", () => {
		const stub = new TestEntityStub("cAAAAAAAAA");
		const ctxDataStub = new TestEntityStub("dCONTEXTDATA");
		const metaStub = new TestOpResultStub<EntityMetadata>({
			status: "filled",
			value: { id_string: "cAAAAAAAAA", context_data: ctxDataStub },
		});
		const opts: MockSessionAtomOpts = {
			metadataQueue: [metaStub],
			calls: { loadCount: 0, metadataCount: 0 },
		};
		const ownStorage = mockOwnStorage<unknown>(null, opts);

		const store = createStore();
		render(
			<JotaiProvider store={store}>
				{withDispatcher(<EntityFullPanelBuiltinFallback stub={stub} ownStorage={ownStorage} />)}
			</JotaiProvider>,
		);

		expect(opts.calls.loadCount).toBe(0);
		expect(opts.calls.metadataCount).toBe(1);

		// The kind label matches the c-tag.
		expect(screen.getByText("context provenance node")).toBeTruthy();
		// The stub's shortId appears in the header AND in the
		// `id_string` metadata row (one mono `<code>`).
		expect(screen.getAllByText("cAAAAAAAAA").length).toBeGreaterThanOrEqual(2);
		// The context_data RefLink renders the linked stub's shortId.
		expect(screen.getByText("dCONTEXTDATA")).toBeTruthy();
		// And the row label "context_data" is present.
		expect(screen.getByText("context_data")).toBeTruthy();
	});

	it("renders 'pending…' while the metadata stub is unfilled", () => {
		const stub = new TestEntityStub("!some-builtin");
		const metaStub = new TestOpResultStub<EntityMetadata>({ status: "pending" });
		const opts: MockSessionAtomOpts = {
			metadataQueue: [metaStub],
			calls: { loadCount: 0, metadataCount: 0 },
		};
		const ownStorage = mockOwnStorage<unknown>(null, opts);
		const store = createStore();
		render(
			<JotaiProvider store={store}>
				{withDispatcher(<EntityFullPanelBuiltinFallback stub={stub} ownStorage={ownStorage} />)}
			</JotaiProvider>,
		);
		expect(opts.calls.loadCount).toBe(0);
		expect(opts.calls.metadataCount).toBe(1);
		expect(screen.getByText("builtin")).toBeTruthy();
		expect(screen.getByText("pending…")).toBeTruthy();
	});

	it("uses the anonymous-node kind label for a `.`-tagged stub", () => {
		const stub = new TestEntityStub(".");
		const metaStub = new TestOpResultStub<EntityMetadata>({
			status: "filled",
			value: { id_string: "." },
		});
		const opts: MockSessionAtomOpts = {
			metadataQueue: [metaStub],
			calls: { loadCount: 0, metadataCount: 0 },
		};
		const ownStorage = mockOwnStorage<unknown>(null, opts);
		const store = createStore();
		render(
			<JotaiProvider store={store}>
				{withDispatcher(<EntityFullPanelBuiltinFallback stub={stub} ownStorage={ownStorage} />)}
			</JotaiProvider>,
		);
		expect(screen.getByText("anonymous node")).toBeTruthy();
		expect(opts.calls.loadCount).toBe(0);
	});
});
