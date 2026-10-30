/**
 * Workstation-debug-panel table for `services`. Short ids
 * rendered as `<DebugRefChip>` so users can click into the
 * service's metadata view; connection ids and other identifiers
 * remain plain monospace.
 */

import type { ServiceRecord } from "@fxtr-ui/connector-workstation";
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

export interface ServicesDebugTableProps {
	readonly records: ReadonlyArray<ServiceRecord>;
}

export function ServicesDebugTable({ records }: ServicesDebugTableProps) {
	const connector = useWorkstationConnector();
	return (
		<section style={sectionStyle}>
			<header style={headerStyle}>Services ({records.length})</header>
			{records.length === 0 ? (
				<div style={mutedStyle}>No tracked services.</div>
			) : (
				<table style={tableStyle}>
					<thead>
						<tr>
							<th style={thStyle}>short_id</th>
							<th style={thStyle}>status</th>
							<th style={thStyle}>service_type_id</th>
							<th style={thStyle}>external_names</th>
							<th style={thStyle}>registered by</th>
							<th style={thStyle}>pinned by sessions</th>
						</tr>
					</thead>
					<tbody>
						{records.map((r) => (
							<tr key={r.short_id} style={r.broken ? brokenRowStyle : undefined}>
								<td style={rowKeyStyle}>
									<DebugRefChip
										entry={{ stub: connector.serviceRef(r.short_id), display: "default" }}
									/>
								</td>
								<td style={tdStyle}>
									{r.broken ? (
										<span style={brokenChipStyle}>broken</span>
									) : (
										<span style={mutedStyle}>alive</span>
									)}
								</td>
								<td style={tdStyle}>{r.service_type_id}</td>
								<td style={tdStyle}>
									<NameList names={r.external_names} />
								</td>
								<td style={tdStyle}>
									<RegisteredBy
										connectionIds={r.registering_connection_ids ?? []}
										systemKinds={r.system_source_kinds ?? []}
									/>
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

function RegisteredBy({
	connectionIds,
	systemKinds,
}: {
	readonly connectionIds: readonly string[];
	readonly systemKinds: readonly string[];
}) {
	if (connectionIds.length === 0 && systemKinds.length === 0) {
		return <span style={mutedStyle}>—</span>;
	}
	return (
		<div style={chipListStyle}>
			{systemKinds.map((kind) => (
				<span key={`sys:${kind}`} style={systemChipStyle}>
					system:{kind}
				</span>
			))}
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

const chipListStyle: CSSProperties = {
	display: "flex",
	flexWrap: "wrap",
	gap: "4px",
};

const systemChipStyle: CSSProperties = {
	...chipStyle,
	background: "var(--accent-subtle, var(--surface-raised))",
	color: "var(--text-primary)",
	fontWeight: 600,
};

// A broken service is dead but still known: struck through, with a
// "broken" status chip, until its trackers release it.
const brokenRowStyle: CSSProperties = {
	textDecoration: "line-through",
	opacity: 0.6,
};

const brokenChipStyle: CSSProperties = {
	...chipStyle,
	background: "var(--danger-subtle, var(--surface-raised))",
	color: "var(--danger, var(--text-primary))",
	fontWeight: 600,
	textDecoration: "none",
};
