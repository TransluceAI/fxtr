/**
 * `WorkstationNavigateContext` — indirection for "navigate the
 * workstation to this stub", parallel to `RefLinkImplContext`.
 *
 * The context value is a HOOK function — a `() => (stub) => void`
 * — *not* a plain `(stub) => void` function. This lets the
 * shell's impl re-evaluate the user's current contexts
 * (`PanelContext`, `BreadcrumbSourceContext`, `WorkstationContext`,
 * `HoverCloseContext`, …) on each call site, so navigation
 * behaviour matches the call site's panel position (e.g. clicks
 * inside a ref panel do `followLink({ replace-in-source })`,
 * clicks outside any panel do `restoreIntoNewPanel`).
 *
 * The hook impl is identity-stable (a module-level function in
 * the shell), so rules-of-hooks is satisfied — the inner hook
 * call sequence is the same on every render.
 *
 * If no provider is installed, the default `missingNavigateHandler`
 * is what runs — it returns a function that throws when invoked,
 * pointing the consumer at `@fxtr-ui/workstation`'s shell.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { createContext, useContext } from "react";

/**
 * The navigation action: open the given stub in the appropriate
 * panel (the call site's current panel if any, else a new one).
 */
export type WorkstationNavigateFn = (stub: EntityRefStub | ServiceRefStub) => void;

/**
 * A `UseWorkstationNavigate` is a hook that resolves the current
 * navigation context and returns a `WorkstationNavigateFn`. The
 * default `missingNavigateHandler` returns a function that throws
 * on invocation; the workstation shell installs an impl that
 * routes through `WorkstationApi.followLink` /
 * `restoreIntoNewPanel`.
 *
 * Because the value is a hook, it can read additional React
 * contexts (the consumer's panel, breadcrumb source, etc.) at
 * the call site — letting one provider serve every position in
 * the tree without re-mounting per scope.
 */
export type UseWorkstationNavigate = () => WorkstationNavigateFn;

const missingNavigateHandler: UseWorkstationNavigate = () => () => {
	throw new Error(
		"useWorkstationNavigate: no workstation navigation handler installed. " +
			"Mount this subtree inside `@fxtr-ui/workstation`'s `<WorkstationShell>` " +
			"(or your own `<WorkstationNavigateContext.Provider>` with a hook impl).",
	);
};

/**
 * React context carrying the active `UseWorkstationNavigate` hook
 * impl. The default value throws on invocation; the shell
 * overrides via `<WorkstationNavigateContext.Provider>` with its
 * own `useShellNavigate` hook (see `@fxtr-ui/workstation`'s
 * `ShellRefLinkImpl.tsx`).
 */
export const WorkstationNavigateContext =
	createContext<UseWorkstationNavigate>(missingNavigateHandler);

/**
 * Hook returning a navigation function bound to the caller's
 * current context. Use the returned function inside event
 * handlers etc.:
 *
 *     const navigate = useWorkstationNavigate();
 *     const onClick = (e) => {
 *       e.preventDefault();
 *       e.stopPropagation();
 *       navigate(stub);
 *     };
 *
 * Implementation: reads the active `UseWorkstationNavigate`
 * hook from context and calls it. Because `useContext` returns a
 * stable function reference (the shell mounts one provider with
 * one value), the inner hook call sequence is consistent across
 * renders — rules of hooks satisfied.
 */
export function useWorkstationNavigate(): WorkstationNavigateFn {
	const useImpl = useContext(WorkstationNavigateContext);
	return useImpl();
}
