/**
 * Hook for tracking dockview panel layout.
 *
 * Subscribes to dockview's add/remove/active/layout events and
 * returns a flat list of groups (matching dockview's own
 * grouping) with each group's panels. Carries only the JSON-
 * derivable fields (id, component, title, isActive) — the active
 * stub for a ref panel lives in its session userState and is
 * looked up by the row component via `PanelSessionRegistry`.
 *
 * Ported from the old `usePanelLayout.ts` with the params-level
 * stub lookup removed.
 */

import type { DockviewApi } from "dockview";
import { useCallback, useRef, useSyncExternalStore } from "react";

export interface SidebarPanel {
	readonly panelId: string;
	/** Dockview panel-component id, e.g. "ref" or "roots". */
	readonly component: string;
	readonly title: string;
	readonly isActive: boolean;
}

export interface SidebarGroup {
	readonly groupId: string;
	readonly panels: ReadonlyArray<SidebarPanel>;
}

function buildLayout(api: DockviewApi): SidebarGroup[] {
	const groups: SidebarGroup[] = [];
	for (const group of api.groups) {
		const panels: SidebarPanel[] = [];
		for (const panel of group.panels) {
			panels.push({
				panelId: panel.id,
				component: panel.api.component,
				title: panel.title ?? panel.id,
				isActive: api.activePanel?.id === panel.id,
			});
		}
		groups.push({ groupId: group.id, panels });
	}
	return groups;
}

export function usePanelLayout(api: DockviewApi | null): ReadonlyArray<SidebarGroup> {
	// Cache the layout; rebuild on dockview events.
	// `useSyncExternalStore` demands a stable `getSnapshot` return
	// value between notifications — mutating a ref in place in
	// `subscribe` and only returning the cached value in
	// `getSnapshot` satisfies that.
	const layoutRef = useRef<ReadonlyArray<SidebarGroup>>(EMPTY_GROUPS);
	const initializedRef = useRef(false);

	const subscribe = useCallback(
		(onStoreChange: () => void) => {
			if (api === null) return () => {};

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
				// onDidLayoutChange catches drags/resizes/moves that
				// don't pass through the add/remove events, plus
				// `updateParameters` calls (which is how we sneak
				// new `sessionId`s into ref-panel params).
				api.onDidLayoutChange(rebuildAndNotify),
			];
			return () => {
				for (const d of disposables) d.dispose();
			};
		},
		[api],
	);

	const getSnapshot = useCallback(() => {
		if (api !== null && !initializedRef.current) {
			layoutRef.current = buildLayout(api);
			initializedRef.current = true;
		}
		return layoutRef.current;
	}, [api]);

	const getServerSnapshot = useCallback(() => EMPTY_GROUPS, []);

	return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

const EMPTY_GROUPS: ReadonlyArray<SidebarGroup> = [];
