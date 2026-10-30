/**
 * Workstation-debug-panel table for `entities`. Short ids
 * rendered as `<DebugRefChip>`; `fxtr_id` shown as plain
 * monospace text for inspection.
 */

import type { EntityRecord } from "@fxtr-ui/connector-workstation";
import { DebugRefChip } from "@fxtr-ui/panel-viewer";
import type { CSSProperties } from "react";
import { useWorkstationConnector } from "../../useWorkstationConnector.js";
import {
	chipStyle,
	headerStyle,
	mutedStyle,
	rowKeyStyle,
	tableStyle,
	thStyle,
} from "./table_styles.js";

export interface EntitiesDebugTableProps {
	readonly records: ReadonlyArray<EntityRecord>;
}

export function EntitiesDebugTable({ records }: EntitiesDebugTableProps) {
	const connector = useWorkstationConnector();
	return (
		<section style={sectionStyle}>
			<header style={headerStyle}>Entities ({records.length})</header>
			{records.length === 0 ? (
				<div style={mutedStyle}>No tracked entities.</div>
			) : (
				<table style={tableStyle}>
					<thead>
						<tr>
							<th style={thStyle}>short_id</th>
							<th style={thStyle}>fxtr_id</th>
							<th style={thStyle}>external_names</th>
							<th style={thStyle}>registered by</th>
							<th style={thStyle}>pinned by sessions</th>
						</tr>
					</thead>
					<tbody>
						{records.map((r) => (
							<tr key={r.short_id}>
								<td style={rowKeyStyle}>
									<DebugRefChip
										entry={{ stub: connector.entityRef(r.short_id), display: "default" }}
									/>
								</td>
								<td style={fxtrIdStyle}>{r.fxtr_id}</td>
								<td style={tdStyle}>
									<NameList names={r.external_names} />
								</td>
								<td style={tdStyle}>
									<ConnectionList connectionIds={r.registering_connection_ids ?? []} />
								</td>
								<td style={tdStyle}>
									<SessionList sessionIds={r.pinned_by_sessions ?? []} />
								</td>
							</tr>
						))}
					</tbody>
				</table>
			)}
		</section>
	);
}

function NameList({ names }: { readonly names: readonly string[] }) {
	if (names.length === 0) return <span style={mutedStyle}>—</span>;
	return (
		<div style={chipListStyle}>
			{names.map((n) => (
				<span key={n} style={chipStyle}>
					{n}
				</span>
			))}
		</div>
	);
}

function ConnectionList({ connectionIds }: { readonly connectionIds: readonly string[] }) {
	if (connectionIds.length === 0) return <span style={mutedStyle}>—</span>;
	return (
		<div style={chipListStyle}>
			{connectionIds.map((id) => (
				<span key={id} style={chipStyle}>
					{id.slice(0, 8)}…
				</span>
			))}
		</div>
	);
}

function SessionList({ sessionIds }: { readonly sessionIds: readonly string[] }) {
	if (sessionIds.length === 0) return <span style={mutedStyle}>—</span>;
	return (
		<div style={chipListStyle}>
			{sessionIds.map((id) => (
				<span key={id} style={chipStyle}>
					{id.slice(0, 8)}…
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

const fxtrIdStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-2)",
	verticalAlign: "top",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
	maxWidth: "320px",
	overflow: "hidden",
	textOverflow: "ellipsis",
	whiteSpace: "nowrap",
};

const chipListStyle: CSSProperties = {
	display: "flex",
	flexWrap: "wrap",
	gap: "4px",
};
