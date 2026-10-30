/**
 * Render-layer tests for `<DispatcherProvider>` + `<RenderDispatch>`
 * + the dispatched-component-side hooks.
 *
 * The state-machine behavior of `staticEntityTypeDispatch` /
 * `staticServiceTypeDispatch` is covered by non-React tests
 * under `frontend_lib/packages/fxtr-ui-react-dispatch/tests/`.
 * Here we use *handcrafted* registry entries with effect-fns
 * that write a known component immediately so the React side's
 * behavior (fallback rendering, error boundary, context
 * threading) can be tested without depending on the static
 * helpers.
 *
 * Fallback components (Loading / NoMatch / Error / optional
 * Builtin) live on the `DispatchKey` itself. The handcrafted
 * registry entries here just carry the state-machine recipe
 * (`effectFn` + `initialState` + `initialProgressMessage?`);
 * fallback rendering is driven off `TestKey.fallbacks`.
 *
 * @vitest-environment jsdom
 */

import type { EntityRefStub } from "@fxtr-ui/core";
import {
	type DispatchKeyRegistryEntry,
	type DispatchedComponent,
	DispatcherProvider,
	type ErrorFallbackComponent,
	type NoProps,
	RenderDispatch,
	createDispatchRegistry,
	createStaticDispatcher,
	declareEntityDispatchKey,
	entityEntry,
	useDispatchProgressMessage,
	useDispatchProgressState,
	useDispatchSharedAtom,
} from "@fxtr-ui/react-dispatch";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useAtomValue, useSetAtom } from "jotai";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { TestEntityRefStub, makeMockSession } from "./_mocks";

afterEach(() => {
	cleanup();
});

/**
 * Build a handcrafted entity registry entry whose effect-fn
 * runs `body(get, set)` on mount. Lets each test wire its own
 * state-machine behavior without the load + metadata round
 * trips the static helpers do. Fallbacks now come from the
 * key; the registry entry only carries the state-machine
 * recipe.
 */
function manualEntityEntry<S>(opts: {
	readonly initialState: S;
	readonly body: NonNullable<
		ReturnType<DispatchKeyRegistryEntry<EntityRefStub, NoProps, S>["effectFn"]>
	>;
	readonly initialProgressMessage?: string;
}): DispatchKeyRegistryEntry<EntityRefStub, NoProps, S> {
	const base = {
		initialState: opts.initialState,
		effectFn: () => opts.body,
	};
	if (opts.initialProgressMessage === undefined) return base;
	return { ...base, initialProgressMessage: opts.initialProgressMessage };
}

const Fallback: DispatchedComponent<EntityRefStub, NoProps> = ({ stub }) => (
	<div data-testid="fallback">fallback for {stub.shortId}</div>
);

const NoMatch: DispatchedComponent<EntityRefStub, NoProps> = ({ stub }) => (
	<div data-testid="no-match">no-match for {stub.shortId}</div>
);

const ErrorFallback: ErrorFallbackComponent<EntityRefStub, NoProps> = ({ stub, error }) => (
	<div data-testid="error-fallback">
		<span data-testid="error-stub">{stub.shortId}</span>
		<span data-testid="error-message">
			{error instanceof Error ? error.message : String(error)}
		</span>
	</div>
);

const Resolved: DispatchedComponent<EntityRefStub, NoProps> = ({ stub }) => (
	<div data-testid="resolved">resolved for {stub.shortId}</div>
);

const Throwing: DispatchedComponent<EntityRefStub, NoProps> = (_props) => {
	throw new Error("intentional render error");
};

const TestKey = declareEntityDispatchKey({
	uuid: "test-key-uuid",
	description: "TestKey",
	fallbacks: {
		Loading: Fallback,
		NoMatch,
		Error: ErrorFallback,
	},
});

// ----------------------------------------------------------------------
// Tests
// ----------------------------------------------------------------------

describe("<RenderDispatch> — fallback + resolution", () => {
	it("renders the key's Loading fallback while the effect hasn't resolved", () => {
		const entry = manualEntityEntry({
			initialState: null,
			body: () => {}, // never resolves
		});
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(TestKey, entry)],
			serviceEntries: [],
		});
		const { session } = makeMockSession();
		const dispatcher = createStaticDispatcher({ session, registry });
		const stub = new TestEntityRefStub("e-1");

		render(
			<DispatcherProvider dispatcher={dispatcher}>
				<RenderDispatch as={TestKey} stub={stub} />
			</DispatcherProvider>,
		);

		expect(screen.getByTestId("fallback").textContent).toContain("e-1");
	});

	it("renders the resolved component after the effect writes it", () => {
		// Effect writes `Resolved` straight into componentAtom on
		// mount — bypasses any load / metadata round-trips.
		const customEntry: DispatchKeyRegistryEntry<EntityRefStub, NoProps, null> = {
			initialState: null,
			effectFn:
				({ componentAtom }) =>
				(_get, set) => {
					set(componentAtom, Resolved);
				},
		};
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(TestKey, customEntry)],
			serviceEntries: [],
		});
		const { session } = makeMockSession();
		const dispatcher = createStaticDispatcher({ session, registry });
		const stub = new TestEntityRefStub("e-2");

		render(
			<DispatcherProvider dispatcher={dispatcher}>
				<RenderDispatch as={TestKey} stub={stub} />
			</DispatcherProvider>,
		);

		expect(screen.queryByTestId("fallback")).toBeNull();
		expect(screen.getByTestId("resolved").textContent).toContain("e-2");
	});
});

describe("<RenderDispatch> — error boundary", () => {
	it("renders the key's Error fallback when the dispatched component throws", () => {
		const customEntry: DispatchKeyRegistryEntry<EntityRefStub, NoProps, null> = {
			initialState: null,
			effectFn:
				({ componentAtom }) =>
				(_get, set) => {
					set(componentAtom, Throwing);
				},
		};
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(TestKey, customEntry)],
			serviceEntries: [],
		});
		const { session } = makeMockSession();
		const dispatcher = createStaticDispatcher({ session, registry });
		const stub = new TestEntityRefStub("e-3");

		render(
			<DispatcherProvider dispatcher={dispatcher}>
				<RenderDispatch as={TestKey} stub={stub} />
			</DispatcherProvider>,
		);

		expect(screen.getByTestId("error-fallback")).not.toBeNull();
		expect(screen.getByTestId("error-message").textContent).toBe("intentional render error");
		expect(screen.getByTestId("error-stub").textContent).toBe("e-3");
	});
});

describe("<RenderDispatch> — useDispatchSharedAtom", () => {
	it("provides a stable shared atom that the dispatched component can read + write", () => {
		// Component reads the shared atom and renders its `count`
		// field. A button bumps the count.
		const Counter: DispatchedComponent<EntityRefStub, NoProps> = () => {
			const sharedAtom = useDispatchSharedAtom<{ count: number }>({ count: 0 });
			const shared = useAtomValue(sharedAtom);
			const setShared = useSetAtom(sharedAtom);
			return (
				<div data-testid="counter">
					<span data-testid="count">{shared.count}</span>
					<button
						type="button"
						data-testid="bump"
						onClick={() => setShared((prev) => ({ count: prev.count + 1 }))}
					>
						bump
					</button>
				</div>
			);
		};

		const customEntry: DispatchKeyRegistryEntry<EntityRefStub, NoProps, null> = {
			initialState: null,
			effectFn:
				({ componentAtom }) =>
				(_get, set) => {
					set(componentAtom, Counter);
				},
		};
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(TestKey, customEntry)],
			serviceEntries: [],
		});
		const { session } = makeMockSession();
		const dispatcher = createStaticDispatcher({ session, registry });
		const stub = new TestEntityRefStub("e-4");

		render(
			<DispatcherProvider dispatcher={dispatcher}>
				<RenderDispatch as={TestKey} stub={stub} />
			</DispatcherProvider>,
		);

		expect(screen.getByTestId("count").textContent).toBe("0");
		fireEvent.click(screen.getByTestId("bump"));
		expect(screen.getByTestId("count").textContent).toBe("1");
		fireEvent.click(screen.getByTestId("bump"));
		expect(screen.getByTestId("count").textContent).toBe("2");
	});

	it("resets shared state to null when the dispatched component changes", async () => {
		// Cycle the rendered component between two different
		// identities; the shared atom should reset between them so
		// each component sees its own declared default.
		const First: DispatchedComponent<EntityRefStub, NoProps> = () => {
			const sharedAtom = useDispatchSharedAtom<{ tag: string }>({ tag: "first-default" });
			const shared = useAtomValue(sharedAtom);
			const setShared = useSetAtom(sharedAtom);
			return (
				<div>
					<span data-testid="first-tag">{shared.tag}</span>
					<button
						type="button"
						data-testid="first-set"
						onClick={() => setShared({ tag: "first-modified" })}
					>
						set
					</button>
				</div>
			);
		};
		const Second: DispatchedComponent<EntityRefStub, NoProps> = () => {
			const sharedAtom = useDispatchSharedAtom<{ tag: string }>({ tag: "second-default" });
			const shared = useAtomValue(sharedAtom);
			return <span data-testid="second-tag">{shared.tag}</span>;
		};

		// External signal to swap the rendered component.
		const customEntry: DispatchKeyRegistryEntry<EntityRefStub, NoProps, "first" | "second"> = {
			initialState: "first",
			effectFn:
				({ stateAtom, componentAtom }) =>
				(get, set) => {
					const phase = get(stateAtom);
					set(componentAtom, phase === "first" ? First : Second);
				},
		};
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(TestKey, customEntry)],
			serviceEntries: [],
		});
		const { session } = makeMockSession();
		const dispatcher = createStaticDispatcher({ session, registry });
		const stub = new TestEntityRefStub("e-5");

		// Wrap with a small harness that exposes a button to flip
		// the state-machine state, simulating the static helper
		// transitioning between phases that write different
		// components. The flip writes through React's
		// active-store path (`useSetAtom(session.userState)`)
		// rather than `session.store.set` directly — without a
		// `<JotaiProvider>` around the test, React reads from
		// jotai's default global store, so writes have to land
		// there too for the rest of the chain to observe them.
		const Harness = (): ReactNode => {
			const setUserState = useSetAtom(session.userState);
			return (
				<>
					<button
						type="button"
						data-testid="flip"
						onClick={() => {
							setUserState(((prev: unknown) => {
								// `StateValue`'s recursive union is too
								// strict to express the dispatcher's
								// nested shape cleanly here; cast via
								// `unknown`.
								const root =
									(prev as null | {
										entries?: Record<string, Record<string, unknown>>;
									}) ?? {};
								const entries = root.entries ?? {};
								return {
									...root,
									entries: {
										...entries,
										[TestKey.uuid]: {
											...(entries[TestKey.uuid] ?? {}),
											[stub.shortId]: {
												dispatcher_state: "second",
												component_shared_state: null,
											},
										},
									},
								};
								// biome-ignore lint/suspicious/noExplicitAny: writing through the FxtrSessionAtom's update signature; runtime is correct but the structural fit against `StateValue` is too strict to express cleanly here.
							}) as any);
						}}
					>
						flip
					</button>
					<DispatcherProvider dispatcher={dispatcher}>
						<RenderDispatch as={TestKey} stub={stub} />
					</DispatcherProvider>
				</>
			);
		};

		render(<Harness />);

		// Phase 1: First component, default tag.
		expect(screen.getByTestId("first-tag").textContent).toBe("first-default");
		// Mutate First's shared state.
		fireEvent.click(screen.getByTestId("first-set"));
		expect(screen.getByTestId("first-tag").textContent).toBe("first-modified");
		// Flip the state. The effect re-runs via `atomEffect`'s
		// dep tracking, writes `Second` into componentAtom (which
		// triggers the wrapper's shared-reset to null), then
		// React rerenders to `Second`, whose
		// `useDispatchSharedAtom` reads the null and returns its
		// declared default. The dep-tracking → effect re-run path
		// is microtask-scheduled by `jotai-effect`, so we await
		// the swap rather than expecting it synchronously after
		// the click.
		fireEvent.click(screen.getByTestId("flip"));
		await waitFor(() => {
			expect(screen.queryByTestId("first-tag")).toBeNull();
		});
		expect(screen.getByTestId("second-tag").textContent).toBe("second-default");
	});
});

describe("<RenderDispatch> — useDispatchProgressMessage + useDispatchProgressState", () => {
	it("exposes the progress message and state-machine state to the rendered component", () => {
		// A Loading fallback that surfaces both progress message and
		// state so we can assert their values via DOM queries.
		const FallbackWithProgress: DispatchedComponent<EntityRefStub, NoProps> = ({ stub }) => {
			const msg = useDispatchProgressMessage();
			const state = useDispatchProgressState<{ phase: string }>();
			return (
				<div data-testid="fallback-with-progress">
					<span data-testid="progress-msg">{msg}</span>
					<span data-testid="state-phase">{state.phase}</span>
					<span data-testid="stub-shortid">{stub.shortId}</span>
				</div>
			);
		};

		// A separate key whose `fallbacks.Loading` reads the
		// dispatch-internal state / progress message. The progress
		// hooks read off the entry's atoms, which are set up by
		// the dispatcher from this key + the manual entry below.
		const ProgressKey = declareEntityDispatchKey({
			uuid: "test-key-progress-uuid",
			description: "TestKeyWithProgress",
			fallbacks: {
				Loading: FallbackWithProgress,
				NoMatch,
				Error: ErrorFallback,
			},
		});

		const customEntry: DispatchKeyRegistryEntry<EntityRefStub, NoProps, { phase: string }> = {
			initialState: { phase: "loading-entity" },
			initialProgressMessage: "loading entity…",
			// Effect does nothing — stay in the Loading fallback so
			// the hooks read the initial values.
			effectFn: () => (_get, _set) => {},
		};
		const registry = createDispatchRegistry({
			entityEntries: [entityEntry(ProgressKey, customEntry)],
			serviceEntries: [],
		});
		const { session } = makeMockSession();
		const dispatcher = createStaticDispatcher({ session, registry });
		const stub = new TestEntityRefStub("e-6");

		render(
			<DispatcherProvider dispatcher={dispatcher}>
				<RenderDispatch as={ProgressKey} stub={stub} />
			</DispatcherProvider>,
		);

		expect(screen.getByTestId("progress-msg").textContent).toBe("loading entity…");
		expect(screen.getByTestId("state-phase").textContent).toBe("loading-entity");
		expect(screen.getByTestId("stub-shortid").textContent).toBe("e-6");
	});
});

describe("<RenderDispatch> — error paths", () => {
	it("renders the key's NoMatch fallback when the registry has no matching entry", () => {
		// Static dispatcher used to throw `UnregisteredDispatchKeyError`
		// here. Per the dispatch-key fallback refactor it now degrades
		// gracefully to rendering the key's `NoMatch` fallback —
		// behaviourally symmetric with the dynamic dispatcher's
		// "service returned no renderer" terminal.
		const registry = createDispatchRegistry({
			entityEntries: [],
			serviceEntries: [],
		});
		const { session } = makeMockSession();
		const dispatcher = createStaticDispatcher({ session, registry });
		const stub = new TestEntityRefStub("e-7");

		render(
			<DispatcherProvider dispatcher={dispatcher}>
				<RenderDispatch as={TestKey} stub={stub} />
			</DispatcherProvider>,
		);

		expect(screen.getByTestId("no-match").textContent).toContain("e-7");
	});

	it("useDispatchSharedAtom throws when called outside a <RenderDispatch>", () => {
		const Bad = (): ReactNode => {
			useDispatchSharedAtom({});
			return null;
		};
		expect(() => render(<Bad />)).toThrow(/no active <RenderDispatch>/);
	});
});

// (Static-helper state-machine coverage lives in
// `frontend_lib/packages/fxtr-ui-react-dispatch/tests/static_entity_registry.test.ts`
// and `.../static_service_registry.test.ts`; we don't duplicate it
// through the React layer here.)
