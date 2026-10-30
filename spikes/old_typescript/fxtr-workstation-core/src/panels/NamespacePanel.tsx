/**
 * NamespacePanel — dockview content renderer showing the namespace's
 * pins as a two-column list.
 *
 * Left column: pin name.
 * Right column: `<EntityLink entity={id} as="block" />`, which composes
 * the BlockLink dispatch with the interaction hooks so the row is
 * clickable / hoverable / draggable / right-clickable.
 *
 * Each row wraps in a BreadcrumbSource with `currentKey: null, via:
 * "namespace-panel"` so a click produces a length-1 trail
 * `[{ to: id, via: "namespace-panel" }]` with no from-segment. The
 * panel itself intentionally does NOT provide PanelContext — clicks
 * from namespace rows therefore land with `sourcePanelId: undefined`,
 * so `navigate()` opens a new panel (rather than replacing the
 * namespace panel, which we never want).
 */

import type { FxtrID, PromiseOrEager } from "@fxtr/core";
import type { IDockviewPanelProps } from "dockview";
import { Suspense, useEffect, useState } from "react";
import { EntityLink } from "../EntityLink.js";
import { BreadcrumbSourceProvider } from "../breadcrumb/BreadcrumbSourceContext.js";
import type { BreadcrumbSource } from "../breadcrumb/types.js";
import { useNamespace } from "../workstation/NamespaceContext.js";

/**
 * NamespacePanel takes no params: the namespace itself is supplied via
 * `NamespaceContext` so we don't have to round-trip a class instance
 * through dockview's JSON persistence.
 */
export type NamespacePanelParams = Record<string, never>;

const ROW_SOURCE: BreadcrumbSource = {
	pendingBreadcrumbs: [],
	currentKey: null,
	via: "namespace-panel",
};

export function NamespacePanel(_props: IDockviewPanelProps<NamespacePanelParams>) {
	const namespace = useNamespace();
	const [pins, setPins] = useState<Array<[string, FxtrID]> | null>(null);
	const [error, setError] = useState<unknown>(null);

	useEffect(() => {
		let cancelled = false;
		setPins(null);
		setError(null);
		const result: PromiseOrEager<ReadonlyMap<string, FxtrID>> = namespace.listPins();
		Promise.resolve(result).then(
			(map) => {
				if (cancelled) return;
				// retrieve() is needed for subsequent loads inside this scope;
				// mirrors the approach in the example-dev App. For the pin
				// list alone we don't need it yet — we only render EntityLinks,
				// which feed through `<Render>` and go via the scope's
				// existing discovery path. If that turns out to be lazy,
				// we'll add retrieve() calls here.
				const entries = Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
				setPins(entries);
			},
			(err) => {
				if (cancelled) return;
				setError(err);
			},
		);
		return () => {
			cancelled = true;
		};
	}, [namespace]);

	if (error) {
		return (
			<div className="fw-panel__body" style={bodyStyle}>
				<div style={errorStyle}>Failed to list pins: {String(error)}</div>
			</div>
		);
	}
	if (!pins) {
		return (
			<div className="fw-panel__body" style={bodyStyle}>
				<div style={mutedStyle}>Loading pins…</div>
			</div>
		);
	}
	if (pins.length === 0) {
		return (
			<div className="fw-panel__body" style={bodyStyle}>
				<div style={mutedStyle}>This namespace has no pins.</div>
			</div>
		);
	}

	return (
		<div className="fw-panel__body" style={bodyStyle}>
			<BreadcrumbSourceProvider value={ROW_SOURCE}>
				<ul style={listStyle}>
					{pins.map(([name, id]) => (
						<li key={`${name}:${id.idString()}`} style={rowStyle}>
							<span style={nameStyle}>{name}</span>
							<div style={refCellStyle}>
								<Suspense fallback={<span style={mutedStyle}>…</span>}>
									<EntityLink entity={id} as="block" />
								</Suspense>
							</div>
						</li>
					))}
				</ul>
			</BreadcrumbSourceProvider>
		</div>
	);
}

const bodyStyle = {
	height: "100%",
	overflow: "auto",
	padding: "var(--sp-4)",
	fontFamily: "var(--font-prose)",
	color: "var(--text-primary)",
} as const;

const listStyle = {
	listStyle: "none",
	margin: 0,
	padding: 0,
	display: "flex",
	flexDirection: "column" as const,
	gap: "var(--sp-3)",
};

const rowStyle = {
	display: "grid",
	gridTemplateColumns: "minmax(120px, 0.3fr) 1fr",
	alignItems: "center",
	gap: "var(--sp-4)",
	padding: "var(--sp-2) var(--sp-3)",
	borderRadius: "var(--radius-card)",
	background: "var(--surface-sunken)",
	border: "1px solid var(--border-subtle)",
};

const nameStyle = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
	overflow: "hidden",
	textOverflow: "ellipsis",
	whiteSpace: "nowrap" as const,
};

const refCellStyle = {
	minWidth: 0,
};

const mutedStyle = {
	color: "var(--text-muted)",
	fontSize: "var(--text-body-sm)",
};

const errorStyle = {
	color: "var(--status-error)",
	background: "var(--status-error-bg)",
	padding: "var(--sp-3)",
	borderRadius: "var(--radius-card)",
	fontSize: "var(--text-body-sm)",
};
