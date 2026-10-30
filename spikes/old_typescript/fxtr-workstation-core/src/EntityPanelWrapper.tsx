/**
 * EntityPanelWrapper — dockview content renderer for an entity panel.
 *
 * Reads `{ chain, activeIndex, historyRoot }` from params, hydrates the
 * active FxtrID, and renders:
 *
 *   ┌─────────────────────────────┐
 *   │ BreadcrumbBar               │  ← chain + history-root icon
 *   ├─────────────────────────────┤
 *   │ <Render as={FullPanel}>     │  ← renderer for chain[activeIndex]
 *   └─────────────────────────────┘
 *
 * Wraps `<Render>` in:
 *   - PanelContext: descendants know which panel they're in + the
 *     full chain/activeIndex/historyRoot state. Drag hooks read this
 *     at drag-start.
 *   - BreadcrumbSource: seeds the trail with `currentKey = active entity`
 *     so nested refs produce `{ from: active, to: target, via: "panel-ref" }`.
 *   - PanelErrorBoundary: downstream renderer crashes don't take out
 *     the whole workstation.
 *
 * Before mounting `<Render>` we run `scope.lift(activeEntity)` + pre-
 * check `scope.isTracked(activeEntity)`. Untracked entities (typically
 * a restore survivor whose server-side data is gone) still show the
 * breadcrumb bar (so the user can navigate back into a tracked
 * ancestor) but the body renders a friendly "not available" message.
 *
 * The inner `<Render>` is keyed on the active entity's idString so
 * `updateParameters({ chain, activeIndex, historyRoot })` triggers a
 * clean remount when the user clicks a link (chain grows) or clicks a
 * breadcrumb entry (activeIndex shifts).
 */

import { FxtrID } from "@fxtr/core";
import { useFxtrScope } from "@fxtr/react";
import { Render } from "@fxtr/react-dispatch";
import type { IDockviewPanelProps } from "dockview";
import { Suspense, useCallback, useEffect, useMemo, useRef } from "react";
import { BreadcrumbBar } from "./breadcrumb/BreadcrumbBar.js";
import { BreadcrumbSourceProvider } from "./breadcrumb/BreadcrumbSourceContext.js";
import type { BreadcrumbSource, EntityPanelParams } from "./breadcrumb/types.js";
import { PanelErrorBoundary } from "./fallbacks/PanelErrorBoundary.js";
import { FullPanel } from "./keys.js";
import { shortId } from "./shortId.js";
import { PanelContextProvider } from "./workstation/PanelContext.js";

export type { EntityPanelParams };

export function EntityPanelWrapper(props: IDockviewPanelProps<EntityPanelParams>) {
	const { chain, activeIndex, historyRoot } = props.params;
	const api = props.api;
	const scope = useFxtrScope();

	const activeIdString = chain[activeIndex];
	if (activeIdString === undefined) {
		// Defensive: malformed params. Render a minimal error body
		// rather than throwing inside dockview's renderer.
		return <MalformedParamsBody />;
	}

	const entity = useMemo(() => FxtrID.fromIdString(activeIdString), [activeIdString]);

	const isTracked = useMemo(() => {
		try {
			scope.lift(entity);
		} catch (err) {
			console.warn("[workstation] scope.lift threw for", activeIdString, err);
		}
		return scope.isTracked(entity);
	}, [scope, entity, activeIdString]);

	const titleRef = useRef<string>(shortId(entity));
	const lastEntityRef = useRef<FxtrID>(entity);
	if (lastEntityRef.current !== entity) {
		lastEntityRef.current = entity;
		titleRef.current = shortId(entity);
	}

	const setPanelTitle = useCallback((title: string) => {
		titleRef.current = title;
	}, []);

	useEffect(() => {
		api.setTitle(titleRef.current);
	});

	const source = useMemo<BreadcrumbSource>(
		() => ({ pendingBreadcrumbs: [], currentKey: entity, via: "panel-ref" }),
		[entity],
	);

	return (
		<div className="fw-panel" style={panelStyle}>
			<PanelContextProvider
				value={{ panelId: api.id, kind: "entity", chain, activeIndex, historyRoot }}
			>
				<BreadcrumbBar
					panelId={api.id}
					chain={chain}
					activeIndex={activeIndex}
					historyRoot={historyRoot}
				/>
				<div style={bodyWrapperStyle}>
					<BreadcrumbSourceProvider value={source}>
						{isTracked ? (
							<PanelErrorBoundary>
								<Suspense fallback={<FallbackLoading />}>
									<Render
										key={activeIdString}
										entity={entity}
										as={FullPanel}
										setPanelTitle={setPanelTitle}
									/>
								</Suspense>
							</PanelErrorBoundary>
						) : (
							<EntityNotTrackedBody entityIdString={activeIdString} />
						)}
					</BreadcrumbSourceProvider>
				</div>
			</PanelContextProvider>
		</div>
	);
}

function FallbackLoading() {
	return <div style={loadingStyle}>Loading entity…</div>;
}

function MalformedParamsBody() {
	return (
		<div style={notTrackedRootStyle}>
			<div style={notTrackedHeadingStyle}>Panel params malformed</div>
			<div style={notTrackedBodyStyle}>
				This panel's chain / activeIndex is invalid. It may have been restored from a corrupted
				layout.
			</div>
		</div>
	);
}

function EntityNotTrackedBody({ entityIdString }: { entityIdString: string }) {
	return (
		<div style={notTrackedRootStyle}>
			<div style={notTrackedHeadingStyle}>Entity not available</div>
			<div style={notTrackedBodyStyle}>
				The entity <code style={notTrackedIdStyle}>{entityIdString}</code> isn't present in this
				namespace. It may have been removed on the server since this panel was last saved, or it was
				never resolvable from this namespace's scope.
			</div>
		</div>
	);
}

const panelStyle = {
	height: "100%",
	display: "flex",
	flexDirection: "column" as const,
} as const;

const bodyWrapperStyle = {
	flex: "1 1 auto",
	minHeight: 0,
	display: "flex",
	flexDirection: "column" as const,
} as const;

const loadingStyle = {
	padding: "var(--sp-5, 16px)",
	color: "var(--text-muted, #888)",
	fontSize: "12px",
} as const;

const notTrackedRootStyle = {
	padding: "var(--sp-4)",
	fontFamily: "var(--font-prose)",
	color: "var(--text-primary)",
	display: "flex",
	flexDirection: "column" as const,
	gap: "var(--sp-2)",
} as const;

const notTrackedHeadingStyle = {
	fontWeight: 600,
	color: "var(--text-primary)",
	fontSize: "var(--text-body)",
} as const;

const notTrackedBodyStyle = {
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
	lineHeight: 1.5,
} as const;

const notTrackedIdStyle = {
	fontFamily: "var(--font-mono)",
	background: "var(--surface-inset)",
	padding: "0 var(--sp-1)",
	borderRadius: "var(--radius-badge)",
} as const;
