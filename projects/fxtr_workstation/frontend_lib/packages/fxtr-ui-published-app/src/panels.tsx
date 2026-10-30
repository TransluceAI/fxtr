/**
 * The viewer's injected panel registrations for the PanelViewer — all
 * connector-generic (no knowledge of any particular flake's entity types):
 *
 *   - **Roots** — browses the connector's discovery lists (`listEntities` /
 *     `listServices`), rendering each as a `RefLink`. Clicking one opens a ref
 *     panel (the viewer's `ShellRefLinkImpl` routes the click to
 *     `restoreIntoNewPanel` because this panel provides no `PanelContext`).
 *   - **Storage inspector** — a stateless-connector affordance: dumps the keys in
 *     the shared `Storage` (sessions + shortId table + layout all live here),
 *     with a refresh button.
 *   - **Dispatcher debug** — renders `dispatcher.debugView()` (which carries the
 *     "reload everything" button).
 */

import type { HttpConnector, HttpEntityRecord, HttpServiceRecord } from "@fxtr-ui/connector-http";
import {
	type BreadcrumbSource,
	BreadcrumbSourceProvider,
	type PanelRegistration,
	RefLink,
	useConnector,
} from "@fxtr-ui/panel-viewer";
import { useDispatcher } from "@fxtr-ui/react-dispatch";
import type { IDockviewPanelProps } from "dockview";
import { useAtomValue } from "jotai";
import { type CSSProperties, Suspense, useMemo, useState } from "react";

export const ROOTS_PANEL_ID = "roots";
const STORAGE_INSPECTOR_PANEL_ID = "storage-inspector";
const DISPATCHER_DEBUG_PANEL_ID = "dispatcher-debug";

// Roots rows open a new panel: `currentKey: null` → length-1 trail,
// no source panel → `restoreIntoNewPanel`.
const ROW_SOURCE: BreadcrumbSource = {
	pendingBreadcrumbs: [],
	currentKey: null,
	via: "roots-panel",
};

// ---- Roots panel -----------------------------------------------------

function RootsPanel(_props: IDockviewPanelProps) {
	// These panels only mount inside this viewer's PanelViewer, which is always
	// given an HttpConnector — narrow to reach its discovery lists.
	const connector = useConnector() as HttpConnector;
	const entityList = useMemo(() => connector.listEntities(), [connector]);
	const serviceList = useMemo(() => connector.listServices(), [connector]);
	const entities = useAtomValue(entityList.records);
	const services = useAtomValue(serviceList.records);

	return (
		<div style={bodyStyle}>
			<BreadcrumbSourceProvider value={ROW_SOURCE}>
				<EntitySection records={entities} />
				<ServiceSection records={services} />
			</BreadcrumbSourceProvider>
		</div>
	);
}

function EntitySection({ records }: { readonly records: readonly HttpEntityRecord[] }) {
	const rows = useMemo(() => [...records].sort((a, b) => a.name.localeCompare(b.name)), [records]);
	return (
		<section style={sectionStyle}>
			<div style={headingStyle}>Entities</div>
			{rows.length === 0 ? (
				<div style={emptyStyle}>No discoverable entities.</div>
			) : (
				rows.map((r) => (
					<div key={`e:${r.idString}:${r.name}`} style={rowStyle}>
						<span style={nameStyle}>{r.name}</span>
						<Suspense fallback={<span style={mutedStyle}>…</span>}>
							<RefLink stub={r.ref} as="block" />
						</Suspense>
					</div>
				))
			)}
		</section>
	);
}

function ServiceSection({ records }: { readonly records: readonly HttpServiceRecord[] }) {
	const rows = useMemo(() => [...records].sort((a, b) => a.name.localeCompare(b.name)), [records]);
	return (
		<section style={sectionStyle}>
			<div style={headingStyle}>Services</div>
			{rows.length === 0 ? (
				<div style={emptyStyle}>No discoverable services.</div>
			) : (
				rows.map((r) => (
					<div key={`s:${r.name}`} style={rowStyle}>
						<span style={nameStyle}>{r.name}</span>
						<Suspense fallback={<span style={mutedStyle}>…</span>}>
							<RefLink stub={r.ref} as="block" />
						</Suspense>
					</div>
				))
			)}
		</section>
	);
}

// ---- Storage inspector panel -----------------------------------------

function makeStorageInspectorPanel(storage: Storage) {
	return function StorageInspectorPanel(_props: IDockviewPanelProps) {
		const [tick, setTick] = useState(0);
		// Recomputed each render; the Refresh button bumps `tick` to re-read
		// the live storage (cheap — a handful of keys).
		void tick;
		const entries: Array<{ readonly key: string; readonly preview: string }> = [];
		for (let i = 0; i < storage.length; i++) {
			const key = storage.key(i);
			if (key === null) continue;
			const raw = storage.getItem(key) ?? "";
			entries.push({ key, preview: raw.length > 160 ? `${raw.slice(0, 160)}…` : raw });
		}
		entries.sort((a, b) => a.key.localeCompare(b.key));

		return (
			<div style={bodyStyle}>
				<div style={inspectorHeaderStyle}>
					<span style={headingStyle}>Storage ({entries.length} keys)</span>
					<button type="button" style={refreshBtnStyle} onClick={() => setTick((t) => t + 1)}>
						Refresh
					</button>
				</div>
				{entries.length === 0 ? (
					<div style={emptyStyle}>Storage is empty.</div>
				) : (
					entries.map((e) => (
						<div key={e.key} style={storageRowStyle}>
							<div style={storageKeyStyle}>{e.key}</div>
							<div style={storageValStyle}>{e.preview}</div>
						</div>
					))
				)}
			</div>
		);
	};
}

// ---- Dispatcher debug panel ------------------------------------------

function DispatcherDebugPanel(_props: IDockviewPanelProps) {
	const dispatcher = useDispatcher();
	return (
		<div style={bodyStyle}>
			{dispatcher.debugView !== undefined ? (
				dispatcher.debugView()
			) : (
				<div style={emptyStyle}>This dispatcher exposes no debug view.</div>
			)}
		</div>
	);
}

// ---- Registrations ---------------------------------------------------

export function makePanelRegistrations(storage: Storage): PanelRegistration[] {
	return [
		{
			id: ROOTS_PANEL_ID,
			title: "Roots",
			component: RootsPanel,
			sidebarButton: { label: "Roots" },
		},
		{
			id: STORAGE_INSPECTOR_PANEL_ID,
			title: "Storage",
			component: makeStorageInspectorPanel(storage),
			sidebarButton: { label: "Storage" },
		},
		{
			id: DISPATCHER_DEBUG_PANEL_ID,
			title: "Dispatcher debug",
			component: DispatcherDebugPanel,
			sidebarButton: { label: "Dispatcher" },
		},
	];
}

// ---- styles ----------------------------------------------------------

const bodyStyle: CSSProperties = {
	height: "100%",
	overflow: "auto",
	padding: 16,
	display: "flex",
	flexDirection: "column",
	gap: 16,
};
const sectionStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 6 };
const headingStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: 11,
	letterSpacing: "0.05em",
	color: "#888",
};
const rowStyle: CSSProperties = {
	display: "grid",
	gridTemplateColumns: "minmax(110px, 0.3fr) 1fr",
	alignItems: "center",
	gap: 12,
	padding: "6px 10px",
	borderRadius: 6,
	background: "#f6f6f6",
	border: "1px solid #e3e3e3",
};
const nameStyle: CSSProperties = {
	fontFamily: "ui-monospace, monospace",
	fontSize: 12,
	color: "#555",
	overflow: "hidden",
	textOverflow: "ellipsis",
	whiteSpace: "nowrap",
};
const mutedStyle: CSSProperties = { color: "#aaa", fontSize: 12 };
const emptyStyle: CSSProperties = { color: "#999", fontStyle: "italic", fontSize: 12 };
const inspectorHeaderStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	justifyContent: "space-between",
	gap: 12,
};
const refreshBtnStyle: CSSProperties = {
	padding: "2px 10px",
	border: "1px solid #ccc",
	borderRadius: 6,
	background: "#fff",
	cursor: "pointer",
	fontSize: 12,
};
const storageRowStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: 2,
	padding: "6px 10px",
	borderRadius: 6,
	background: "#f6f6f6",
	border: "1px solid #e3e3e3",
};
const storageKeyStyle: CSSProperties = {
	fontFamily: "ui-monospace, monospace",
	fontSize: 12,
	fontWeight: 600,
	color: "#444",
};
const storageValStyle: CSSProperties = {
	fontFamily: "ui-monospace, monospace",
	fontSize: 11,
	color: "#777",
	wordBreak: "break-all",
};
