/**
 * DockviewLayout — workstation dockview container.
 *
 * Registers the panel-content renderers ("entity", "namespace") and
 * exposes the DockviewApi via `onReady`. Callers (WorkstationShell) use
 * the api to imperatively add/remove panels.
 *
 * Drag-drop acceptance: drops carrying `application/x-fxtr-entity` are
 * accepted via `onUnhandledDragOverEvent`. At drop time the handler
 * discriminates the payload kind and routes to the right API method:
 *
 * - `kind: "link"` → `workstationApi.followLink({ ..., intent: open-new + position })`.
 * - `kind: "snapshot"` → `workstationApi.restoreIntoNewPanel(state, position)`.
 */

import { FxtrID } from "@fxtr/core";
import {
	type DockviewDidDropEvent,
	DockviewReact,
	type DockviewReadyEvent,
	type IDockviewPanelProps,
	positionToDirection,
} from "dockview";
import { type FunctionComponent, useCallback, useEffect, useRef } from "react";
import { EntityPanelWrapper } from "./EntityPanelWrapper.js";
import { DRAG_MIME_FXTR_ENTITY, parseDragData } from "./interactions/drag.js";
import { NamespacePanel } from "./panels/NamespacePanel.js";
import { PANEL_COMPONENT_NAMESPACE } from "./panels/namespacePanelComponent.js";
import type { DropPosition, WorkstationApi } from "./workstation/navigate.js";

export const PANEL_COMPONENT_ENTITY = "entity";

const components: Record<string, FunctionComponent<IDockviewPanelProps>> = {
	// biome-ignore lint/suspicious/noExplicitAny: dockview erases panel param shapes
	[PANEL_COMPONENT_ENTITY]: EntityPanelWrapper as FunctionComponent<IDockviewPanelProps<any>>,
	// biome-ignore lint/suspicious/noExplicitAny: dockview erases panel param shapes
	[PANEL_COMPONENT_NAMESPACE]: NamespacePanel as FunctionComponent<IDockviewPanelProps<any>>,
};

export interface DockviewLayoutProps {
	onReady: (event: DockviewReadyEvent) => void;
	/** Workstation API used to route drops into the right method. */
	workstationApi: WorkstationApi;
	className?: string;
}

export function DockviewLayout(props: DockviewLayoutProps) {
	const { onReady, workstationApi } = props;

	// Keep the latest api in a ref so our dockview subscriptions
	// don't need to re-subscribe when it changes identity.
	const apiPropRef = useRef(workstationApi);
	useEffect(() => {
		apiPropRef.current = workstationApi;
	}, [workstationApi]);

	const handleReady = useCallback(
		(event: DockviewReadyEvent) => {
			event.api.onUnhandledDragOverEvent((e) => {
				const types = e.nativeEvent.dataTransfer?.types;
				if (types && Array.from(types).includes(DRAG_MIME_FXTR_ENTITY)) {
					e.accept();
				}
			});
			onReady(event);
		},
		[onReady],
	);

	const onDidDrop = useCallback((event: DockviewDidDropEvent) => {
		const dt = event.nativeEvent.dataTransfer;
		if (!dt) return;
		const payload = parseDragData(dt);
		if (!payload) return;

		const direction = positionToDirection(event.position);
		const position: DropPosition = {};
		if (event.group) position.referenceGroup = event.group;
		if (direction !== "within") position.direction = direction;

		const api = apiPropRef.current;

		if (payload.kind === "snapshot") {
			api.restoreIntoNewPanel(payload.newPanelState, position);
			return;
		}

		// kind === "link"
		let target: FxtrID;
		try {
			target = FxtrID.fromIdString(payload.entityIdString);
		} catch (err) {
			console.error("[workstation] failed to parse dropped entity id", err);
			return;
		}
		if (payload.sourcePanelId === undefined) {
			// A content-ref drag that was packed as `link` but has no
			// source panel id — shouldn't normally happen because non-
			// panel origins pack `SnapshotDragPayload` at drag-start.
			// Defensive: drop silently.
			console.warn("[workstation] received link drag with no sourcePanelId; ignoring", payload);
			return;
		}
		api.followLink({
			target,
			trail: payload.trail,
			sourcePanelId: payload.sourcePanelId,
			intent: { kind: "open-new", position },
		});
	}, []);

	return (
		<DockviewReact
			className={props.className ?? "fw-dockview-theme"}
			components={components}
			onReady={handleReady}
			onDidDrop={onDidDrop}
		/>
	);
}
