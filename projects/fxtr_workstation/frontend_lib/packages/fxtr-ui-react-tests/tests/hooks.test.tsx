/**
 * Tests for `useSessionRootStateAtom`, `useSubAtom`, `useResult` — the
 * three hooks `@fxtr-ui/react` exposes. All exercised under a
 * `<FxtrSessionProvider>` with a mock session.
 *
 * @vitest-environment jsdom
 */

import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type {
	FxtrSessionAtom,
	FxtrSessionUpdateCtx,
	FxtrUpdate,
	UserStateWriteOptions,
} from "@fxtr-ui/core";
import {
	FxtrSessionProvider,
	useResult,
	useSessionRootStateAtom,
	useSubAtom,
} from "@fxtr-ui/react";
import { atom, useAtomValue, useSetAtom } from "jotai";
import { TestOpResult, makeMockSession } from "./_mocks";

/**
 * Build a typed `FxtrSessionAtom<T | null>` backed by a primitive
 * atom, with a stand-in ctx whose `session` field points at the
 * given session (so the ctx-threading assertion in subAtom tests
 * can compare identity). Used to test `useSubAtom` against a
 * known typed shape without smuggling extra structure into
 * `session.userState`.
 */
function makeTypedAtom<T>(
	session: ReturnType<typeof makeMockSession>["session"],
	initial: T,
): FxtrSessionAtom<T> {
	const inner = atom<T>(initial);
	const ctx: FxtrSessionUpdateCtx = {
		allocateLoad: () => {
			throw new Error("not used");
		},
		allocateInvoke: () => {
			throw new Error("not used");
		},
		allocateGetEntityMetadata: () => {
			throw new Error("not used");
		},
		allocateGetServiceMetadata: () => {
			throw new Error("not used");
		},
		allocateCallback: () => {
			throw new Error("not used");
		},
		session,
	};
	return atom(
		(get) => get(inner),
		(get, set, update: FxtrUpdate<T>, _opts?: UserStateWriteOptions) => {
			const prev = get(inner);
			const next =
				typeof update === "function"
					? (update as (p: T, c: FxtrSessionUpdateCtx) => T)(prev, ctx)
					: update;
			set(inner, next);
		},
	) as FxtrSessionAtom<T>;
}

afterEach(cleanup);

function wrapWith(session: ReturnType<typeof makeMockSession>["session"]) {
	return ({ children }: { children: React.ReactNode }) => (
		<FxtrSessionProvider session={session}>{children}</FxtrSessionProvider>
	);
}

// --- useSessionRootStateAtom: read --------------------------------------

describe("useSessionRootStateAtom (read)", () => {
	it("returns the default when the session has no user state yet", () => {
		const { session } = makeMockSession({ initial: null });
		const { result } = renderHook(
			() => {
				const a = useSessionRootStateAtom<{ msg?: string }>({ msg: "fallback" });
				return useAtomValue(a);
			},
			{ wrapper: wrapWith(session) },
		);
		expect(result.current).toEqual({ msg: "fallback" });
	});

	it("returns the current user state, typed via the generic", () => {
		const { session } = makeMockSession({ initial: { msg: "hi" } });
		const { result } = renderHook(
			() => {
				const a = useSessionRootStateAtom<{ msg?: string }>({});
				return useAtomValue(a);
			},
			{ wrapper: wrapWith(session) },
		);
		expect(result.current.msg).toBe("hi");
	});

	it("re-renders when the session's user state updates", () => {
		const { session, userStatePrivate } = makeMockSession({ initial: { msg: "before" } });
		const { result } = renderHook(
			() => {
				const a = useSessionRootStateAtom<{ msg?: string }>({});
				return useAtomValue(a);
			},
			{ wrapper: wrapWith(session) },
		);
		expect(result.current.msg).toBe("before");
		act(() => {
			session.store.set(userStatePrivate, { msg: "after" });
		});
		expect(result.current.msg).toBe("after");
	});

	it("lazy-init form: factory is called once at mount", () => {
		const { session } = makeMockSession({ initial: null });
		let factoryCalls = 0;
		const { result, rerender } = renderHook(
			() => {
				const a = useSessionRootStateAtom<{ msg: string }>(() => {
					factoryCalls += 1;
					return { msg: "lazy" };
				});
				return useAtomValue(a);
			},
			{ wrapper: wrapWith(session) },
		);
		expect(result.current.msg).toBe("lazy");
		expect(factoryCalls).toBe(1);
		rerender();
		// Factory is captured once and never re-runs even on re-render.
		expect(factoryCalls).toBe(1);
	});

	it("ignores changes to initialDefault after the first render", () => {
		const { session } = makeMockSession({ initial: null });
		const { result, rerender } = renderHook(
			({ d }: { d: { msg: string } }) => {
				const a = useSessionRootStateAtom<{ msg: string }>(d);
				return useAtomValue(a);
			},
			{ wrapper: wrapWith(session), initialProps: { d: { msg: "first" } } },
		);
		expect(result.current.msg).toBe("first");
		rerender({ d: { msg: "second" } });
		// The default was captured at mount; subsequent default changes
		// are ignored — same convention as `useState(initialValue)`.
		expect(result.current.msg).toBe("first");
	});
});

// --- useSessionRootStateAtom: write -------------------------------------

describe("useSessionRootStateAtom (write via useSetAtom)", () => {
	it("static-value writes reach the underlying user state", () => {
		const { session } = makeMockSession({ initial: null });
		const { result } = renderHook(
			() => {
				const a = useSessionRootStateAtom<{ a: number }>({ a: 0 });
				return { atom: a, set: useSetAtom(a) };
			},
			{ wrapper: wrapWith(session) },
		);
		act(() => result.current.set({ a: 1 }));
		expect(session.store.get(result.current.atom)).toEqual({ a: 1 });
	});

	it("function-form writes get (prev, ctx); prev reflects the latest value", () => {
		const { session } = makeMockSession({ initial: { a: 1 } });
		const { result } = renderHook(
			() => {
				const a = useSessionRootStateAtom<{ a: number }>({ a: 0 });
				return { atom: a, set: useSetAtom(a) };
			},
			{ wrapper: wrapWith(session) },
		);
		let seenPrev: { a: number } | null = null;
		let seenCtxSession: unknown = null;
		act(() =>
			result.current.set((prev, ctx) => {
				seenPrev = prev;
				seenCtxSession = ctx.session;
				return { a: prev.a + 1 };
			}),
		);
		expect(seenPrev).toEqual({ a: 1 });
		// The ctx is bound to the session that owns the atom.
		expect(seenCtxSession).toBe(session);
		expect(session.store.get(result.current.atom)).toEqual({ a: 2 });
	});

	it("function-form prev defaults in when the underlying is null", () => {
		const { session } = makeMockSession({ initial: null });
		const { result } = renderHook(
			() => {
				const a = useSessionRootStateAtom<{ a: number }>({ a: 7 });
				return { atom: a, set: useSetAtom(a) };
			},
			{ wrapper: wrapWith(session) },
		);
		let seenPrev: { a: number } | null = null;
		act(() =>
			result.current.set((prev) => {
				seenPrev = prev;
				return { a: prev.a + 1 };
			}),
		);
		expect(seenPrev).toEqual({ a: 7 });
		expect(session.store.get(result.current.atom)).toEqual({ a: 8 });
	});
});

// --- useSubAtom ------------------------------------------------------

describe("useSubAtom", () => {
	it("projects a sub-key for read via useAtomValue against the child atom", () => {
		const { session } = makeMockSession();
		const parent = makeTypedAtom<{ a: number; b: string }>(session, { a: 1, b: "hi" });
		const { result } = renderHook(() => useSubAtom(parent, "a"), {
			wrapper: wrapWith(session),
		});
		// The returned atom is a fresh writable; we can read its
		// projected value via the session's store.
		expect(session.store.get(result.current)).toBe(1);
	});

	it("writes through subAtom propagate to the parent atom", () => {
		const { session } = makeMockSession();
		const parent = makeTypedAtom<{ counter: number }>(session, { counter: 0 });
		const { result } = renderHook(() => useSubAtom(parent, "counter"), {
			wrapper: wrapWith(session),
		});
		act(() => {
			session.store.set(result.current, 5);
		});
		expect(session.store.get(parent)).toEqual({ counter: 5 });
	});

	it("returns identity-stable atom across re-renders with same parent + key", () => {
		const { session } = makeMockSession();
		const parent = makeTypedAtom<{ msg: string }>(session, { msg: "hi" });
		const { result, rerender } = renderHook(() => useSubAtom(parent, "msg"), {
			wrapper: wrapWith(session),
		});
		const first = result.current;
		rerender();
		expect(result.current).toBe(first);
	});

	it("returns a fresh atom when the key changes", () => {
		const { session } = makeMockSession();
		const parent = makeTypedAtom<{ a: number; b: number }>(session, { a: 1, b: 2 });
		const initialProps: { key: "a" | "b" } = { key: "a" };
		const { result, rerender } = renderHook(
			(props: { key: "a" | "b" }) => useSubAtom(parent, props.key),
			{
				wrapper: wrapWith(session),
				initialProps,
			},
		);
		const first = result.current;
		rerender({ key: "b" });
		expect(result.current).not.toBe(first);
		expect(session.store.get(result.current)).toBe(2);
	});

	it("chained useSubAtom(useSubAtom(...)) composes — read + write flow through", () => {
		const { session } = makeMockSession();
		const parent = makeTypedAtom<{ outer: { inner: number } }>(session, {
			outer: { inner: 7 },
		});
		const { result } = renderHook(
			() => {
				const outerAtom = useSubAtom(parent, "outer");
				const innerAtom = useSubAtom(outerAtom, "inner");
				return innerAtom;
			},
			{ wrapper: wrapWith(session) },
		);
		expect(session.store.get(result.current)).toBe(7);
		act(() => session.store.set(result.current, 42));
		expect(session.store.get(parent)).toEqual({ outer: { inner: 42 } });
	});
});

// --- useResult -------------------------------------------------------

describe("useResult", () => {
	it("returns the stub's current status", () => {
		const { session } = makeMockSession();
		const stub = new TestOpResult<number>({ status: "pending" });
		const { result } = renderHook(() => useResult(stub), {
			wrapper: wrapWith(session),
		});
		expect(result.current).toEqual({ status: "pending" });
	});

	it("re-renders when the underlying result atom changes", () => {
		const { session } = makeMockSession();
		const stub = new TestOpResult<number>({ status: "pending" });
		const { result } = renderHook(() => useResult(stub), {
			wrapper: wrapWith(session),
		});
		expect(result.current.status).toBe("pending");
		act(() => {
			session.store.set(stub._resultPrivate, { status: "filled", value: 42 });
		});
		expect(result.current).toEqual({ status: "filled", value: 42 });
	});

	it("surfaces the errored status + message", () => {
		const { session } = makeMockSession();
		const stub = new TestOpResult<number>({ status: "errored", message: "boom" });
		const { result } = renderHook(() => useResult(stub), {
			wrapper: wrapWith(session),
		});
		expect(result.current).toEqual({ status: "errored", message: "boom" });
	});
});

// --- Multi-session component isolation -------------------------------

describe("multi-session isolation", () => {
	it("two providers with separate sessions have independent component-internal state", () => {
		const a = makeMockSession({ sessionId: "A", initial: { msg: "alpha" } });
		const b = makeMockSession({ sessionId: "B", initial: { msg: "beta" } });

		function Show({ label }: { readonly label: string }) {
			const atom = useSessionRootStateAtom<{ msg?: string }>({});
			const state = useAtomValue(atom);
			return <span data-testid={label}>{state.msg}</span>;
		}

		render(
			<>
				<FxtrSessionProvider session={a.session}>
					<Show label="a" />
				</FxtrSessionProvider>
				<FxtrSessionProvider session={b.session}>
					<Show label="b" />
				</FxtrSessionProvider>
			</>,
		);
		expect(screen.getByTestId("a").textContent).toBe("alpha");
		expect(screen.getByTestId("b").textContent).toBe("beta");

		// Mutate A; B is unaffected.
		act(() => {
			a.session.store.set(a.userStatePrivate, { msg: "alpha-updated" });
		});
		expect(screen.getByTestId("a").textContent).toBe("alpha-updated");
		expect(screen.getByTestId("b").textContent).toBe("beta");
	});

	it("ctx threaded into a function-form write is bound to the right session", () => {
		const a = makeMockSession({ sessionId: "A" });
		const b = makeMockSession({ sessionId: "B" });

		function Probe({
			label,
			capture,
		}: {
			readonly label: string;
			readonly capture: (sessionId: string) => void;
		}) {
			const atom = useSessionRootStateAtom<{ x?: number }>({});
			const set = useSetAtom(atom);
			return (
				<button
					type="button"
					data-testid={label}
					onClick={() =>
						set((prev, ctx) => {
							capture(ctx.session.sessionId);
							return { x: (prev?.x ?? 0) + 1 };
						})
					}
				>
					go
				</button>
			);
		}

		const captured: string[] = [];
		render(
			<>
				<FxtrSessionProvider session={a.session}>
					<Probe label="a" capture={(id) => captured.push(`a→${id}`)} />
				</FxtrSessionProvider>
				<FxtrSessionProvider session={b.session}>
					<Probe label="b" capture={(id) => captured.push(`b→${id}`)} />
				</FxtrSessionProvider>
			</>,
		);
		act(() => screen.getByTestId("a").click());
		act(() => screen.getByTestId("b").click());
		expect(captured).toEqual(["a→A", "b→B"]);
	});
});
