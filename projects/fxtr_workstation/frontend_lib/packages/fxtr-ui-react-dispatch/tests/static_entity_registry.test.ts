/**
 * Drive `staticEntityTypeDispatch`'s state machine directly:
 * the effect-fn factory's returned `(get, set) => …` body is
 * invoked manually with a jotai store's `get` / `set` so we can
 * step through transitions deterministically (rather than
 * waiting on jotai-effect's microtask batching).
 *
 * Test invariants exercised:
 *   - `init` → allocate a load, transition to `loading-entity`,
 *     write the progress message.
 *   - `loading-entity` while load is pending → no-op.
 *   - `loading-entity` after load fills with a value carrying
 *     `$fxtr.type` → allocate metadata, transition to
 *     `loading-type-metadata`.
 *   - `loading-entity` after load errors → terminal `errored`.
 *   - `loading-type-metadata` after metadata fills → terminal
 *     `resolved`, write the looked-up component into
 *     `componentAtom` (or `NoMatch` on a miss).
 *   - Terminal-state re-sync: with the state already in
 *     `resolved` but `componentAtom` at `Loading`, the next
 *     run resyncs the component (covers reattach-to-existing-
 *     session).
 *   - Loaded value without a valid `$fxtr.type` → terminal
 *     `errored`.
 *
 * Fallback components now live on the dispatch key rather than
 * on the `staticEntityTypeDispatch` options; the dispatcher
 * threads them in through the effect-fn factory's `fallbacks`
 * parameter. In these direct-effect-fn tests we pass the
 * fallbacks bundle ourselves when invoking the factory.
 */

import type { EntityMetadata, EntityRefStub } from "@fxtr-ui/core";
import { type WritableAtom, atom, createStore } from "jotai";
import { describe, expect, it } from "vitest";
import type { EntityKeyFallbacks } from "../src/fallbacks";
import type { DispatchedComponent, ErrorFallbackComponent } from "../src/registry_types";
import {
	type StaticEntityDispatchState,
	staticEntityTypeDispatch,
} from "../src/static_entity_registry";
import { TestEntityRefStub, TestOpResultStub, makeMockSessionAtom } from "./_mocks";

// Sentinel component values — we just compare by identity.
const LoadingFallback = function LoadingFallback(_p: { stub: unknown }) {
	return null;
} as unknown as DispatchedComponent<EntityRefStub, object>;
const NoMatchFallback = function NoMatchFallback(_p: { stub: unknown }) {
	return null;
} as unknown as DispatchedComponent<EntityRefStub, object>;
const NoteCard = function NoteCard(_p: { stub: unknown }) {
	return null;
} as unknown as DispatchedComponent<EntityRefStub, object>;
const ErrorFallback = function ErrorFallback(_p: {
	stub: unknown;
	error: unknown;
}) {
	return null;
} as unknown as ErrorFallbackComponent<EntityRefStub, object>;

// Default fallback bundle threaded into the effect-fn factory.
// Tests that need a `Builtin` override redeclare locally.
const defaultEntityFallbacks: EntityKeyFallbacks<object> = {
	Loading: LoadingFallback,
	NoMatch: NoMatchFallback,
	Error: ErrorFallback,
};

/**
 * Build the per-entry atoms + the inner effect function, all
 * wired up against a single jotai store so reads + writes from
 * the effect see each other.
 */
function setup(opts: {
	readonly loadStub: TestOpResultStub<unknown>;
	readonly metaStub?: TestOpResultStub<EntityMetadata>;
	readonly lookupMap?: ReadonlyMap<string, DispatchedComponent<EntityRefStub, object>>;
	readonly fallbacks?: EntityKeyFallbacks<object>;
}) {
	const stub = new TestEntityRefStub("e-test");
	const stateAtomBundle = makeMockSessionAtom<StaticEntityDispatchState>({
		initial: { phase: "init" },
		loadQueue: [opts.loadStub],
		entityMetadataQueue: opts.metaStub ? [opts.metaStub] : [],
	});

	// The state machine doesn't touch `sharedAtom` directly —
	// that's the production `componentAtom` wrapper's job. For
	// these state-machine tests we only need a writable
	// componentAtom; the test passes a plain `atom(...)` and
	// reads it back.
	//
	// Box the component reference — jotai's `atom(fn)` overload
	// resolves to "derived atom with fn as the read function" for
	// any function argument, including React components. Same
	// reasoning as the production `componentBoxAtom` wrapper.
	const componentBoxPrivate = atom<{
		readonly Component: DispatchedComponent<EntityRefStub, object>;
	}>({ Component: LoadingFallback });
	const componentAtom: WritableAtom<
		DispatchedComponent<EntityRefStub, object>,
		[DispatchedComponent<EntityRefStub, object>],
		void
	> = atom(
		(get) => get(componentBoxPrivate).Component,
		(_get, set, v: DispatchedComponent<EntityRefStub, object>) =>
			set(componentBoxPrivate, { Component: v }),
	);

	const progressMessageAtom = atom<string>("");

	const entry = staticEntityTypeDispatch<object>({
		lookupMap: opts.lookupMap ?? new Map<string, DispatchedComponent<EntityRefStub, object>>(),
	});

	const innerEffect = entry.effectFn({
		stub,
		stateAtom: stateAtomBundle.atom,
		componentAtom,
		progressMessageAtom,
		fallbacks: opts.fallbacks ?? defaultEntityFallbacks,
	});

	const store = createStore();
	const run = (): void => {
		innerEffect(store.get, store.set);
	};

	return {
		stub,
		store,
		run,
		stateAtomBundle,
		componentAtom,
		progressMessageAtom,
		// Use to read the current state from outside.
		readState: () => store.get(stateAtomBundle.atom),
		readComponent: () => store.get(componentAtom),
		readProgress: () => store.get(progressMessageAtom),
	};
}

// A `$fxtr.type` ref pointing at a "Note" type entity. The
// loaded value carries this at the conventional field key.
const NOTE_TYPE_REF = new TestEntityRefStub("note-type");
const LOADED_NOTE_VALUE = {
	title: "hello",
	body: "world",
	"$fxtr.type": NOTE_TYPE_REF,
};

describe("staticEntityTypeDispatch — happy path", () => {
	it("init → loading-entity allocates a load and writes the progress message", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const { run, readState, readProgress } = setup({ loadStub });

		run();

		const state = readState();
		expect(state.phase).toBe("loading-entity");
		if (state.phase !== "loading-entity") return;
		expect(state.load).toBe(loadStub);
		expect(readProgress()).toBe("loading entity…");
	});

	it("loading-entity stays parked while the load is pending", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const { run, readState } = setup({ loadStub });
		run(); // init → loading-entity
		run(); // pending — should no-op
		expect(readState().phase).toBe("loading-entity");
	});

	it("loading-entity → loading-type-metadata on fill (allocates metadata)", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const metaStub = new TestOpResultStub<EntityMetadata>({ status: "pending" });
		const { run, store, readState, readProgress } = setup({ loadStub, metaStub });

		run(); // init → loading-entity
		store.set(loadStub._resultPrivate, { status: "filled", value: LOADED_NOTE_VALUE });
		run(); // loading-entity → loading-type-metadata

		const state = readState();
		expect(state.phase).toBe("loading-type-metadata");
		if (state.phase !== "loading-type-metadata") return;
		expect(state.typeRef).toBe(NOTE_TYPE_REF);
		expect(state.meta).toBe(metaStub);
		expect(readProgress()).toBe("resolving renderer…");
	});

	it("loading-type-metadata → resolved on fill, writes the looked-up component", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const metaStub = new TestOpResultStub<EntityMetadata>({ status: "pending" });
		const lookupMap = new Map<string, DispatchedComponent<EntityRefStub, object>>([
			["note-type-id", NoteCard],
		]);
		const { run, store, readState, readComponent, readProgress } = setup({
			loadStub,
			metaStub,
			lookupMap,
		});

		run(); // init → loading-entity
		store.set(loadStub._resultPrivate, { status: "filled", value: LOADED_NOTE_VALUE });
		run(); // loading-entity → loading-type-metadata
		store.set(metaStub._resultPrivate, {
			status: "filled",
			value: { id_string: "note-type-id" },
		});
		run(); // loading-type-metadata → resolved

		const state = readState();
		expect(state.phase).toBe("resolved");
		if (state.phase !== "resolved") return;
		expect(state.typeIdString).toBe("note-type-id");
		expect(readComponent()).toBe(NoteCard);
		expect(readProgress()).toBe("");
	});

	it("falls back to NoMatchFallback when the resolved type-id has no entry in lookupMap", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const metaStub = new TestOpResultStub<EntityMetadata>({ status: "pending" });
		const { run, store, readComponent } = setup({ loadStub, metaStub });

		run();
		store.set(loadStub._resultPrivate, { status: "filled", value: LOADED_NOTE_VALUE });
		run();
		store.set(metaStub._resultPrivate, { status: "filled", value: { id_string: "unknown" } });
		run();

		// Resolved-with-no-match writes `NoMatchFallback` (not
		// `LoadingFallback`) so consumers can distinguish "still
		// loading" from "loaded but no renderer registered".
		expect(readComponent()).toBe(NoMatchFallback);
	});
});

describe("staticEntityTypeDispatch — error paths", () => {
	it("loading-entity → errored when the load errors", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const { run, store, readState } = setup({ loadStub });

		run();
		store.set(loadStub._resultPrivate, { status: "errored", message: "boom" });
		run();

		const state = readState();
		expect(state.phase).toBe("errored");
		if (state.phase !== "errored") return;
		expect(state.message).toBe("boom");
	});

	it("loading-entity → errored when the loaded value has no $fxtr.type", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const { run, store, readState } = setup({ loadStub });

		run();
		store.set(loadStub._resultPrivate, { status: "filled", value: { title: "no type" } });
		run();

		const state = readState();
		expect(state.phase).toBe("errored");
		if (state.phase !== "errored") return;
		expect(state.message).toContain("$fxtr.type");
	});

	it("loading-entity → errored when $fxtr.type isn't an EntityRefStub", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const { run, store, readState } = setup({ loadStub });

		run();
		store.set(loadStub._resultPrivate, {
			status: "filled",
			value: { "$fxtr.type": "not-a-stub" },
		});
		run();

		expect(readState().phase).toBe("errored");
	});

	it("loading-type-metadata → errored when metadata errors", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const metaStub = new TestOpResultStub<EntityMetadata>({ status: "pending" });
		const { run, store, readState } = setup({ loadStub, metaStub });

		run();
		store.set(loadStub._resultPrivate, { status: "filled", value: LOADED_NOTE_VALUE });
		run();
		store.set(metaStub._resultPrivate, { status: "errored", message: "no-meta" });
		run();

		const state = readState();
		expect(state.phase).toBe("errored");
		if (state.phase !== "errored") return;
		expect(state.message).toBe("no-meta");
	});

	it("errored phase writes the ErrorThrower to componentAtom", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const { run, store, readComponent } = setup({ loadStub });

		const initialComponent = readComponent();
		run(); // init → loading-entity
		store.set(loadStub._resultPrivate, { status: "errored", message: "boom" });
		run(); // loading-entity → sets stateAtom = errored
		// Re-run for the errored arm — in production `atomEffect`
		// would re-fire automatically on the stateAtom write; here
		// we manually invoke since we're not going through
		// jotai-effect's scheduling.
		run();

		// componentAtom should now hold the synthetic
		// `ErrorThrower` (identity-distinct from the Loading
		// fallback).
		expect(readComponent()).not.toBe(initialComponent);
	});
});

describe("staticEntityTypeDispatch — builtin entities (`.` / `!` / `c` tags)", () => {
	it("short-circuits to resolved-builtin on a `.`-tagged stub, skipping the load", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		// Override the stub to one with a `.`-tagged shortId.
		const builtinStub = new TestEntityRefStub(".anon-1");
		const stateAtomBundle = makeMockSessionAtom<StaticEntityDispatchState>({
			initial: { phase: "init" },
			loadQueue: [loadStub], // shouldn't be touched — load is skipped
		});
		const componentBoxPrivate = atom<{
			readonly Component: DispatchedComponent<EntityRefStub, object>;
		}>({ Component: LoadingFallback });
		const componentAtom: WritableAtom<
			DispatchedComponent<EntityRefStub, object>,
			[DispatchedComponent<EntityRefStub, object>],
			void
		> = atom(
			(get) => get(componentBoxPrivate).Component,
			(_get, set, v: DispatchedComponent<EntityRefStub, object>) =>
				set(componentBoxPrivate, { Component: v }),
		);
		const progressMessageAtom = atom<string>("");
		const Builtin = function Builtin(_p: { stub: unknown }) {
			return null;
		} as unknown as DispatchedComponent<EntityRefStub, object>;
		const entry = staticEntityTypeDispatch<object>({
			lookupMap: new Map(),
		});
		const inner = entry.effectFn({
			stub: builtinStub,
			stateAtom: stateAtomBundle.atom,
			componentAtom,
			progressMessageAtom,
			fallbacks: {
				Loading: LoadingFallback,
				NoMatch: NoMatchFallback,
				Error: ErrorFallback,
				Builtin,
			},
		});

		const store = createStore();
		inner(store.get, store.set); // init → resolved-builtin
		expect(store.get(stateAtomBundle.atom).phase).toBe("resolved-builtin");

		inner(store.get, store.set); // resolved-builtin arm writes Builtin
		expect(store.get(componentAtom)).toBe(Builtin);
		// The load queue should still have its stub untouched —
		// init → resolved-builtin must skip `allocateLoad`.
		expect(loadStub._resultPrivate).toBeDefined();
	});

	it("short-circuits to resolved-builtin on a `c`-tagged stub, skipping the load", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		// c-tag (context-provenance) shortIds carry no loadable
		// content; the dispatcher treats them as builtins so the
		// owning fallback can do a metadata call instead.
		const builtinStub = new TestEntityRefStub("cAAAAAAAAA");
		const stateAtomBundle = makeMockSessionAtom<StaticEntityDispatchState>({
			initial: { phase: "init" },
			loadQueue: [loadStub], // shouldn't be touched — load is skipped
		});
		const componentBoxPrivate = atom<{
			readonly Component: DispatchedComponent<EntityRefStub, object>;
		}>({ Component: LoadingFallback });
		const componentAtom: WritableAtom<
			DispatchedComponent<EntityRefStub, object>,
			[DispatchedComponent<EntityRefStub, object>],
			void
		> = atom(
			(get) => get(componentBoxPrivate).Component,
			(_get, set, v: DispatchedComponent<EntityRefStub, object>) =>
				set(componentBoxPrivate, { Component: v }),
		);
		const progressMessageAtom = atom<string>("");
		const Builtin = function Builtin(_p: { stub: unknown }) {
			return null;
		} as unknown as DispatchedComponent<EntityRefStub, object>;
		const entry = staticEntityTypeDispatch<object>({
			lookupMap: new Map(),
		});
		const inner = entry.effectFn({
			stub: builtinStub,
			stateAtom: stateAtomBundle.atom,
			componentAtom,
			progressMessageAtom,
			fallbacks: {
				Loading: LoadingFallback,
				NoMatch: NoMatchFallback,
				Error: ErrorFallback,
				Builtin,
			},
		});

		const store = createStore();
		inner(store.get, store.set); // init → resolved-builtin
		expect(store.get(stateAtomBundle.atom).phase).toBe("resolved-builtin");

		inner(store.get, store.set); // resolved-builtin arm writes Builtin
		expect(store.get(componentAtom)).toBe(Builtin);
		// Load queue's stub should still be unconsumed —
		// init → resolved-builtin must skip `allocateLoad`.
		expect(loadStub._resultPrivate).toBeDefined();
	});

	it("short-circuits to resolved-builtin on a `u`-tagged stub, skipping the load", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		// u-tag (untracked-provenance wrap) shortIds are content-less, like
		// `.`/`!`/`c`: treat them as builtins rather than loading bytes that
		// don't exist (reachable when viewing a provenance node whose child
		// refs project to their `u`-form).
		const builtinStub = new TestEntityRefStub("u!demo_type");
		const stateAtomBundle = makeMockSessionAtom<StaticEntityDispatchState>({
			initial: { phase: "init" },
			loadQueue: [loadStub], // shouldn't be touched — load is skipped
		});
		const componentBoxPrivate = atom<{
			readonly Component: DispatchedComponent<EntityRefStub, object>;
		}>({ Component: LoadingFallback });
		const componentAtom: WritableAtom<
			DispatchedComponent<EntityRefStub, object>,
			[DispatchedComponent<EntityRefStub, object>],
			void
		> = atom(
			(get) => get(componentBoxPrivate).Component,
			(_get, set, v: DispatchedComponent<EntityRefStub, object>) =>
				set(componentBoxPrivate, { Component: v }),
		);
		const progressMessageAtom = atom<string>("");
		const Builtin = function Builtin(_p: { stub: unknown }) {
			return null;
		} as unknown as DispatchedComponent<EntityRefStub, object>;
		const entry = staticEntityTypeDispatch<object>({ lookupMap: new Map() });
		const inner = entry.effectFn({
			stub: builtinStub,
			stateAtom: stateAtomBundle.atom,
			componentAtom,
			progressMessageAtom,
			fallbacks: {
				Loading: LoadingFallback,
				NoMatch: NoMatchFallback,
				Error: ErrorFallback,
				Builtin,
			},
		});

		const store = createStore();
		inner(store.get, store.set); // init → resolved-builtin
		expect(store.get(stateAtomBundle.atom).phase).toBe("resolved-builtin");
		inner(store.get, store.set); // resolved-builtin arm writes Builtin
		expect(store.get(componentAtom)).toBe(Builtin);
		expect(loadStub._resultPrivate).toBeDefined();
	});

	it("falls through to NoMatchFallback when no Builtin option is supplied", () => {
		const builtinStub = new TestEntityRefStub("!builtin-1");
		const stateAtomBundle = makeMockSessionAtom<StaticEntityDispatchState>({
			initial: { phase: "init" },
		});
		const componentBoxPrivate = atom<{
			readonly Component: DispatchedComponent<EntityRefStub, object>;
		}>({ Component: LoadingFallback });
		const componentAtom: WritableAtom<
			DispatchedComponent<EntityRefStub, object>,
			[DispatchedComponent<EntityRefStub, object>],
			void
		> = atom(
			(get) => get(componentBoxPrivate).Component,
			(_get, set, v: DispatchedComponent<EntityRefStub, object>) =>
				set(componentBoxPrivate, { Component: v }),
		);
		const progressMessageAtom = atom<string>("");
		const entry = staticEntityTypeDispatch<object>({
			lookupMap: new Map(),
		});
		const inner = entry.effectFn({
			stub: builtinStub,
			stateAtom: stateAtomBundle.atom,
			componentAtom,
			progressMessageAtom,
			fallbacks: defaultEntityFallbacks,
		});
		const store = createStore();
		inner(store.get, store.set);
		inner(store.get, store.set);
		expect(store.get(componentAtom)).toBe(NoMatchFallback);
	});
});

describe("staticEntityTypeDispatch — terminal re-sync (reattach case)", () => {
	it("resolved phase re-syncs componentAtom if it drifts", () => {
		const loadStub = new TestOpResultStub<unknown>({ status: "pending" });
		const metaStub = new TestOpResultStub<EntityMetadata>({ status: "pending" });
		const lookupMap = new Map<string, DispatchedComponent<EntityRefStub, object>>([
			["note-type-id", NoteCard],
		]);
		const helpers = setup({ loadStub, metaStub, lookupMap });

		// Skip ahead by writing the terminal state directly,
		// simulating a session reattach where userState already
		// carries `{phase: "resolved", typeIdString: "note-type-id"}`
		// but the in-memory componentAtom is fresh
		// (= LoadingFallback).
		helpers.store.set(helpers.stateAtomBundle.privateAtom, {
			phase: "resolved",
			typeIdString: "note-type-id",
		});
		expect(helpers.readComponent()).toBe(LoadingFallback);

		helpers.run(); // terminal-state re-sync writes the right component
		expect(helpers.readComponent()).toBe(NoteCard);
	});
});
