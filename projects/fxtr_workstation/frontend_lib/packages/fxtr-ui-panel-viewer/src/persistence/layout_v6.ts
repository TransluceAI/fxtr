/**
 * Layout persistence v6 — load / save / clear helpers.
 *
 * Stored under `storage["fxtr-workstation-layout"]` (the injected
 * `Storage` — the viewer's `storage` prop; `localStorage` in the
 * workstation demo, a shared handle in the HTTP demo) as a versioned
 * JSON payload:
 *
 *   {
 *     version: 6,
 *     dockview: SerializedDockview,   // dockview's own toJSON output
 *     sidebar: { collapsed: boolean; width: number },
 *   }
 *
 * Ref panels' `sessionId`s ride along inside the dockview JSON
 * via their params, so no shell-side bookkeeping is needed to
 * recover them — `usePanelSession` attaches to the persisted id
 * on mount and falls into the `unavailable` body if the server
 * has dropped it.
 *
 * **Note**: the dispatcher session id is stored separately under
 * its own key — see `dispatcher_session_id.ts`. The shell writes
 * it directly the moment `createSession` resolves, without
 * routing through this layout payload, so the read-side at boot
 * is a sync `localStorage.getItem` rather than a reactive write.
 *
 * Version handling. The schema started at v4 with the workstation
 * frontend rewrite — v3 was the old `@fxtr-workstation/core`'s
 * entity-id-flavored shape, which has no meaningful migration
 * into the new world. v5 was a no-op bump for the
 * `StubRef` → `PanelViewEntry` refactor: chain entries gained a
 * required `display` field. v6 is the stage9post1 bump: chain
 * entries now carry a live stub (`{stub, display}` instead of
 * `{kind, shortId, display}`), `HistoryRoot.branched.prefix`
 * carries stub-bearing entries too, and `RefPanelChainState` has
 * a `storages: unknown[]` array parallel to `chain` replacing the
 * singleton `ownStorage` / `metadataStorage`. None of these
 * survive a v5 payload's shape, so the bump clears stale layouts
 * on first load. Any version mismatch (including absent /
 * malformed) drops the persisted state and falls back to a fresh
 * default layout.
 */

import type { SerializedDockview } from "dockview";

export const LAYOUT_STORAGE_KEY = "fxtr-workstation-layout";

/** Bump this if we break the stored format. Old payloads are dropped. */
export const LAYOUT_STORAGE_VERSION = 6;

export interface PersistedSidebarState {
	readonly collapsed: boolean;
	readonly width: number;
}

export interface PersistedLayoutV6 {
	readonly version: typeof LAYOUT_STORAGE_VERSION;
	readonly dockview: SerializedDockview;
	readonly sidebar: PersistedSidebarState;
}

/**
 * Read + version-check the persisted layout from the injected
 * `storage`. Returns `null` for:
 *
 *   - missing key
 *   - JSON parse failure
 *   - version mismatch
 *   - missing required fields
 *
 * Callers fall back to a default layout in any of those cases.
 */
export function loadPersistedLayout(storage: Storage): PersistedLayoutV6 | null {
	const raw = storage.getItem(LAYOUT_STORAGE_KEY);
	if (raw === null) return null;
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch (err) {
		console.warn("[fxtr-ui] failed to parse persisted layout:", err);
		return null;
	}
	if (parsed === null || typeof parsed !== "object") return null;
	const p = parsed as Partial<PersistedLayoutV6> & { version?: unknown };
	if (p.version !== LAYOUT_STORAGE_VERSION) return null;
	if (p.dockview === undefined || typeof p.dockview !== "object" || p.dockview === null) {
		return null;
	}
	const sidebar = p.sidebar;
	if (sidebar === undefined || typeof sidebar !== "object" || sidebar === null) return null;
	const s = sidebar as Partial<PersistedSidebarState>;
	if (typeof s.collapsed !== "boolean") return null;
	if (typeof s.width !== "number" || !Number.isFinite(s.width)) return null;
	return {
		version: LAYOUT_STORAGE_VERSION,
		dockview: p.dockview as SerializedDockview,
		sidebar: { collapsed: s.collapsed, width: s.width },
	};
}

/**
 * Write the layout payload to the injected `storage`. Swallows
 * storage errors (quota, private-mode rejections) with a warning —
 * persistence is best-effort.
 */
export function savePersistedLayout(storage: Storage, payload: PersistedLayoutV6): void {
	try {
		storage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(payload));
	} catch (err) {
		console.warn("[fxtr-ui] failed to persist layout:", err);
	}
}

/** Remove the persisted layout. Called when restore fails. */
export function clearPersistedLayout(storage: Storage): void {
	try {
		storage.removeItem(LAYOUT_STORAGE_KEY);
	} catch {
		// ignore — nothing to do on remove failure.
	}
}
