/**
 * React-render tests for `@fxtr-ui/workstation-dispatch`'s
 * `RefLink` + `RefLinkImplContext`.
 *
 * Covered:
 *   - Default impl: rendered without a context Provider, `RefLink`
 *     calls into `RenderDispatch` (no extra wrappers). We assert
 *     against the dispatched key's `NoMatchFallback` content
 *     since the test dispatcher has no registered entries.
 *   - Custom impl via context: providing a different
 *     `RefLinkImpl` through `RefLinkImplContext.Provider` makes
 *     `RefLink` forward to it — the shell uses this hook to
 *     install its interactive impl without `@fxtr-ui/workstation-dispatch`
 *     ever knowing.
 *
 * @vitest-environment jsdom
 */

import { EntityRefStub } from "@fxtr-ui/core";
import {
	DispatcherProvider,
	createDispatchRegistry,
	createStaticDispatcher,
} from "@fxtr-ui/react-dispatch";
import {
	BlockLink,
	InlineLink,
	RefLink,
	type RefLinkImpl,
	RefLinkImplContext,
	type RefLinkProps,
} from "@fxtr-ui/workstation-dispatch";
import { cleanup, render, screen } from "@testing-library/react";
import { atom, createStore } from "jotai";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

// ----- Test stubs + mock session ----------------------------------

class TestEntityStub extends EntityRefStub {
	constructor(public readonly shortId: string) {
		super();
	}
}

function mockSession(): Parameters<typeof createStaticDispatcher>[0]["session"] {
	const lifecycle = atom<"connecting" | "connected" | "stale" | "closed">("connected");
	// Plain primitive atom standing in for the session's userState
	// atom. The dispatcher just reads `null` here (no entries
	// previously rendered) and writes through it for cache state.
	const userState = atom<unknown>(null);
	return {
		sessionId: "test-session",
		lifecycle,
		userState,
		store: createStore(),
		detach: async () => {},
	} as unknown as Parameters<typeof createStaticDispatcher>[0]["session"];
}

// ----- Bare wrapper around <RefLink> with dispatcher in scope -----

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

describe("RefLink default impl", () => {
	it("with no context provider, dispatches via RenderDispatch (no extra wrappers)", () => {
		// No matching registry entry → the dispatcher renders the
		// key's `NoMatchFallback`. For `BlockLink`, the canonical
		// fallback (`EntityBlockLinkFallback`) shows a short id —
		// we just check the short id appears somewhere in the
		// rendered output, confirming the default impl plumbed the
		// dispatch correctly.
		const stub = new TestEntityStub("e-default-block");
		render(withDispatcher(<RefLink stub={stub} as="block" />));
		expect(screen.getByText("e-default-block")).toBeTruthy();
	});

	it("default impl routes 'inline' to InlineLink", () => {
		const stub = new TestEntityStub("e-default-inline");
		render(withDispatcher(<RefLink stub={stub} as="inline" />));
		// Inline fallback uses a `<code>` element with the shortId.
		const node = screen.getByText("e-default-inline");
		expect(node.tagName.toLowerCase()).toBe("code");
	});

	it("default impl forwards linkText to the dispatched component", () => {
		const stub = new TestEntityStub("e-text-test");
		render(withDispatcher(<RefLink stub={stub} as="inline" linkText="hello" />));
		expect(screen.getByText("hello")).toBeTruthy();
		expect(screen.queryByText("e-text-test")).toBeNull();
	});
});

describe("RefLink impl override via RefLinkImplContext", () => {
	it("uses the impl supplied via the context provider", () => {
		const customImpl: RefLinkImpl = vi.fn(({ stub }) => (
			<div data-testid="custom-impl">custom impl rendered: {stub.shortId}</div>
		));

		const stub = new TestEntityStub("e-custom");
		render(
			withDispatcher(
				<RefLinkImplContext.Provider value={customImpl}>
					<RefLink stub={stub} as="block" />
				</RefLinkImplContext.Provider>,
			),
		);

		// Custom impl rendered, not the default's dispatch.
		const customNode = screen.getByTestId("custom-impl");
		expect(customNode.textContent).toBe("custom impl rendered: e-custom");
		// The default impl's fallback should NOT be in the tree.
		// `EntityBlockLinkFallback` renders the shortId as a styled
		// span — when overridden, that exact text isn't present.
		expect(screen.queryByText("e-custom", { selector: "span" })).toBeNull();
	});

	it("forwards all props to the impl unchanged", () => {
		const seen: RefLinkProps[] = [];
		const recordingImpl: RefLinkImpl = (props) => {
			seen.push(props);
			return <span data-testid="recording">ok</span>;
		};

		const stub = new TestEntityStub("e-forward");
		render(
			withDispatcher(
				<RefLinkImplContext.Provider value={recordingImpl}>
					<RefLink stub={stub} as="inline" linkText="custom-text" />
				</RefLinkImplContext.Provider>,
			),
		);

		expect(seen).toHaveLength(1);
		expect(seen[0]?.stub).toBe(stub);
		expect(seen[0]?.as).toBe("inline");
		expect(seen[0]?.linkText).toBe("custom-text");
	});

	it("falls back to default impl when provider is removed", () => {
		const customImpl: RefLinkImpl = ({ stub }) => (
			<span data-testid="custom">{stub.shortId} custom</span>
		);

		const stub1 = new TestEntityStub("e-with-provider");
		const stub2 = new TestEntityStub("e-without-provider");

		const { rerender } = render(
			withDispatcher(
				<RefLinkImplContext.Provider value={customImpl}>
					<RefLink stub={stub1} as="block" />
				</RefLinkImplContext.Provider>,
			),
		);
		expect(screen.getByTestId("custom").textContent).toBe("e-with-provider custom");

		// Re-render without the provider — should fall through to default.
		rerender(withDispatcher(<RefLink stub={stub2} as="block" />));
		expect(screen.queryByTestId("custom")).toBeNull();
		expect(screen.getByText("e-without-provider")).toBeTruthy();
	});
});

describe("Canonical key identity", () => {
	it("BlockLink and InlineLink imports are stable across re-imports", async () => {
		// A renderer module imports BlockLink/InlineLink and uses
		// the same object the host shell has. We can't test the
		// import-map shim here, but we can at least confirm the
		// in-package imports come from one source.
		const reImported = await import("@fxtr-ui/workstation-dispatch");
		expect(reImported.BlockLink).toBe(BlockLink);
		expect(reImported.InlineLink).toBe(InlineLink);
	});
});
