/**
 * `useLayoutPersistence` — save side of the layout persistence
 * loop.
 *
 * Subscribes to dockview's `onDidLayoutChange` (drags, splits,
 * resizes, panel adds / removes, `updateParameters` calls — i.e.
 * every change that should land in `toJSON`) and writes the
 * serialized layout to `localStorage` 200ms-debounced after each
 * burst of changes. Sidebar collapse / width are read through
 * refs so they participate in every write without re-binding the
 * dockview subscription.
 *
 * The dispatcher session id is **not** part of this payload — it
 * has its own `localStorage` key and is written directly by the
 * shell when `createSession` resolves. See
 * `dispatcher_session_id.ts`.
 *
 * The load side is a one-shot call to `loadPersistedLayout` at
 * mount, done by the shell directly — no hook needed.
 *
 * Final-write on unmount: if a debounce timer is pending when
 * the hook tears down, it fires synchronously before unsubscribe
 * so the most recent layout makes it to storage.
 */

import type { DockviewApi } from "dockview";
import { useCallback, useEffect, useRef } from "react";
import {
	LAYOUT_STORAGE_VERSION,
	type PersistedLayoutV6,
	savePersistedLayout,
} from "./layout_v6.js";

const SAVE_DEBOUNCE_MS = 200;

export interface UseLayoutPersistenceOptions {
	/** Storage handle to persist into (the viewer's `storage` prop). */
	readonly storage: Storage;
	readonly api: DockviewApi | null;
	readonly sidebarCollapsed: boolean;
	readonly sidebarWidth: number;
}

export function useLayoutPersistence(opts: UseLayoutPersistenceOptions): void {
	const { storage, api, sidebarCollapsed, sidebarWidth } = opts;

	// Refs so `writeLayout` reads the latest values without
	// having to re-bind on every state change.
	const sidebarCollapsedRef = useRef(sidebarCollapsed);
	sidebarCollapsedRef.current = sidebarCollapsed;
	const sidebarWidthRef = useRef(sidebarWidth);
	sidebarWidthRef.current = sidebarWidth;

	const writeLayout = useCallback(() => {
		if (api === null) return;
		const payload: PersistedLayoutV6 = {
			version: LAYOUT_STORAGE_VERSION,
			dockview: api.toJSON(),
			sidebar: {
				collapsed: sidebarCollapsedRef.current,
				width: sidebarWidthRef.current,
			},
		};
		savePersistedLayout(storage, payload);
	}, [api, storage]);

	// Dockview events → debounced save. Final flush on unmount
	// if a timer is pending.
	useEffect(() => {
		if (api === null) return;
		let timer: ReturnType<typeof setTimeout> | null = null;
		const flush = () => {
			timer = null;
			writeLayout();
		};
		const disposable = api.onDidLayoutChange(() => {
			if (timer !== null) clearTimeout(timer);
			timer = setTimeout(flush, SAVE_DEBOUNCE_MS);
		});
		return () => {
			disposable.dispose();
			if (timer !== null) {
				clearTimeout(timer);
				flush();
			}
		};
	}, [api, writeLayout]);

	// Sidebar collapse changes → immediate save.
	// biome-ignore lint/correctness/useExhaustiveDependencies: `sidebarCollapsed` is the trigger; `writeLayout` reads it through a ref.
	useEffect(() => {
		if (api === null) return;
		writeLayout();
	}, [api, sidebarCollapsed, writeLayout]);

	// Sidebar width changes → debounced save (drag-resize fires
	// many times in quick succession).
	// biome-ignore lint/correctness/useExhaustiveDependencies: `sidebarWidth` is the trigger; `writeLayout` reads it through a ref.
	useEffect(() => {
		if (api === null) return;
		const timer = setTimeout(writeLayout, SAVE_DEBOUNCE_MS);
		return () => clearTimeout(timer);
	}, [api, sidebarWidth, writeLayout]);
}
