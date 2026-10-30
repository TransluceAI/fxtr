/**
 * Internal React error boundary used by `<RenderDispatch>` to
 * wrap the dispatched component. Catches render-time throws —
 * both genuine render errors from user components and the
 * synthetic throws used by the static dispatch helpers' `errored`
 * terminal phase — and renders the registry entry's
 * `ErrorFallback` component in their place.
 *
 * React error boundaries must be class components (the
 * `getDerivedStateFromError` / `componentDidCatch` lifecycle
 * has no functional equivalent in the public API as of React
 * 19). Everything else in this package is functional; this
 * class is the one exception.
 *
 * `<RenderDispatch>` mounts a fresh `DispatchErrorBoundary`
 * per `(stub, dispatchKey)` pair via a React `key`. When the
 * entry changes (consumer passes a different stub or key), the
 * outer key changes, React tears down the old boundary, and
 * the new boundary starts with `hasError: false`.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { Component, type ErrorInfo, type ReactNode } from "react";
import type { ErrorFallbackComponent } from "./registry_types";

interface DispatchErrorBoundaryProps<
	Stub extends EntityRefStub | ServiceRefStub,
	Props extends object,
> {
	readonly stub: Stub;
	readonly extraProps: Props;
	readonly ErrorFallback: ErrorFallbackComponent<Stub, Props>;
	readonly children: ReactNode;
}

interface DispatchErrorBoundaryState {
	readonly hasError: boolean;
	readonly error: unknown;
}

export class DispatchErrorBoundary<
	Stub extends EntityRefStub | ServiceRefStub,
	Props extends object,
> extends Component<DispatchErrorBoundaryProps<Stub, Props>, DispatchErrorBoundaryState> {
	override state: DispatchErrorBoundaryState = { hasError: false, error: null };

	// Not `override` — `getDerivedStateFromError` is an optional
	// static on `Component` and TS's strict override check rejects
	// `override` on optional inherited members.
	static getDerivedStateFromError(error: unknown): DispatchErrorBoundaryState {
		return { hasError: true, error };
	}

	// Note: not marked `override` — `Component.componentDidCatch`
	// is declared optional on the base type, and TS's strict
	// override check rejects `override` on optional inherited
	// members.
	componentDidCatch(error: unknown, errorInfo: ErrorInfo): void {
		// Log so the boundary's swallow doesn't make the error
		// invisible in dev tools. The user's `ErrorFallback` is
		// responsible for presenting the failure; we just record it
		// for the console.
		console.error("[fxtr-ui-react-dispatch] dispatched component threw:", error, errorInfo);
	}

	override render(): ReactNode {
		if (this.state.hasError) {
			const { ErrorFallback, stub, extraProps } = this.props;
			// `ErrorFallback`'s props shape is `{stub, error} & Props`;
			// spread the `extraProps` collected by `<RenderDispatch>`.
			const merged = { stub, error: this.state.error, ...extraProps } as {
				stub: Stub;
				error: unknown;
			} & Props;
			return <ErrorFallback {...merged} />;
		}
		return this.props.children;
	}
}
