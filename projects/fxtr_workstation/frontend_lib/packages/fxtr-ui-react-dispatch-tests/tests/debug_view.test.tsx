/**
 * Smoke tests for `dispatcher.debugView()` on both the static and
 * dynamic dispatchers (stage 10c chunk 5).
 *
 * Coverage:
 *   - Static dispatcher: `debugView()` returns an element that
 *     renders the dispatcher status, session id, and entry table.
 *     Resolving a new entry through `getEntry` causes the entry
 *     table to grow reactively (via the dispatcher's internal
 *     `mountedIdsAtom`).
 *   - Dynamic dispatcher: `debugView()` renders the status / four
 *     type-cache sections / compiled-modules section / entries
 *     section. The cache-key list updates reactively when the
 *     dispatcher's state machine writes into the underlying
 *     atoms.
 *
 * @vitest-environment jsdom
 */

import type { EntityRefStub } from "@fxtr-ui/core";
import {
	type DispatchKeyRegistryEntry,
	EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
	type ErrorFallbackComponent,
	type NoProps,
	createDispatchRegistry,
	createDynamicDispatcher,
	createStaticDispatcher,
	declareEntityDispatchKey,
	entityEntry,
} from "@fxtr-ui/react-dispatch";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TestEntityRefStub, makeMockSession } from "./_mocks";

// ----- Sentinels ------------------------------------------------------

function Fallback() {
	return null;
}
function NoMatch() {
	return null;
}
const ErrFallback: ErrorFallbackComponent<EntityRefStub, NoProps> = () => null;

const TestKey = declareEntityDispatchKey<NoProps>({
	uuid: "00000000-0000-4000-8000-000000040001",
	description: "DebugViewTestKey",
	fallbacks: {
		Loading: Fallback,
		NoMatch,
		Error: ErrFallback,
	},
});

function Resolved() {
	return null;
}

// ----- Static dispatcher tests ----------------------------------------

afterEach(() => {
	cleanup();
});

describe("StaticDispatcher.debugView()", () => {
	it("renders the dispatcher status + session id + empty-entries message", () => {
		const registry = createDispatchRegistry({ entityEntries: [], serviceEntries: [] });
		const { session } = makeMockSession({ sessionId: "session-static-empty" });
		const dispatcher = createStaticDispatcher({ session, registry });
		const view = dispatcher.debugView?.();
		expect(view, "static dispatcher should expose debugView()").toBeDefined();
		// biome-ignore lint/style/noNonNullAssertion: debugView presence asserted above
		render(view!);

		expect(screen.getByTestId("dispatcher-status").textContent).toBe("connected");
		expect(screen.getByTestId("session-id").textContent).toBe("session-static-empty");
		expect(screen.getByTestId("mounted-count").textContent).toBe("0");
		expect(screen.getByTestId("no-entries")).toBeTruthy();
	});

	it("entry table grows reactively when getEntry adds an entry", async () => {
		// Custom entry that immediately writes a known Resolved
		// component to `componentAtom` — keeps the test focused on
		// the debug-view's reactivity, not on state-machine
		// semantics.
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
		const { session } = makeMockSession({ sessionId: "session-static-grows" });
		const dispatcher = createStaticDispatcher({ session, registry });
		const view = dispatcher.debugView?.();
		// biome-ignore lint/style/noNonNullAssertion: debugView is defined for staticDispatcher
		render(view!);

		// Initially: no entries.
		expect(screen.getByTestId("mounted-count").textContent).toBe("0");
		expect(screen.queryByTestId("no-entries")).toBeTruthy();

		// Resolve an entry. The dispatcher's mountedIdsAtom updates
		// inside `getEntry`; the debug view re-renders.
		act(() => {
			const entry = dispatcher.getEntry(TestKey, new TestEntityRefStub("e-static"));
			// Touch componentAtom so the state machine effect mounts.
			session.store.get(entry.componentAtom);
		});

		await waitFor(() => {
			expect(screen.getByTestId("mounted-count").textContent).toBe("1");
		});
		expect(screen.queryByTestId("no-entries")).toBeNull();
		const rowTestId = `entry-row-${TestKey.uuid}::e-static`;
		expect(screen.getByTestId(rowTestId)).toBeTruthy();
	});

	it("status updates when the dispatcher is detached", async () => {
		const registry = createDispatchRegistry({ entityEntries: [], serviceEntries: [] });
		const { session } = makeMockSession({ sessionId: "session-static-detach" });
		const dispatcher = createStaticDispatcher({ session, registry });
		const view = dispatcher.debugView?.();
		// biome-ignore lint/style/noNonNullAssertion: debugView is defined for staticDispatcher
		render(view!);

		expect(screen.getByTestId("dispatcher-status").textContent).toBe("connected");

		await act(async () => {
			await dispatcher.detach();
		});

		expect(screen.getByTestId("dispatcher-status").textContent).toBe("closed");
	});
});

// ----- Dynamic dispatcher tests ---------------------------------------

describe("DynamicDispatcher.debugView()", () => {
	it("renders the four type-cache sections + compiled-modules + entries + reload-inbox sections", () => {
		// Use a non-"connected" lifecycle so the dispatcher's
		// reload-subscription effect's lifecycle gate short-circuits
		// and doesn't try to allocate a callback/invoke on the
		// mock session (whose allocate* methods aren't wired).
		// The debug-view rendering doesn't depend on that subscription
		// having happened — we're just validating the surface
		// shape against empty caches.
		const { session } = makeMockSession({
			sessionId: "session-dynamic-empty",
			lifecycle: "connecting",
		});
		const dispatchServiceRef = new TestEntityRefStub("svc-mock") as unknown as Parameters<
			typeof createDynamicDispatcher
		>[0]["dispatchServiceRef"];

		const dispatcher = createDynamicDispatcher({
			session,
			dispatchServiceRef,
			loadEntityData: async () => ({ module: new Uint8Array() }),
			compileRendererBytes: async () => ({}),
		});
		const view = dispatcher.debugView?.();
		expect(view, "dynamic dispatcher should expose debugView()").toBeDefined();
		// biome-ignore lint/style/noNonNullAssertion: presence asserted above
		render(view!);

		// Status + session id show.
		expect(screen.getByTestId("dispatcher-status")).toBeTruthy();
		expect(screen.getByTestId("session-id").textContent).toBe("session-dynamic-empty");
		expect(screen.getByTestId("session-lifecycle").textContent).toBe("connecting");

		// All four cache sections present, each starting at count 0.
		expect(screen.getByTestId("svc-type-cache-count").textContent).toBe("(0)");
		expect(screen.getByTestId("svc-type-active-count").textContent).toBe("(0)");
		expect(screen.getByTestId("type-renderer-cache-count").textContent).toBe("(0)");
		expect(screen.getByTestId("type-renderer-active-count").textContent).toBe("(0)");
		expect(screen.getByTestId("compiled-modules-count").textContent).toBe("0");
		expect(screen.getByTestId("no-entries")).toBeTruthy();
		expect(screen.getByTestId("reload-inbox-section")).toBeTruthy();
		// Reload subscription hasn't fired (lifecycle != "connected"),
		// so the inbox is null and the empty-state message renders.
		expect(screen.getByTestId("reload-inbox-empty")).toBeTruthy();

		void dispatcher.detach();
	});

	it("the 'Reload everything' button wipes the type caches", async () => {
		// Keep the reload subscription from firing (its allocate* would
		// hit the unwired mock); the manual-reload wipe is independent
		// of the subscription.
		const { session, userStatePrivate } = makeMockSession({
			sessionId: "session-dynamic-reload-btn",
			lifecycle: "connecting",
		});
		const dispatchServiceRef = new TestEntityRefStub("svc-mock") as unknown as Parameters<
			typeof createDynamicDispatcher
		>[0]["dispatchServiceRef"];

		const dispatcher = createDynamicDispatcher({
			session,
			dispatchServiceRef,
			loadEntityData: async () => ({ module: new Uint8Array() }),
			compileRendererBytes: async () => ({}),
		});

		// Seed a non-empty type-renderer cache so the wipe is visible.
		session.store.set(userStatePrivate, {
			...EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
			type_renderer_cache: { t1: { k1: null } },
			// biome-ignore lint/suspicious/noExplicitAny: pre-seeded userState narrowed to the dispatcher's shape
		} as any);

		const view = dispatcher.debugView?.();
		// biome-ignore lint/style/noNonNullAssertion: presence asserted by the prior test
		render(view!);

		expect(screen.getByTestId("type-renderer-cache-count").textContent).toBe("(1)");

		fireEvent.click(screen.getByTestId("reload-everything"));

		await waitFor(() => {
			expect(screen.getByTestId("type-renderer-cache-count").textContent).toBe("(0)");
		});

		void dispatcher.detach();
	});
});
