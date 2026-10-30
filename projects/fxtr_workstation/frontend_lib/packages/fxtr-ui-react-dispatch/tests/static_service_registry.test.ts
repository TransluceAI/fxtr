/**
 * Mirror of `static_entity_registry.test.ts` for the
 * service-side state machine: `init → loading-type-metadata →
 * resolved | errored`. Same direct-invocation strategy.
 *
 * Fallback components are sourced from the dispatch key at
 * production time; in these direct-effect-fn tests we pass a
 * `fallbacks` bundle into the factory ourselves.
 */

import type { ServiceMetadata, ServiceRefStub } from "@fxtr-ui/core";
import { type WritableAtom, atom, createStore } from "jotai";
import { describe, expect, it } from "vitest";
import type { ServiceKeyFallbacks } from "../src/fallbacks";
import type { DispatchedComponent, ErrorFallbackComponent } from "../src/registry_types";
import {
	type StaticServiceDispatchState,
	staticServiceTypeDispatch,
} from "../src/static_service_registry";
import { TestOpResultStub, TestServiceRefStub, makeMockSessionAtom } from "./_mocks";

const LoadingFallback = function LoadingFallback(_p: { stub: unknown }) {
	return null;
} as unknown as DispatchedComponent<ServiceRefStub, object>;
const NoMatchFallback = function NoMatchFallback(_p: { stub: unknown }) {
	return null;
} as unknown as DispatchedComponent<ServiceRefStub, object>;
const EchoCard = function EchoCard(_p: { stub: unknown }) {
	return null;
} as unknown as DispatchedComponent<ServiceRefStub, object>;
const ErrorFallback = function ErrorFallback(_p: {
	stub: unknown;
	error: unknown;
}) {
	return null;
} as unknown as ErrorFallbackComponent<ServiceRefStub, object>;

const defaultServiceFallbacks: ServiceKeyFallbacks<object> = {
	Loading: LoadingFallback,
	NoMatch: NoMatchFallback,
	Error: ErrorFallback,
};

function setup(opts: {
	readonly metaStub: TestOpResultStub<ServiceMetadata>;
	readonly lookupMap?: ReadonlyMap<string, DispatchedComponent<ServiceRefStub, object>>;
}) {
	const stub = new TestServiceRefStub("s-test");
	const stateAtomBundle = makeMockSessionAtom<StaticServiceDispatchState>({
		initial: { phase: "init" },
		serviceMetadataQueue: [opts.metaStub],
	});

	// Box the component reference — see static_entity_registry.test
	// for the rationale; same jotai overload trap.
	const componentBoxPrivate = atom<{
		readonly Component: DispatchedComponent<ServiceRefStub, object>;
	}>({ Component: LoadingFallback });
	const componentAtom: WritableAtom<
		DispatchedComponent<ServiceRefStub, object>,
		[DispatchedComponent<ServiceRefStub, object>],
		void
	> = atom(
		(get) => get(componentBoxPrivate).Component,
		(_get, set, v: DispatchedComponent<ServiceRefStub, object>) =>
			set(componentBoxPrivate, { Component: v }),
	);
	const progressMessageAtom = atom<string>("");

	const entry = staticServiceTypeDispatch<object>({
		lookupMap: opts.lookupMap ?? new Map<string, DispatchedComponent<ServiceRefStub, object>>(),
	});

	const innerEffect = entry.effectFn({
		stub,
		stateAtom: stateAtomBundle.atom,
		componentAtom,
		progressMessageAtom,
		fallbacks: defaultServiceFallbacks,
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
		readState: () => store.get(stateAtomBundle.atom),
		readComponent: () => store.get(componentAtom),
		readProgress: () => store.get(progressMessageAtom),
	};
}

describe("staticServiceTypeDispatch", () => {
	it("init → loading-type-metadata allocates a metadata query", () => {
		const metaStub = new TestOpResultStub<ServiceMetadata>({ status: "pending" });
		const { run, readState, readProgress } = setup({ metaStub });

		run();

		const state = readState();
		expect(state.phase).toBe("loading-type-metadata");
		if (state.phase !== "loading-type-metadata") return;
		expect(state.meta).toBe(metaStub);
		expect(readProgress()).toBe("resolving renderer…");
	});

	it("loading-type-metadata → resolved on fill", () => {
		const metaStub = new TestOpResultStub<ServiceMetadata>({ status: "pending" });
		const lookupMap = new Map<string, DispatchedComponent<ServiceRefStub, object>>([
			["echo-type-id", EchoCard],
		]);
		const { run, store, readState, readComponent, readProgress } = setup({ metaStub, lookupMap });

		run();
		store.set(metaStub._resultPrivate, {
			status: "filled",
			value: { type_id_string: "echo-type-id" },
		});
		run();

		const state = readState();
		expect(state.phase).toBe("resolved");
		if (state.phase !== "resolved") return;
		expect(state.typeIdString).toBe("echo-type-id");
		expect(readComponent()).toBe(EchoCard);
		expect(readProgress()).toBe("");
	});

	it("falls back to NoMatchFallback on unknown type-id", () => {
		const metaStub = new TestOpResultStub<ServiceMetadata>({ status: "pending" });
		const { run, store, readComponent } = setup({ metaStub });

		run();
		store.set(metaStub._resultPrivate, {
			status: "filled",
			value: { type_id_string: "unknown" },
		});
		run();

		expect(readComponent()).toBe(NoMatchFallback);
	});

	it("loading-type-metadata → errored when metadata errors", () => {
		const metaStub = new TestOpResultStub<ServiceMetadata>({ status: "pending" });
		const { run, store, readState } = setup({ metaStub });

		run();
		store.set(metaStub._resultPrivate, { status: "errored", message: "no-meta" });
		run();

		const state = readState();
		expect(state.phase).toBe("errored");
		if (state.phase !== "errored") return;
		expect(state.message).toBe("no-meta");
	});

	it("resolved phase re-syncs componentAtom on reattach", () => {
		const metaStub = new TestOpResultStub<ServiceMetadata>({ status: "pending" });
		const lookupMap = new Map<string, DispatchedComponent<ServiceRefStub, object>>([
			["echo-type-id", EchoCard],
		]);
		const helpers = setup({ metaStub, lookupMap });

		// Reattach-style direct write to terminal state.
		helpers.store.set(helpers.stateAtomBundle.privateAtom, {
			phase: "resolved",
			typeIdString: "echo-type-id",
		});
		expect(helpers.readComponent()).toBe(LoadingFallback);
		helpers.run();
		expect(helpers.readComponent()).toBe(EchoCard);
	});
});
