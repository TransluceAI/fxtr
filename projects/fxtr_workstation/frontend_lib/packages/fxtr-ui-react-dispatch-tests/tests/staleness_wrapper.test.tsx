/**
 * Render-layer tests for `makeEntryWrapper` (the dynamic
 * dispatcher's wrap-on-write staleness affordance).
 *
 * Non-React contract (WeakMap memoisation, symbol-stamping) is
 * covered by the sibling unit tests under
 * `frontend_lib/packages/fxtr-ui-react-dispatch/tests/staleness_wrapper.test.ts`.
 * Here we exercise the actual render: confirming that
 * `display: contents` ↔ `display: block; opacity: 0.5` toggles
 * when the staleAtom flips, and that the inner component subtree
 * keeps its identity across the flip (no unmount).
 *
 * @vitest-environment jsdom
 */

import type { EntityRefStub } from "@fxtr-ui/core";
import { type DispatchedComponent, makeEntryWrapper } from "@fxtr-ui/react-dispatch";
import { act, cleanup, render, screen } from "@testing-library/react";
import { Provider as JotaiProvider, atom, createStore } from "jotai";
import { afterEach, describe, expect, it } from "vitest";
import { TestEntityRefStub } from "./_mocks";

// Sentinel inner component with a stable test id so the render
// tests can find it. Typed as a real `DispatchedComponent` so the
// wrap helper's type plumbing is exercised end-to-end.
const InnerSentinel: DispatchedComponent<EntityRefStub, object> = ({ stub }) => (
	<div data-testid="inner-sentinel">stub: {stub.shortId}</div>
);

const sentinelStub = new TestEntityRefStub("test-stub");

afterEach(() => {
	cleanup();
});

describe("makeEntryWrapper render behaviour", () => {
	it("renders the inner component inside a div with display: contents when fresh", () => {
		const store = createStore();
		const staleAtom = atom(false);
		const wrap = makeEntryWrapper(staleAtom);
		const Wrapped = wrap(InnerSentinel);

		render(
			<JotaiProvider store={store}>
				<Wrapped stub={sentinelStub} />
			</JotaiProvider>,
		);

		const inner = screen.getByTestId("inner-sentinel");
		const wrapDiv = inner.parentElement as HTMLDivElement;
		// The wrap is the immediate parent <div>; its style should
		// be `display: contents` when not stale.
		expect(wrapDiv.tagName.toLowerCase()).toBe("div");
		expect(wrapDiv.style.display).toBe("contents");
		// Inline opacity should not be set (the FRESH_STYLE only
		// touches `display`).
		expect(wrapDiv.style.opacity).toBe("");
	});

	it("toggles to display: block + opacity: 0.5 when the staleAtom flips to true", () => {
		const store = createStore();
		const staleAtom = atom(false);
		const wrap = makeEntryWrapper(staleAtom);
		const Wrapped = wrap(InnerSentinel);

		render(
			<JotaiProvider store={store}>
				<Wrapped stub={sentinelStub} />
			</JotaiProvider>,
		);

		const innerBefore = screen.getByTestId("inner-sentinel");
		const wrapDivBefore = innerBefore.parentElement as HTMLDivElement;
		expect(wrapDivBefore.style.display).toBe("contents");

		// Flip — wrap re-renders with STALE_STYLE. Wrap the store
		// write in `act` so React flushes its queued render before
		// we read the resulting DOM.
		act(() => {
			store.set(staleAtom, true);
		});

		const innerAfter = screen.getByTestId("inner-sentinel");
		const wrapDivAfter = innerAfter.parentElement as HTMLDivElement;
		expect(wrapDivAfter.style.display).toBe("block");
		expect(wrapDivAfter.style.opacity).toBe("0.5");

		// The inner DOM node is the same object across the flip —
		// React doesn't unmount/remount the inner component just
		// because the wrap div's style changed.
		expect(innerAfter).toBe(innerBefore);
		expect(wrapDivAfter).toBe(wrapDivBefore);
	});

	it("toggles back to display: contents when the staleAtom flips back to false", () => {
		const store = createStore();
		const staleAtom = atom(true);
		const wrap = makeEntryWrapper(staleAtom);
		const Wrapped = wrap(InnerSentinel);

		render(
			<JotaiProvider store={store}>
				<Wrapped stub={sentinelStub} />
			</JotaiProvider>,
		);

		const inner = screen.getByTestId("inner-sentinel");
		const wrapDiv = inner.parentElement as HTMLDivElement;
		expect(wrapDiv.style.display).toBe("block");
		expect(wrapDiv.style.opacity).toBe("0.5");

		act(() => {
			store.set(staleAtom, false);
		});

		expect(wrapDiv.style.display).toBe("contents");
		expect(wrapDiv.style.opacity).toBe("");
	});

	it("forwards props to the inner component", () => {
		const store = createStore();
		const staleAtom = atom(false);
		const wrap = makeEntryWrapper(staleAtom);
		const Wrapped = wrap(InnerSentinel);

		const stub = new TestEntityRefStub("stub-xyz");

		render(
			<JotaiProvider store={store}>
				<Wrapped stub={stub} />
			</JotaiProvider>,
		);

		expect(screen.getByTestId("inner-sentinel").textContent).toBe("stub: stub-xyz");
	});
});
