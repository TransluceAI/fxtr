/**
 * <FxtrSubscope> component — creates a child scope for cache isolation
 * or scope bridging.
 *
 * When this component unmounts, its child scope is cleaned up and all
 * entities loaded within it are released. A fresh context value is created
 * for the subtree, ensuring cache isolation from the parent.
 *
 * Use `include` to bridge external scopes (e.g., from useFxtrScope)
 * into the component tree. The child scope is recreated if the include
 * set changes.
 */

import { type FxtrScope, createFxtrScope } from "@fxtr/core";
import { type ReactNode, useContext, useEffect, useRef } from "react";
import { FxtrContext, type FxtrContextValue, createFxtrContextValue } from "./context.js";

interface SubscopeState {
	ctx: FxtrContextValue;
	parents: FxtrScope[];
}

export function FxtrSubscope({
	include,
	children,
}: {
	include?: FxtrScope[];
	children: ReactNode;
}) {
	const parentCtx = useContext(FxtrContext);
	if (!parentCtx) {
		throw new Error("<FxtrSubscope> must be used within a <FxtrProvider>");
	}

	const allParents = [parentCtx.scope, ...(include ?? [])];
	const stateRef = useRef<SubscopeState | null>(null);

	// Check if parents changed (by reference identity of each scope).
	const current = stateRef.current;
	const parentsChanged =
		!current ||
		current.parents.length !== allParents.length ||
		current.parents.some((p, i) => p !== allParents[i]);

	if (parentsChanged) {
		current?.ctx.scope.scheduleClose();
		stateRef.current = {
			ctx: createFxtrContextValue(
				createFxtrScope({ name: "fxtr-react-subscope", include: allParents }),
			),
			parents: allParents,
		};
	}

	const state = stateRef.current;
	if (!state) {
		throw new Error("FxtrSubscope: internal error — scope not initialized");
	}

	// Clean up on unmount.
	useEffect(() => {
		return () => {
			stateRef.current?.ctx.scope.scheduleClose();
			stateRef.current = null;
		};
	}, []);

	return <FxtrContext.Provider value={state.ctx}>{children}</FxtrContext.Provider>;
}
