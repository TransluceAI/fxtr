/**
 * PanelErrorBoundary — catches render errors inside a dockview panel's
 * content and shows a fallback body instead of bubbling up to the
 * whole workstation.
 *
 * A class component because React only supports error boundaries as
 * classes. No external deps; the fallback is a plain div themed with
 * workstation CSS tokens.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

export interface PanelErrorBoundaryProps {
	children: ReactNode;
	/** Optional custom fallback. If omitted, a default error panel is shown. */
	fallback?: (error: Error) => ReactNode;
	/** Optional hook for logging / telemetry. */
	onError?: (error: Error, info: ErrorInfo) => void;
}

interface PanelErrorBoundaryState {
	error: Error | null;
}

export class PanelErrorBoundary extends Component<
	PanelErrorBoundaryProps,
	PanelErrorBoundaryState
> {
	state: PanelErrorBoundaryState = { error: null };

	static getDerivedStateFromError(error: Error): PanelErrorBoundaryState {
		return { error };
	}

	componentDidCatch(error: Error, info: ErrorInfo): void {
		console.error("[workstation] panel render failed", error, info);
		this.props.onError?.(error, info);
	}

	render(): ReactNode {
		const { error } = this.state;
		if (error !== null) {
			if (this.props.fallback) return this.props.fallback(error);
			return <DefaultPanelErrorFallback error={error} />;
		}
		return this.props.children;
	}
}

function DefaultPanelErrorFallback({ error }: { error: Error }) {
	return (
		<div style={rootStyle}>
			<div style={headingStyle}>Failed to render panel</div>
			<div style={messageStyle}>{error.message || String(error)}</div>
		</div>
	);
}

const rootStyle = {
	padding: "var(--sp-4)",
	fontFamily: "var(--font-prose)",
	color: "var(--text-primary)",
	display: "flex",
	flexDirection: "column" as const,
	gap: "var(--sp-2)",
} as const;

const headingStyle = {
	fontWeight: 600,
	color: "var(--status-error)",
	fontSize: "var(--text-body)",
} as const;

const messageStyle = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
	whiteSpace: "pre-wrap" as const,
	wordBreak: "break-word" as const,
	background: "var(--status-error-bg)",
	padding: "var(--sp-3)",
	borderRadius: "var(--radius-card)",
} as const;
