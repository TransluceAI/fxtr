/**
 * `RootsPanel` — dockview content renderer showing the
 * workstation's externally-named entities and services.
 *
 * Data source: `connector.listEntities()` /
 * `connector.listServices()`. Each record carries a
 * `external_names: readonly string[]` array; a single record can
 * appear under multiple names, in which case we render one row
 * per `(name, record)` pair. Records with no external names are
 * filtered out — they're internal / RPC-only registrations the
 * user can't address.
 *
 * Layout:
 *
 *   ┌────────────────────────────────────────────────────────┐
 *   │ ENTITIES                                               │
 *   │   demo/notes      ▣ link  da1b2c                       │
 *   │   demo/section    ▣ link  d3e4f5                       │
 *   │                                                        │
 *   │ SERVICES                                               │
 *   │   demo/svc        ▣ service  s1                        │
 *   └────────────────────────────────────────────────────────┘
 *
 * Each row wraps in a single `BreadcrumbSourceProvider` with
 * `currentKey: null, via: "roots-panel"` so a click produces a
 * length-1 trail with no from-segment. The panel intentionally
 * does NOT provide `PanelContext` — clicks from roots rows
 * therefore see `usePanelContext() === null` and route through
 * `restoreIntoNewPanel(computeSnapshotFromTrail(...))` rather
 * than `followLink(replace-in-source)`, which is correct: rows
 * always open a new ref panel.
 *
 * Stateless: the roots panel has no session of its own.
 * Information panels at workstation level don't need per-panel
 * persistent state, and skipping the session machinery keeps
 * them out of `PanelSessionRegistry`, out of the
 * shell-mints-and-detaches lifecycle, and out of the
 * pending/unavailable code paths.
 */

import type {
	EntityRecord,
	ServiceRecord,
	WorkstationConnector,
} from "@fxtr-ui/connector-workstation";
import { BreadcrumbSourceProvider } from "@fxtr-ui/panel-viewer";
import type { BreadcrumbSource } from "@fxtr-ui/panel-viewer";
import type { StubKind } from "@fxtr-ui/panel-viewer";
import { RefLink } from "@fxtr-ui/workstation-dispatch";
import type { IDockviewPanelProps } from "dockview";
import { useAtomValue } from "jotai";
import { type CSSProperties, Suspense, useMemo } from "react";
import { useWorkstationConnector } from "../useWorkstationConnector.js";
import type { RootsPanelParams } from "./RootsPanelParams.js";

const ROW_SOURCE: BreadcrumbSource = {
	pendingBreadcrumbs: [],
	currentKey: null,
	via: "roots-panel",
};

interface Row {
	readonly name: string;
	readonly shortId: string;
}

function extractRows(records: ReadonlyArray<EntityRecord | ServiceRecord>): ReadonlyArray<Row> {
	const rows: Row[] = [];
	for (const record of records) {
		for (const name of record.external_names) {
			rows.push({ name, shortId: record.short_id });
		}
	}
	rows.sort((a, b) => a.name.localeCompare(b.name));
	return rows;
}

export function RootsPanel(_props: IDockviewPanelProps<RootsPanelParams>) {
	const connector = useWorkstationConnector();
	return <RootsPanelBody connector={connector} />;
}

function RootsPanelBody({ connector }: { readonly connector: WorkstationConnector }) {
	const entityList = useMemo(() => connector.listEntities(), [connector]);
	const serviceList = useMemo(() => connector.listServices(), [connector]);

	const entities = useAtomValue(entityList.records);
	const services = useAtomValue(serviceList.records);

	const entityRows = useMemo(() => extractRows(entities), [entities]);
	const serviceRows = useMemo(() => extractRows(services), [services]);

	return (
		<div className="fw-panel__body" style={bodyStyle}>
			<BreadcrumbSourceProvider value={ROW_SOURCE}>
				<Section title="Entities" rows={entityRows} kind="entity" connector={connector} />
				<Section title="Services" rows={serviceRows} kind="service" connector={connector} />
			</BreadcrumbSourceProvider>
		</div>
	);
}

function Section({
	title,
	rows,
	kind,
	connector,
}: {
	readonly title: string;
	readonly rows: ReadonlyArray<Row>;
	readonly kind: StubKind;
	readonly connector: WorkstationConnector;
}) {
	return (
		<div style={sectionStyle}>
			<div style={sectionHeadingStyle}>{title}</div>
			{rows.length === 0 ? (
				<div style={emptyStyle}>
					No externally-named {kind === "entity" ? "entities" : "services"}.
				</div>
			) : (
				<ul style={listStyle}>
					{rows.map((row) => (
						<Row
							key={`${kind}:${row.shortId}:${row.name}`}
							row={row}
							kind={kind}
							connector={connector}
						/>
					))}
				</ul>
			)}
		</div>
	);
}

function Row({
	row,
	kind,
	connector,
}: {
	readonly row: Row;
	readonly kind: StubKind;
	readonly connector: WorkstationConnector;
}) {
	const stub = useMemo(
		() =>
			kind === "entity" ? connector.entityRef(row.shortId) : connector.serviceRef(row.shortId),
		[connector, kind, row.shortId],
	);
	return (
		<li style={rowStyle}>
			<span style={nameStyle}>{row.name}</span>
			<div style={refCellStyle}>
				<Suspense fallback={<span style={mutedStyle}>…</span>}>
					<RefLink stub={stub} as="block" />
				</Suspense>
			</div>
		</li>
	);
}

const bodyStyle: CSSProperties = {
	height: "100%",
	overflow: "auto",
	padding: "var(--sp-4)",
	fontFamily: "var(--font-prose)",
	color: "var(--text-primary)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-4)",
};

const sectionStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2)",
};

const sectionHeadingStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-muted)",
	fontFamily: "var(--font-prose)",
};

const listStyle: CSSProperties = {
	listStyle: "none",
	margin: 0,
	padding: 0,
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2)",
};

const rowStyle: CSSProperties = {
	display: "grid",
	gridTemplateColumns: "minmax(120px, 0.3fr) 1fr",
	alignItems: "center",
	gap: "var(--sp-4)",
	padding: "var(--sp-2) var(--sp-3)",
	borderRadius: "var(--radius-card)",
	background: "var(--surface-sunken)",
	border: "1px solid var(--border-subtle)",
};

const nameStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
	overflow: "hidden",
	textOverflow: "ellipsis",
	whiteSpace: "nowrap",
};

const refCellStyle: CSSProperties = {
	minWidth: 0,
};

const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontSize: "var(--text-body-sm)",
};

const emptyStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontSize: "var(--text-body-sm)",
	fontStyle: "italic",
	padding: "var(--sp-2) var(--sp-3)",
};
