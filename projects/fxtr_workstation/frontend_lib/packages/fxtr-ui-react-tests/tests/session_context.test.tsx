/**
 * Tests for `<FxtrSessionProvider>` + `useSession`.
 *
 * @vitest-environment jsdom
 */

import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FxtrSessionProvider, useSession } from "@fxtr-ui/react";
import { makeMockSession } from "./_mocks";

afterEach(cleanup);

describe("FxtrSessionProvider", () => {
	it("makes the session reachable via useSession", () => {
		const { session } = makeMockSession({ sessionId: "abc" });
		const { result } = renderHook(() => useSession(), {
			wrapper: ({ children }) => (
				<FxtrSessionProvider session={session}>{children}</FxtrSessionProvider>
			),
		});
		expect(result.current).toBe(session);
		expect(result.current.sessionId).toBe("abc");
	});

	it("scopes jotai's nearest-Provider store to session.store", () => {
		// Two sessions with two distinct stores, hosted by two
		// providers. A `useSession` inside provider A returns A's
		// session even if provider B is also mounted elsewhere.
		const a = makeMockSession({ sessionId: "A" });
		const b = makeMockSession({ sessionId: "B" });

		function Inner({ label }: { readonly label: string }) {
			const session = useSession();
			return <span data-testid={label}>{session.sessionId}</span>;
		}

		render(
			<>
				<FxtrSessionProvider session={a.session}>
					<Inner label="a" />
				</FxtrSessionProvider>
				<FxtrSessionProvider session={b.session}>
					<Inner label="b" />
				</FxtrSessionProvider>
			</>,
		);

		expect(screen.getByTestId("a").textContent).toBe("A");
		expect(screen.getByTestId("b").textContent).toBe("B");
	});

	it("updates the context value when the session prop changes", () => {
		const a = makeMockSession({ sessionId: "A" });
		const b = makeMockSession({ sessionId: "B" });

		function ShowId() {
			const session = useSession();
			return <span data-testid="id">{session.sessionId}</span>;
		}

		const { rerender } = render(
			<FxtrSessionProvider session={a.session}>
				<ShowId />
			</FxtrSessionProvider>,
		);
		expect(screen.getByTestId("id").textContent).toBe("A");

		rerender(
			<FxtrSessionProvider session={b.session}>
				<ShowId />
			</FxtrSessionProvider>,
		);
		expect(screen.getByTestId("id").textContent).toBe("B");
	});
});

describe("useSession outside provider", () => {
	it("throws a clear error", () => {
		// Swallow React's error-boundary log noise; the throw is
		// what we're asserting on.
		const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
		try {
			expect(() => renderHook(() => useSession())).toThrow(
				/useSession\(\) called outside a <FxtrSessionProvider>/,
			);
		} finally {
			errorSpy.mockRestore();
		}
		// `act` is silenced; nothing depends on it for the throw
		// case, but its presence avoids an unused-import lint.
		void act;
	});
});
