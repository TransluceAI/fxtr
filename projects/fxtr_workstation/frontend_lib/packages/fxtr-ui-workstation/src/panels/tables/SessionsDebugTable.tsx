/**
 * Workstation-debug-panel table for `sessions`. Each row has an
 * "open debug" button that opens a per-session debug panel
 * (chunk 7) — caller passes the open callback as a prop so this
 * file stays focused on rendering.
 */

import type { SessionRecord } from "@fxtr-ui/connector-workstation";
import type { CSSProperties } from "react";
import { headerStyle, mutedStyle, rowKeyStyle, tableStyle, thStyle } from "./table_styles.js";

export interface SessionsDebugTableProps {
	readonly records: ReadonlyArray<SessionRecord>;
	readonly onOpenSessionDebug: (sessionId: string) => void;
}

export function SessionsDebugTable({ records, onOpenSessionDebug }: SessionsDebugTableProps) {
	return (
		<section style={sectionStyle}>
			<header style={headerStyle}>Sessions ({records.length})</header>
			{records.length === 0 ? (
				<div style={mutedStyle}>No active sessions.</div>
			) : (
				<table style={tableStyle}>
					<thead>
						<tr>
							<th style={thStyle}>session_id</th>
							<th style={thStyle}>metadata</th>
							<th style={thStyle}>created_at</th>
							<th style={thStyle} aria-label="actions" />
						</tr>
					</thead>
					<tbody>
						{records.map((r) => (
							<tr key={r.session_id}>
								<td style={rowKeyStyle} title={r.session_id}>
									{r.session_id.slice(0, 8)}…
								</td>
								<td style={metadataStyle}>{JSON.stringify(r.metadata)}</td>
								<td style={tdStyle}>{r.created_at}</td>
								<td style={actionsCellStyle}>
									<button
										type="button"
										style={openButtonStyle}
										onClick={() => onOpenSessionDebug(r.session_id)}
									>
										open debug
									</button>
								</td>
							</tr>
						))}
					</tbody>
				</table>
			)}
		</section>
	);
}

const sectionStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
};

const tdStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-2)",
	verticalAlign: "top",
};

const metadataStyle: CSSProperties = {
	...tdStyle,
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
	maxWidth: "360px",
	overflow: "hidden",
	textOverflow: "ellipsis",
	whiteSpace: "nowrap",
};

const actionsCellStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-2)",
	verticalAlign: "top",
	textAlign: "right",
};

const openButtonStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	cursor: "pointer",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
};
