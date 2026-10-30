/**
 * `DockviewLayout` — workstation dockview container.
 *
 * Registers `PanelWrapper` for `kind: "ref"` panels and (later)
 * `RootsPanel` for `kind: "roots"`. Exposes the `DockviewApi`
 * via `onReady` so callers (`PanelViewer`) can imperatively
 * add / remove / look up panels.
 *
 * Drag-drop acceptance: drops carrying `application/x-fxtr-ref`
 * are accepted via `onUnhandledDragOverEvent`. At drop time the
 * handler discriminates the payload kind and routes through the
 * workstation API:
 *
 *   - `kind: "link"` → `workstationApi.followLink(..., intent: open-new)`.
 *   - `kind: "snapshot"` → `workstationApi.restoreIntoNewPanel(state, position)`.
 *
 * Drop-region suppression: when a renderer-owned drop target inside
 * a panel is being dragged over (signalled via
 * `RefDropRegionContext`'s `isActive` flag), the dockview
 * `onUnhandledDragOverEvent` handler skips `e.accept()` so dockview
 * doesn't show its "open a panel" overlay. The renderer's own drop
 * handler then receives the drop. We mirror the latest context value
 * into a ref so the imperative dockview callback can read it without
 * re-subscribing on every counter bump.
 */

import { RefDropRegionContext } from "@fxtr-ui/workstation-dispatch";
import {
	type DockviewDidDropEvent,
	DockviewReact,
	type DockviewReadyEvent,
	type IDockviewPanelProps,
	positionToDirection,
} from "dockview";
import { type FunctionComponent, useCallback, useContext, useEffect, useMemo, useRef } from "react";
import { breadcrumbOfDragData } from "../breadcrumb/types.js";
import { PanelWrapper } from "../panel/PanelWrapper.js";
import { REF_PANEL_COMPONENT } from "../panel/RefPanelParams.js";
import { RefPanelTab } from "../panel/RefPanelTab.js";
import {
	DRAG_MIME_FXTR_REF,
	parseDragData,
	refPanelChainStateOfDragData,
} from "../reflink/drag.js";
import { entryOfDragData } from "../types.js";
import { useConnector } from "./ConnectorContext.js";
import type { PanelRegistration } from "./PanelRegistration.js";
import type { DropPosition, PanelViewerApi } from "./PanelViewerApi.js";

export interface DockviewLayoutProps {
	readonly onReady: (event: DockviewReadyEvent) => void;
	/** Workstation API used to route drops into the right method. */
	readonly workstationApi: PanelViewerApi;
	/**
	 * Injected non-ref panels. The built-in ref panel (`PanelWrapper`)
	 * is always registered; these add the app's roots / debug / etc.
	 * components without the layout importing them directly.
	 */
	readonly panelRegistrations: readonly PanelRegistration[];
	readonly className?: string;
}

export function DockviewLayout(props: DockviewLayoutProps) {
	const { onReady, workstationApi, panelRegistrations } = props;
	const connector = useConnector();

	// The built-in ref panel is always present; the app's registrations
	// supply the rest. Memoized on the (stable) registration array so the
	// `components` map handed to dockview keeps a steady identity.
	const components = useMemo(() => {
		// biome-ignore lint/suspicious/noExplicitAny: dockview erases panel param shapes
		const map: Record<string, FunctionComponent<IDockviewPanelProps<any>>> = {
			// biome-ignore lint/suspicious/noExplicitAny: dockview erases panel param shapes
			[REF_PANEL_COMPONENT]: PanelWrapper as FunctionComponent<IDockviewPanelProps<any>>,
		};
		for (const reg of panelRegistrations) map[reg.id] = reg.component;
		return map;
	}, [panelRegistrations]);

	// Keep the latest api in a ref so our dockview subscriptions
	// don't need to re-subscribe when it changes identity.
	const apiPropRef = useRef(workstationApi);
	useEffect(() => {
		apiPropRef.current = workstationApi;
	}, [workstationApi]);

	// Mirror the active-drop-region flag into a ref so the
	// imperative dockview drag-over callback can read it without
	// re-binding each time the counter changes. Reading via
	// `useContext` (not `useRefDropRegion`) avoids per-render
	// allocations for handlers we don't use here — we only need
	// the flag.
	const dropRegionCtx = useContext(RefDropRegionContext);
	const dropRegionActiveRef = useRef(false);
	dropRegionActiveRef.current = dropRegionCtx?.isActive ?? false;

	const handleReady = useCallback(
		(event: DockviewReadyEvent) => {
			event.api.onUnhandledDragOverEvent((e) => {
				// Defer to renderer-owned drop targets: when one
				// is currently being dragged over, do not call
				// `e.accept()`, so dockview's drop overlay stays
				// hidden and the drop falls through to the
				// renderer's own handler.
				if (dropRegionActiveRef.current) return;
				const types = e.nativeEvent.dataTransfer?.types;
				if (types && Array.from(types).includes(DRAG_MIME_FXTR_REF)) {
					e.accept();
				}
			});
			onReady(event);
		},
		[onReady],
	);

	const onDidDrop = useCallback(
		(event: DockviewDidDropEvent) => {
			const dt = event.nativeEvent.dataTransfer;
			if (dt === null) return;
			const payload = parseDragData(dt);
			if (payload === null) return;

			const direction = positionToDirection(event.position);
			const position: DropPosition = {
				...(event.group !== undefined ? { referenceGroup: event.group } : {}),
				...(direction !== "within" ? { direction } : {}),
			};

			const api = apiPropRef.current;

			if (payload.kind === "snapshot") {
				// Lift the wire-safe chain summary back to a live
				// `RefPanelChainState` by re-interning each stub
				// through the connector.
				const newPanelState = refPanelChainStateOfDragData(payload.newPanelState, connector);
				api.restoreIntoNewPanel(newPanelState, position);
				return;
			}

			// payload.kind === "link"
			if (payload.sourcePanelId === undefined) {
				// A content-ref drag that was packed as `link` but has no
				// source panel id — shouldn't normally happen because non-
				// panel origins pack `SnapshotDragPayload` at drag-start.
				// Defensive: drop silently.
				console.warn("[fxtr-ui] received link drag with no sourcePanelId; ignoring", payload);
				return;
			}
			api.followLink({
				target: entryOfDragData(payload.target, connector),
				trail: payload.trail.map((b) => breadcrumbOfDragData(b, connector)),
				sourcePanelId: payload.sourcePanelId,
				intent: { kind: "open-new", position },
			});
		},
		[connector],
	);

	return (
		<DockviewReact
			className={props.className ?? "fw-dockview-theme"}
			components={components}
			defaultTabComponent={RefPanelTab}
			onReady={handleReady}
			onDidDrop={onDidDrop}
		/>
	);
}
