/**
 * Workstation-debug-panel table for `rpc_connections`.
 *
 * Read-only display of the four debug-mode fields. Used inside
 * `WorkstationDebugPanel`. No `DebugRefChip` here — connection ids
 * aren't entity / service short ids, so they're shown as plain
 * monospace text.
 */

import type { RpcConnectionRecord } from "@fxtr-ui/connector-workstation";
import type { CSSProperties } from "react";
import {
	chipStyle,
	headerStyle,
	mutedStyle,
	rowKeyStyle,
	tableStyle,
	thStyle,
} from "./table_styles.js";

export interface RpcConnectionsDebugTableProps {
	readonly records: ReadonlyArray<RpcConnectionRecord>;
}

export function RpcConnectionsDebugTable({ records }: RpcConnectionsDebugTableProps) {
	return (
		<section style={sectionStyle}>
			<header style={headerStyle}>RPC Connections ({records.length})</header>
			{records.length === 0 ? (
				<div style={mutedStyle}>No active connections.</div>
			) : (
				<table style={tableStyle}>
					<thead>
						<tr>
							<th style={thStyle}>connection_id</th>
							<th style={thStyle}>name</th>
							<th style={thStyle}>peer_addr</th>
							<th style={thStyle}>connected_at</th>
							<th style={thStyle}>service regs</th>
							<th style={thStyle}>entity regs</th>
						</tr>
					</thead>
					<tbody>
						{records.map((r) => (
							<tr key={r.connection_id}>
								<td style={rowKeyStyle}>{r.connection_id.slice(0, 8)}…</td>
								<td style={tdStyle}>{r.name ?? <span style={mutedStyle}>—</span>}</td>
								<td style={tdStyle}>{r.peer_addr ?? <span style={mutedStyle}>—</span>}</td>
								<td style={tdStyle}>{r.connected_at}</td>
								<td style={tdStyle}>
									<ChipList items={r.service_registration_short_ids ?? []} />
								</td>
								<td style={tdStyle}>
									<ChipList items={r.entity_registration_short_ids ?? []} />
								</td>
							</tr>
						))}
					</tbody>
				</table>
			)}
		</section>
	);
}

function ChipList({ items }: { readonly items: readonly string[] }) {
	if (items.length === 0) return <span style={mutedStyle}>—</span>;
	return (
		<div style={chipListStyle}>
			{items.map((id) => (
				<span key={id} style={chipStyle}>
					{id}
				</span>
			))}
		</div>
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

const chipListStyle: CSSProperties = {
	display: "flex",
	flexWrap: "wrap",
	gap: "4px",
};
