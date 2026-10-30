/**
 * Hook for tracking dockview panel layout.
 *
 * Subscribes to dockview's add/remove/active/layout events and returns
 * a flat list of groups (matching dockview's own grouping) with each
 * group's panels. Entity panels carry their `FxtrID` (lifted from
 * `params.entity`) so the sidebar can dispatch its display via the
 * registry.
 *
 * Ported from thingwatcher's `usePanelLayout.ts` with the FxtrID plumbing
 * swapped in for thing keys.
 */

import { FxtrID } from "@fxtr/core";
import type { DockviewApi } from "dockview";
import { useCallback, useRef, useSyncExternalStore } from "react";
import { PANEL_COMPONENT_ENTITY } from "../DockviewLayout.js";
import type { EntityPanelParams } from "../EntityPanelWrapper.js";

export interface SidebarPanel {
	panelId: string;
	/** Dockview panel-component id, e.g. "entity" or "namespace". */
	component: string;
	title: string;
	isActive: boolean;
	/** Present only for `"entity"` panels. */
	entity?: FxtrID;
}

export interface SidebarGroup {
	groupId: string;
	panels: SidebarPanel[];
}

function buildLayout(api: DockviewApi): SidebarGroup[] {
	const groups: SidebarGroup[] = [];

	for (const group of api.groups) {
		const panels: SidebarPanel[] = [];
		for (const panel of group.panels) {
			const component = panel.api.component;
			let entity: FxtrID | undefined;
			if (component === PANEL_COMPONENT_ENTITY) {
				const params = panel.params as EntityPanelParams | undefined;
				const idStr = params?.chain?.[params.activeIndex];
				if (typeof idStr === "string" && idStr.length > 0) {
					try {
						entity = FxtrID.fromIdString(idStr);
					} catch (err) {
						console.warn("[workstation] bad chain[activeIndex] id on panel", panel.id, idStr, err);
					}
				}
			}
			panels.push({
				panelId: panel.id,
				component,
				title: panel.title ?? panel.id,
				isActive: api.activePanel?.id === panel.id,
				...(entity !== undefined ? { entity } : {}),
			});
		}
		groups.push({ groupId: group.id, panels });
	}

	return groups;
}

export function usePanelLayout(api: DockviewApi | null): SidebarGroup[] {
	// Cache the layout; rebuild on dockview events. `useSyncExternalStore`
	// demands a stable getSnapshot return value between notifications —
	// mutating a ref in place in `subscribe` and only returning the
	// cached value here satisfies that.
	const layoutRef = useRef<SidebarGroup[]>([]);
	const initializedRef = useRef(false);

	const subscribe = useCallback(
		(onStoreChange: () => void) => {
			if (!api) return () => {};

			layoutRef.current = buildLayout(api);
			initializedRef.current = true;

			const rebuildAndNotify = () => {
				layoutRef.current = buildLayout(api);
				onStoreChange();
			};

			const disposables = [
				api.onDidAddPanel(rebuildAndNotify),
				api.onDidRemovePanel(rebuildAndNotify),
				api.onDidAddGroup(rebuildAndNotify),
				api.onDidRemoveGroup(rebuildAndNotify),
				api.onDidActivePanelChange(rebuildAndNotify),
				// onDidLayoutChange catches drags/resizes/moves that don't
				// pass through the add/remove events.
				api.onDidLayoutChange(rebuildAndNotify),
			];
			return () => {
				for (const d of disposables) d.dispose();
			};
		},
		[api],
	);

	const getSnapshot = useCallback(() => {
		if (api && !initializedRef.current) {
			layoutRef.current = buildLayout(api);
			initializedRef.current = true;
		}
		return layoutRef.current;
	}, [api]);

	const getServerSnapshot = useCallback(() => EMPTY_GROUPS, []);

	return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

const EMPTY_GROUPS: SidebarGroup[] = [];
