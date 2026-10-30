/**
 * Top-of-page status bar. Shows the workstation URL and a colored
 * badge for the underlying connection's status. Reads
 * `connectionStatus` via jotai against the connector's store (the
 * surrounding `<JotaiProvider store={connector.store}>` in App).
 */

import type { ConnectionStatus, WorkstationConnector } from "@fxtr-ui/connector-workstation";
import { useAtomValue } from "jotai";

const STATUS_COLORS: Record<ConnectionStatus, string> = {
	connecting: "#e0a800",
	open: "#28a745",
	reconnecting: "#e07700",
	closed: "#dc3545",
};

export function ConnectionStatusBar({
	connector,
	url,
	debugUiUrl,
}: {
	readonly connector: WorkstationConnector;
	readonly url: string;
	readonly debugUiUrl: string | null;
}) {
	const status = useAtomValue(connector.connectionStatus);
	return (
		<div
			style={{
				display: "flex",
				alignItems: "center",
				gap: "8px",
				padding: "8px 12px",
				borderBottom: "1px solid #ddd",
				background: "#fff",
			}}
		>
			<span
				style={{
					display: "inline-block",
					width: "10px",
					height: "10px",
					borderRadius: "50%",
					background: STATUS_COLORS[status],
				}}
			/>
			<span style={{ fontWeight: 600 }}>{status}</span>
			<span style={{ color: "#666" }}>·</span>
			<span style={{ color: "#666", fontFamily: "monospace", fontSize: "11px" }}>{url}</span>
			{debugUiUrl ? (
				<>
					<span style={{ color: "#666" }}>·</span>
					<a href={debugUiUrl} target="_blank" rel="noreferrer" style={{ fontSize: "11px" }}>
						debug UI ↗
					</a>
				</>
			) : null}
		</div>
	);
}
