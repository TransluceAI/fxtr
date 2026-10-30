/**
 * WorkstationShell — top-level component for the panel viewer.
 *
 * Owns the dockview instance, the `WorkstationApi` exposed via
 * `WorkstationContext`, the open-panels sidebar, and the layout-
 * persistence loop.
 *
 * Panel-id policy:
 *   - The namespace panel uses a canonical id (`namespace-main`) so we
 *     can find-or-open it; it auto-reopens if closed.
 *   - Entity panels always get a fresh unique id (`entity-<counter>`).
 *     Clicking a content ref "in place" updates the source panel's
 *     params (chain+activeIndex) rather than reusing a pre-existing
 *     panel for the target entity.
 *
 * Three-method API:
 *   - `followLink(event)` — content-ref clicks / right-clicks / drag-
 *     drops. `replace-in-source` → mutate the source panel's chain via
 *     `computeNextChainState`. `open-new` → derive new state via
 *     `computeOpenNewFromLink`, then `restoreIntoNewPanel`.
 *   - `activateChainEntry(panelId, chainIndex)` — shift activeIndex of
 *     an existing entity panel. No-op if the panel is missing or the
 *     index is already active.
 *   - `restoreIntoNewPanel(params, position?)` — add a new entity
 *     panel with the given fully-resolved `EntityPanelParams`.
 *
 * Panel params are JSON-safe — `{ chain, activeIndex, historyRoot }`
 * with string ids only. The namespace flows to `NamespacePanel` via
 * `NamespaceContext`; rich class instances never travel through
 * dockview's `toJSON` / `fromJSON` round-trip.
 *
 * Layout persistence:
 *   - `onDidLayoutChange` → 200ms debounced write to `localStorage`
 *     under `fxtr-workstation-layout`. Storage version 3 — a bump from
 *     the v2 schema that only carried `{ entityIdString }`.
 *   - On ready, if a persisted layout exists:
 *     - Collect every entity id referenced in the persisted JSON
 *       (walks each panel's `chain` *and* every `historyRoot` prefix
 *       chain). Best-effort bridge via `tryRetrieveById`.
 *     - Then `api.fromJSON`. If that throws, clear the key and build
 *       the default layout.
 */

import { FxtrID, type FxtrReadNamespace } from "@fxtr/core";
import type { AddPanelOptions, DockviewApi, IDockviewPanel, SerializedDockview } from "dockview";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DockviewLayout, PANEL_COMPONENT_ENTITY } from "./DockviewLayout.js";
import { computeNextChainState, computeOpenNewFromLink } from "./breadcrumb/history.js";
import type { EntityPanelParams, HistoryRoot } from "./breadcrumb/types.js";
import { HoverCloseContext, type HoverCloseContextValue } from "./interactions/HoverContext.js";
import { NAMESPACE_PANEL_ID, PANEL_COMPONENT_NAMESPACE } from "./panels/namespacePanelComponent.js";
import { OpenPanelsSidebar } from "./sidebar/OpenPanelsSidebar.js";
import { ResizableSidebar } from "./sidebar/ResizableSidebar.js";
import { NamespaceContextProvider } from "./workstation/NamespaceContext.js";
import { WorkstationProvider } from "./workstation/WorkstationContext.js";
import type { DropPosition, FollowLinkEvent, WorkstationApi } from "./workstation/navigate.js";

/** localStorage key carrying the persisted layout. */
const LAYOUT_STORAGE_KEY = "fxtr-workstation-layout";
/** Bump this if we break the stored format. Old payloads are ignored. */
const LAYOUT_STORAGE_VERSION = 3;
const LAYOUT_SAVE_DEBOUNCE_MS = 200;

const SIDEBAR_WIDTH_MIN = 180;
const SIDEBAR_WIDTH_MAX = 480;
const SIDEBAR_WIDTH_DEFAULT = 240;

function clampSidebarWidth(n: number): number {
	if (!Number.isFinite(n)) return SIDEBAR_WIDTH_DEFAULT;
	return Math.min(SIDEBAR_WIDTH_MAX, Math.max(SIDEBAR_WIDTH_MIN, n));
}

interface PersistedWorkstationLayout {
	version: typeof LAYOUT_STORAGE_VERSION;
	dockview: SerializedDockview;
	sidebarCollapsed: boolean;
	sidebarWidth: number;
}

function loadPersistedLayout(): PersistedWorkstationLayout | null {
	if (typeof localStorage === "undefined") return null;
	const raw = localStorage.getItem(LAYOUT_STORAGE_KEY);
	if (!raw) return null;
	try {
		const parsed = JSON.parse(raw) as Partial<PersistedWorkstationLayout>;
		if (parsed.version !== LAYOUT_STORAGE_VERSION) return null;
		if (!parsed.dockview || typeof parsed.dockview !== "object") return null;
		return {
			version: LAYOUT_STORAGE_VERSION,
			dockview: parsed.dockview as SerializedDockview,
			sidebarCollapsed: parsed.sidebarCollapsed === true,
			sidebarWidth: clampSidebarWidth(
				typeof parsed.sidebarWidth === "number" ? parsed.sidebarWidth : SIDEBAR_WIDTH_DEFAULT,
			),
		};
	} catch (err) {
		console.warn("[workstation] failed to parse persisted layout", err);
		return null;
	}
}

function savePersistedLayout(payload: PersistedWorkstationLayout): void {
	if (typeof localStorage === "undefined") return;
	try {
		localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(payload));
	} catch (err) {
		console.warn("[workstation] failed to persist layout", err);
	}
}

function toDockviewPosition(
	pos: DropPosition | undefined,
): AddPanelOptions<EntityPanelParams>["position"] | undefined {
	if (!pos) return undefined;
	const out: Record<string, unknown> = {};
	if (pos.referenceGroup !== undefined) out.referenceGroup = pos.referenceGroup;
	if (pos.direction !== undefined) out.direction = pos.direction;
	if (Object.keys(out).length === 0) return undefined;
	return out as AddPanelOptions<EntityPanelParams>["position"];
}

function maxEntityCounter(api: DockviewApi): number {
	let max = 0;
	for (const panel of api.panels) {
		const match = /^entity-(\d+)$/.exec(panel.id);
		if (match?.[1]) {
			const n = Number.parseInt(match[1], 10);
			if (Number.isFinite(n) && n > max) max = n;
		}
	}
	return max;
}

/**
 * Walk every historyRoot prefix recursively, collecting its entries.
 */
function collectIdsFromHistoryRoot(root: HistoryRoot, out: Set<string>): void {
	let node: HistoryRoot = root;
	while (node.kind === "branched") {
		for (const id of node.prefix) out.add(id);
		node = node.parent;
	}
}

/**
 * Walk a serialized-dockview JSON and collect every entity id
 * referenced by any entity panel — across all panels' chains and
 * every historyRoot prefix chain. Used at restore time to drive the
 * best-effort `tryRetrieveById` bridging.
 */
function collectEntityIdStringsFromLayout(layout: SerializedDockview): string[] {
	const out = new Set<string>();
	const panels = (layout as unknown as { panels?: Record<string, unknown> }).panels;
	if (!panels || typeof panels !== "object") return [];
	for (const panel of Object.values(panels)) {
		if (!panel || typeof panel !== "object") continue;
		const p = panel as {
			contentComponent?: unknown;
			params?: { chain?: unknown; historyRoot?: unknown };
		};
		if (p.contentComponent !== PANEL_COMPONENT_ENTITY) continue;
		const chain = p.params?.chain;
		if (Array.isArray(chain)) {
			for (const s of chain) {
				if (typeof s === "string" && s.length > 0) out.add(s);
			}
		}
		const root = p.params?.historyRoot;
		if (root && typeof root === "object") {
			collectIdsFromHistoryRoot(root as HistoryRoot, out);
		}
	}
	return Array.from(out);
}

export interface WorkstationShellProps {
	namespace: FxtrReadNamespace;
	/**
	 * Bridge an entity into `namespace.scope` by id. Called on layout
	 * restore for every entity referenced in persisted panels (chain +
	 * every historyRoot prefix).
	 */
	tryRetrieveById: (id: FxtrID) => Promise<FxtrID | null>;
	/**
	 * Entities to open as entity panels on first mount, alongside the
	 * namespace panel. Each one becomes a panel with
	 * `{ chain: [id], activeIndex: 0, historyRoot: { namespace } }`.
	 * Ignored when there is a saved layout to restore.
	 */
	initialPanels?: readonly FxtrID[];
}

export function WorkstationShell(props: WorkstationShellProps) {
	const { namespace, tryRetrieveById, initialPanels } = props;

	const apiRef = useRef<DockviewApi | null>(null);
	const panelCounterRef = useRef(0);
	const [api, setApi] = useState<DockviewApi | null>(null);

	const persistedAtMountRef = useRef<PersistedWorkstationLayout | null>(null);
	if (persistedAtMountRef.current === null) {
		persistedAtMountRef.current = loadPersistedLayout();
	}
	const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(
		() => persistedAtMountRef.current?.sidebarCollapsed ?? false,
	);
	const [sidebarWidth, setSidebarWidthState] = useState<number>(
		() => persistedAtMountRef.current?.sidebarWidth ?? SIDEBAR_WIDTH_DEFAULT,
	);

	const setSidebarWidth = useCallback((w: number) => {
		setSidebarWidthState(clampSidebarWidth(w));
	}, []);

	const hoverClose = useMemo<HoverCloseContextValue>(() => {
		const listeners = new Set<() => void>();
		return {
			registerCloseListener(cb) {
				listeners.add(cb);
				return () => {
					listeners.delete(cb);
				};
			},
			closeAllHovers() {
				for (const cb of listeners) cb();
			},
		};
	}, []);

	const tryRetrieveByIdRef = useRef(tryRetrieveById);
	useEffect(() => {
		tryRetrieveByIdRef.current = tryRetrieveById;
	}, [tryRetrieveById]);

	const nextPanelId = useCallback(() => {
		panelCounterRef.current += 1;
		return `entity-${panelCounterRef.current}`;
	}, []);

	// ------------------------------------------------------------------
	// Shared helpers used by the three API methods.
	// ------------------------------------------------------------------

	const addEntityPanel = useCallback(
		(params: EntityPanelParams, position?: DropPosition | undefined) => {
			const currentApi = apiRef.current;
			if (!currentApi) return;
			const id = nextPanelId();
			const dvPos = toDockviewPosition(position);
			currentApi.addPanel<EntityPanelParams>({
				id,
				component: PANEL_COMPONENT_ENTITY,
				params,
				...(dvPos ? { position: dvPos } : {}),
			});
		},
		[nextPanelId],
	);

	const updateEntityPanelParams = useCallback(
		(panel: IDockviewPanel, params: EntityPanelParams) => {
			panel.api.updateParameters(params as unknown as Record<string, unknown>);
			panel.api.setActive();
		},
		[],
	);

	const readPanelParams = useCallback((panelId: string): EntityPanelParams | null => {
		const currentApi = apiRef.current;
		if (!currentApi) return null;
		const panel = currentApi.getPanel(panelId);
		if (!panel) return null;
		if (panel.view.contentComponent !== PANEL_COMPONENT_ENTITY) return null;
		const p = panel.params as Partial<EntityPanelParams> | undefined;
		if (
			!p ||
			!Array.isArray(p.chain) ||
			typeof p.activeIndex !== "number" ||
			!p.historyRoot ||
			typeof p.historyRoot !== "object"
		) {
			return null;
		}
		return {
			chain: p.chain as string[],
			activeIndex: p.activeIndex,
			historyRoot: p.historyRoot as HistoryRoot,
		};
	}, []);

	// ------------------------------------------------------------------
	// WorkstationApi implementation.
	// ------------------------------------------------------------------

	const followLink = useCallback(
		(event: FollowLinkEvent) => {
			hoverClose.closeAllHovers();

			const currentApi = apiRef.current;
			if (!currentApi) {
				console.warn("[workstation] followLink called before dockview ready", event);
				return;
			}
			const source = readPanelParams(event.sourcePanelId);
			if (!source) {
				// Interaction hooks should route non-panel origins through
				// `restoreIntoNewPanel` instead. If we get here, the source
				// panel was closed between drag-start and drop, or a caller
				// violated the precondition. No-op rather than silently
				// spawning a panel.
				console.warn(
					"[workstation] followLink: source panel missing or not an entity panel",
					event.sourcePanelId,
				);
				return;
			}

			if (event.intent.kind === "replace-in-source") {
				const next = computeNextChainState(source, event.target.idString());
				if (next === source) return;
				const panel = currentApi.getPanel(event.sourcePanelId);
				if (panel) updateEntityPanelParams(panel, next);
				return;
			}

			// open-new: derive state from source + trail, then add.
			const next = computeOpenNewFromLink(source, event.trail);
			addEntityPanel(next, event.intent.position);
		},
		[addEntityPanel, readPanelParams, updateEntityPanelParams, hoverClose],
	);

	const activateChainEntry = useCallback(
		(panelId: string, chainIndex: number) => {
			const currentApi = apiRef.current;
			if (!currentApi) return;
			const source = readPanelParams(panelId);
			if (!source) return;
			if (chainIndex < 0 || chainIndex >= source.chain.length) return;
			if (chainIndex === source.activeIndex) {
				// Still focus the panel.
				currentApi.getPanel(panelId)?.api.setActive();
				return;
			}
			const panel = currentApi.getPanel(panelId);
			if (!panel) return;
			updateEntityPanelParams(panel, { ...source, activeIndex: chainIndex });
		},
		[readPanelParams, updateEntityPanelParams],
	);

	const restoreIntoNewPanel = useCallback(
		(params: EntityPanelParams, position?: DropPosition) => {
			hoverClose.closeAllHovers();
			addEntityPanel(params, position);
		},
		[addEntityPanel, hoverClose],
	);

	const workstationApi = useMemo<WorkstationApi>(
		() => ({ followLink, activateChainEntry, restoreIntoNewPanel }),
		[followLink, activateChainEntry, restoreIntoNewPanel],
	);

	// `initialPanels` is intentionally only honoured on first ready.
	const initialPanelsRef = useRef(initialPanels);

	const onReady = useCallback((event: { api: DockviewApi }) => {
		const dockviewApi = event.api;

		const ensureNamespacePanel = () => {
			if (dockviewApi.getPanel(NAMESPACE_PANEL_ID)) return;
			dockviewApi.addPanel({
				id: NAMESPACE_PANEL_ID,
				component: PANEL_COMPONENT_NAMESPACE,
				title: "Namespace",
				params: {},
			});
		};

		const buildDefaultLayout = () => {
			ensureNamespacePanel();
			const initial = initialPanelsRef.current;
			if (initial && initial.length > 0) {
				const nsPanel = dockviewApi.getPanel(NAMESPACE_PANEL_ID);
				for (const entity of initial) {
					panelCounterRef.current += 1;
					const id = `entity-${panelCounterRef.current}`;
					const params: EntityPanelParams = {
						chain: [entity.idString()],
						activeIndex: 0,
						historyRoot: { kind: "namespace" },
					};
					dockviewApi.addPanel<EntityPanelParams>({
						id,
						component: PANEL_COMPONENT_ENTITY,
						params,
						...(nsPanel
							? { position: { referencePanel: nsPanel, direction: "right" as const } }
							: {}),
					});
				}
			}
		};

		const commitReady = () => {
			dockviewApi.onDidRemovePanel((panel) => {
				if (panel.id === NAMESPACE_PANEL_ID) {
					setTimeout(ensureNamespacePanel, 0);
				}
			});
			apiRef.current = dockviewApi;
			setApi(dockviewApi);
		};

		const persisted = persistedAtMountRef.current;
		if (!persisted) {
			buildDefaultLayout();
			commitReady();
			return;
		}

		const entityIds = collectEntityIdStringsFromLayout(persisted.dockview);
		const bridger = tryRetrieveByIdRef.current;

		(async () => {
			await Promise.all(
				entityIds.map(async (s) => {
					let id: FxtrID;
					try {
						id = FxtrID.fromIdString(s);
					} catch (err) {
						console.warn("[workstation] malformed entity id in layout", s, err);
						return;
					}
					try {
						const result = await bridger(id);
						if (result === null) {
							console.warn("[workstation] entity not found on server at restore:", s);
						}
					} catch (err) {
						console.warn("[workstation] tryRetrieveById threw for", s, err);
					}
				}),
			);

			let restored = false;
			try {
				dockviewApi.fromJSON(persisted.dockview);
				restored = true;
			} catch (err) {
				console.warn("[workstation] fromJSON failed; resetting layout", err);
				try {
					localStorage.removeItem(LAYOUT_STORAGE_KEY);
				} catch {
					// ignore
				}
			}

			ensureNamespacePanel();
			if (restored) {
				panelCounterRef.current = maxEntityCounter(dockviewApi);
			} else {
				buildDefaultLayout();
			}
			commitReady();
		})().catch((err) => {
			console.error("[workstation] unexpected restore failure", err);
			buildDefaultLayout();
			commitReady();
		});
	}, []);

	const sidebarCollapsedRef = useRef(sidebarCollapsed);
	const sidebarWidthRef = useRef(sidebarWidth);
	useEffect(() => {
		sidebarCollapsedRef.current = sidebarCollapsed;
	}, [sidebarCollapsed]);
	useEffect(() => {
		sidebarWidthRef.current = sidebarWidth;
	}, [sidebarWidth]);

	const writeCurrentLayout = useCallback(() => {
		const currentApi = apiRef.current;
		if (!currentApi) return;
		savePersistedLayout({
			version: LAYOUT_STORAGE_VERSION,
			dockview: currentApi.toJSON(),
			sidebarCollapsed: sidebarCollapsedRef.current,
			sidebarWidth: sidebarWidthRef.current,
		});
	}, []);

	useEffect(() => {
		if (!api) return;
		let timer: ReturnType<typeof setTimeout> | null = null;
		const flush = () => {
			timer = null;
			writeCurrentLayout();
		};
		const disposable = api.onDidLayoutChange(() => {
			if (timer !== null) clearTimeout(timer);
			timer = setTimeout(flush, LAYOUT_SAVE_DEBOUNCE_MS);
		});
		return () => {
			disposable.dispose();
			if (timer !== null) {
				clearTimeout(timer);
				flush();
			}
		};
	}, [api, writeCurrentLayout]);

	// biome-ignore lint/correctness/useExhaustiveDependencies: sidebarCollapsed is the intended trigger; writeCurrentLayout reads it through a ref.
	useEffect(() => {
		if (!apiRef.current) return;
		writeCurrentLayout();
	}, [sidebarCollapsed, writeCurrentLayout]);

	// biome-ignore lint/correctness/useExhaustiveDependencies: sidebarWidth is the intended trigger; writeCurrentLayout reads it through a ref.
	useEffect(() => {
		if (!apiRef.current) return;
		const timer = setTimeout(() => {
			writeCurrentLayout();
		}, LAYOUT_SAVE_DEBOUNCE_MS);
		return () => clearTimeout(timer);
	}, [sidebarWidth, writeCurrentLayout]);

	const toggleSidebarCollapsed = useCallback(() => {
		setSidebarCollapsed((v) => !v);
	}, []);

	return (
		<NamespaceContextProvider namespace={namespace}>
			<WorkstationProvider value={workstationApi}>
				<HoverCloseContext.Provider value={hoverClose}>
					<div style={{ height: "100%", display: "flex", flexDirection: "row" }}>
						{sidebarCollapsed ? (
							<OpenPanelsSidebar
								api={api}
								collapsed={true}
								onToggleCollapsed={toggleSidebarCollapsed}
							/>
						) : (
							<ResizableSidebar
								width={sidebarWidth}
								minWidth={SIDEBAR_WIDTH_MIN}
								maxWidth={SIDEBAR_WIDTH_MAX}
								onWidthChange={setSidebarWidth}
							>
								<OpenPanelsSidebar
									api={api}
									collapsed={false}
									onToggleCollapsed={toggleSidebarCollapsed}
								/>
							</ResizableSidebar>
						)}
						<div
							style={{ flex: "1 1 auto", minWidth: 0, display: "flex", flexDirection: "column" }}
						>
							<DockviewLayout onReady={onReady} workstationApi={workstationApi} />
						</div>
					</div>
				</HoverCloseContext.Provider>
			</WorkstationProvider>
		</NamespaceContextProvider>
	);
}
