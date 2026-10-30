/**
 * `ShellHeader` — always-rendered top bar above the sidebar +
 * dockview row.
 *
 * Surfaces the underlying connector's transport status (colored
 * dot + status text) so socket drops aren't silent, plus the
 * workstation URL and an optional link to the workstation
 * server's debug UI. Reads `connector.connectionStatus` via
 * jotai so it re-renders on every transport-state change.
 *
 * Status palette mirrors `@fxtr-ui/demo`'s `ConnectionStatusBar`:
 *
 *   - `connecting` — amber (transitional)
 *   - `open`       — green (healthy)
 *   - `reconnecting` — orange (transient drop)
 *   - `closed`     — red (terminal)
 */

import type { ConnectionStatus, Connector } from "@fxtr-ui/core";
import { useAtomValue } from "jotai";
import type { CSSProperties } from "react";

const STATUS_COLORS: Record<ConnectionStatus, string> = {
	connecting: "#e0a800",
	open: "#28a745",
	reconnecting: "#e07700",
	closed: "#dc3545",
};

export interface ShellHeaderProps {
	readonly connector: Connector;
	/**
	 * Optional URL of the workstation server's debug UI. When set,
	 * the header renders a "debug UI" link that opens in a new tab.
	 * Consumers typically derive this from the workstation
	 * `/client` URL.
	 */
	readonly debugUiUrl?: string;
}

export function ShellHeader({ connector, debugUiUrl }: ShellHeaderProps) {
	const status = useAtomValue(connector.connectionStatus);
	return (
		<div style={headerStyle}>
			<span
				style={{
					...statusDotStyle,
					background: STATUS_COLORS[status],
				}}
				aria-hidden="true"
			/>
			<span style={statusTextStyle}>{status}</span>
			<span style={dividerStyle}>·</span>
			<span style={urlStyle}>{connector.url}</span>
			{debugUiUrl !== undefined ? (
				<>
					<span style={dividerStyle}>·</span>
					<a href={debugUiUrl} target="_blank" rel="noopener noreferrer" style={linkStyle}>
						debug UI ↗
					</a>
				</>
			) : null}
		</div>
	);
}

const headerStyle: CSSProperties = {
	flex: "0 0 auto",
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-1, 4px) var(--sp-3, 12px)",
	background: "var(--surface-sunken, #f4f4f4)",
	borderBottom: "1px solid var(--border-subtle, #e0e0e0)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
};

const statusDotStyle: CSSProperties = {
	display: "inline-block",
	width: "10px",
	height: "10px",
	borderRadius: "50%",
	flex: "0 0 auto",
};

const statusTextStyle: CSSProperties = {
	fontWeight: 600,
	color: "var(--text-primary)",
};

const dividerStyle: CSSProperties = {
	color: "var(--border-subtle, #ccc)",
};

const urlStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontFamily: "var(--font-mono)",
	overflow: "hidden",
	textOverflow: "ellipsis",
	whiteSpace: "nowrap",
};

const linkStyle: CSSProperties = {
	color: "var(--text-secondary, #555)",
	textDecoration: "none",
};
