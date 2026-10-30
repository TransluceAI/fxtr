/**
 * `PanelViewer` — top-level component for the panel viewer.
 *
 * Responsibilities:
 *
 *   - Hold the connector reference (consumer prop) and the
 *     dispatcher reference (also consumer prop — see the
 *     dispatcher-ownership note below).
 *
 *   - Branch on `dispatcher.status`: render `ShellLoading` while
 *     `"connecting"` / `"stale"` / `"closed"`, render
 *     `DispatcherUnavailable` while `"unavailable"`, render the
 *     full panel tree once `"connected"`. The dispatch state
 *     machines call `ctx.allocateLoad` which requires a connected
 *     session, so we gate the tree on connection.
 *
 *   - Maintain the panel-id counter (`panel-<n>`) and the
 *     panel-session registry that lets the `PanelViewerApi`
 *     impl reach each panel's live session.
 *
 *   - Implement `PanelViewerApi` against dockview + the panel-
 *     session registry:
 *       - `followLink({intent:"replace-in-source"})` —
 *         `computeNextChainState` against the source panel's
 *         chain state, write it back through its session
 *         `userState`.
 *       - `followLink({intent:"open-new"})` —
 *         `computeOpenNewFromLink`, then open a new panel
 *         seeded with that state.
 *       - `activateChainEntry(panelId, idx)` — shift activeIndex
 *         on the panel's chain state.
 *       - `restoreIntoNewPanel(state, position?)` — open a new
 *         panel seeded with that state.
 *
 *   - Provide `<ConnectorProvider>`, `<DispatcherProvider>`,
 *     `<PanelViewerProvider>`, `<HoverCloseContext.Provider>`,
 *     and `<PanelSessionRegistryProvider>` so descendants reach
 *     their respective dependencies through context.
 *
 * Open-new-panel flow:
 *
 *   1. Mint a panel id.
 *   2. `dockview.addPanel({sessionId: null})` — panel mounts in
 *      pending state immediately.
 *   3. `await connector.createSession(...)`.
 *   4. Write the seed chain state through the shell's freshly-
 *      attached subscription.
 *   5. `session.detach()` — drains the seed write, then closes
 *      the shell's subscription. The server-side session
 *      persists; the seed is now committed.
 *   6. `panel.api.updateParameters({sessionId: newId})` — the
 *      panel's own `usePanelSession` effect re-fires and
 *      attaches with the snapshot containing the seed.
 *
 * The shell does **not** hold a long-lived subscription to any
 * panel's session — only the panel itself does, registered via
 * `PanelSessionRegistry`. Navigation events route writes through
 * the registered session.
 *
 * Layout persistence: at mount the shell loads a v6 payload
 * from `localStorage` (see `persistence/layout_v6.ts`). On
 * dockview ready, a persisted dockview JSON is replayed via
 * `api.fromJSON`; otherwise the shell builds the default
 * one-roots-panel layout. The save side is wired through
 * `useLayoutPersistence` — debounced dockview events + immediate
 * sidebar writes.
 *
 * Dispatcher ownership: the consumer constructs the `Dispatcher`
 * (typically via `createStaticDispatcher`) and passes it in as
 * a prop. The shell does not own the dispatcher's session
 * lifecycle — the consumer is responsible for that, including
 * `localStorage` persistence of the session id. The shell-
 * exported `readDispatcherSessionId` / `writeDispatcherSessionId`
 * helpers (in `persistence/dispatcher_session_id.ts`) are the
 * idiomatic primitives; the workstation-demo uses them in its
 * App.tsx. When `dispatcher.status === "unavailable"` (server
 * has dropped the session), the shell renders a
 * `DispatcherUnavailable` card whose "Clear local state and
 * reload" button wipes the shell's own persisted state (panel
 * layout) and calls the optional `onClearAndReload` prop —
 * which the consumer wires up to clear its dispatcher-session-id
 * and reload the page.
 *
 * Open-panels sidebar: rendered as a left rail alongside the
 * dockview area. Collapse / width state is shell-local
 * `useState`, initialized from the persisted payload at mount
 * and written back by `useLayoutPersistence`.
 */

import type { Connector, Session } from "@fxtr-ui/core";
import type { JsonValue, StateValue } from "@fxtr-ui/core";
import { type Dispatcher, DispatcherProvider } from "@fxtr-ui/react-dispatch";
import type { AddPanelOptions, DockviewApi, DockviewReadyEvent } from "dockview";
import { atom, useAtomValue } from "jotai";
import { type CSSProperties, useCallback, useMemo, useRef, useState } from "react";
import { computeNextChainState, computeOpenNewFromLink } from "../breadcrumb/history.js";
import type { RefPanelChainState } from "../breadcrumb/types.js";
import { REF_PANEL_COMPONENT, type RefPanelParams } from "../panel/RefPanelParams.js";
import { clearPersistedLayout, loadPersistedLayout } from "../persistence/layout_v6.js";
import { useLayoutPersistence } from "../persistence/useLayoutPersistence.js";
import { HoverCloseContext, type HoverCloseContextValue } from "../reflink/HoverContext.js";
import { ShellRefLinkProvider } from "../reflink/ShellRefLinkImpl.js";
import { OpenPanelsSidebar, type SidebarButton } from "../sidebar/OpenPanelsSidebar.js";
import { ResizableSidebar } from "../sidebar/ResizableSidebar.js";
import { ConnectorProvider } from "./ConnectorContext.js";
import { DockviewLayout } from "./DockviewLayout.js";
import type { PanelRegistration } from "./PanelRegistration.js";
import { type PanelSessionRegistry, PanelSessionRegistryProvider } from "./PanelSessionRegistry.js";
import type { DropPosition, FollowLinkEvent, PanelViewerApi } from "./PanelViewerApi.js";
import { PanelViewerProvider } from "./PanelViewerContext.js";
import { ShellHeader } from "./ShellHeader.js";

const PANEL_ID_PREFIX = "panel-";

const SIDEBAR_WIDTH_MIN = 180;
const SIDEBAR_WIDTH_MAX = 480;
const SIDEBAR_WIDTH_DEFAULT = 240;

/**
 * Default `RefPanelChainState` used by `readPanelChainState`
 * when a panel session's userState is still `null` (e.g.
 * mid-attach, no writes yet).
 */
const DEFAULT_REF_PANEL_CHAIN_STATE: RefPanelChainState = {
	chain: [],
	activeIndex: -1,
	historyRoot: { kind: "roots" },
	storages: [],
};

/**
 * Cast a `RefPanelChainState` to `StateValue` for session writes.
 * `StateValue`'s object arm uses an index signature
 * (`{[k:string]: StateValue}`) which TypeScript can't structurally
 * widen from a named interface — same caveat
 * `useSessionRootStateAtom` documents. The wire codec is the actual
 * runtime gate on encodability.
 */
function asUserState(state: RefPanelChainState): StateValue {
	return state as unknown as StateValue;
}

/**
 * Walk dockview's current panels and return the highest counter
 * suffix among ids matching `panel-<n>`. Used to seed
 * `panelCounterRef` after a layout restore so newly-opened
 * panels don't collide with restored ones.
 */
function maxPanelCounter(api: DockviewApi): number {
	let max = 0;
	const re = new RegExp(`^${PANEL_ID_PREFIX}(\\d+)$`);
	for (const panel of api.panels) {
		const match = re.exec(panel.id);
		if (match?.[1] !== undefined) {
			const n = Number.parseInt(match[1], 10);
			if (Number.isFinite(n) && n > max) max = n;
		}
	}
	return max;
}

function toDockviewPosition(
	pos: DropPosition | undefined,
): AddPanelOptions<RefPanelParams>["position"] | undefined {
	if (pos === undefined) return undefined;
	const out: Record<string, unknown> = {};
	if (pos.referenceGroup !== undefined) out.referenceGroup = pos.referenceGroup;
	if (pos.direction !== undefined) out.direction = pos.direction;
	if (Object.keys(out).length === 0) return undefined;
	return out as AddPanelOptions<RefPanelParams>["position"];
}

export interface PanelViewerProps {
	readonly connector: Connector;
	/**
	 * Storage handle for the viewer's own persisted state (dockview
	 * layout + sidebar). The standard Web Storage interface — pass
	 * `localStorage` (the workstation demo) or, for the HTTP demo, the
	 * *same* handle given to the connector so sessions + the layout that
	 * embeds their ids age together. `@fxtr-ui/core`'s `memoryStorage()`
	 * is the test default.
	 */
	readonly storage: Storage;
	/**
	 * The app's non-ref panels (roots / debug / custom), injected as
	 * registrations. The viewer registers each component with dockview,
	 * renders a sidebar button per registration that declares one, and
	 * exposes `openPanel(id, params?)` for programmatic opens. The
	 * built-in ref panel is always present and is not part of this list.
	 * Workstation-connector apps pass `workstationPanelRegistrations`.
	 */
	readonly panelRegistrations: readonly PanelRegistration[];
	/**
	 * Registration `id` to open on an empty boot (no persisted layout to
	 * restore). When omitted, the viewer opens nothing and starts blank.
	 * Workstation-connector apps pass `ROOTS_PANEL_COMPONENT`.
	 */
	readonly defaultPanelId?: string;
	/**
	 * The dispatcher this shell renders against. The consumer
	 * constructs it (typically via `createStaticDispatcher` over
	 * a dispatcher session it owns) and is responsible for its
	 * lifecycle — including session-id `localStorage` persistence
	 * (use the package's `readDispatcherSessionId` /
	 * `writeDispatcherSessionId` helpers) and calling
	 * `dispatcher.detach()` on unmount.
	 *
	 * The shell branches on `dispatcher.status` for its loading /
	 * unavailable / connected render gates.
	 */
	readonly dispatcher: Dispatcher;
	/**
	 * Optional URL of the workstation server's debug UI. When set,
	 * the always-rendered `ShellHeader` gains a link to it (opens
	 * in a new tab). Consumers typically derive this from the
	 * workstation `/client` URL (`<wsBase>/debug`) when the Python
	 * side mounted the debug-UI static files.
	 */
	readonly debugUiUrl?: string;
	/**
	 * Optional callback invoked from the `DispatcherUnavailable`
	 * card's "Clear local state and reload" button, *after* the
	 * shell has wiped its own panel-layout `localStorage` state.
	 * The consumer wires this up to clear its dispatcher-session-
	 * id key and trigger `window.location.reload()`. When omitted
	 * the shell still wipes layout state but does not reload —
	 * the consumer's UI is responsible for whatever happens next.
	 */
	readonly onClearAndReload?: () => void;
}

export function PanelViewer(props: PanelViewerProps) {
	const {
		connector,
		dispatcher,
		debugUiUrl,
		onClearAndReload,
		panelRegistrations,
		defaultPanelId,
		storage,
	} = props;

	// ----- Sidebar state -----------------------------------------
	// Initial values lazy-loaded from `localStorage`. The
	// `useLayoutPersistence` hook below writes subsequent changes
	// back. Two separate `loadPersistedLayout` calls are cheap
	// (sync localStorage read + JSON.parse) and only run once at
	// mount each.
	const [sidebarCollapsed, setSidebarCollapsed] = useState(
		() => loadPersistedLayout(storage)?.sidebar.collapsed ?? false,
	);
	const [sidebarWidth, setSidebarWidth] = useState(
		() => loadPersistedLayout(storage)?.sidebar.width ?? SIDEBAR_WIDTH_DEFAULT,
	);
	const [dockviewApi, setDockviewApi] = useState<DockviewApi | null>(null);
	const toggleSidebarCollapsed = useCallback(() => {
		setSidebarCollapsed((v) => !v);
	}, []);

	// ----- Dockview API ref --------------------------------------
	// `dockviewRef` is the imperative handle that the navigation
	// callbacks read at fire time; `dockviewApi` (the state
	// declared above) is what the sidebar consumes through render
	// to subscribe to dockview events. Keeping both lets callbacks
	// avoid re-binding on every dockview event while the render
	// tree still sees a stable api reference.
	const dockviewRef = useRef<DockviewApi | null>(null);

	// ----- Panel id counter --------------------------------------
	const panelCounterRef = useRef(0);
	const nextPanelId = useCallback((): string => {
		panelCounterRef.current += 1;
		return `${PANEL_ID_PREFIX}${panelCounterRef.current}`;
	}, []);

	// ----- Panel session registry --------------------------------
	// Backed by a primitive atom so reactive consumers (the
	// open-panels sidebar) re-render on register / unregister. The
	// nav-event handlers read the current snapshot through `get`
	// which doesn't subscribe.
	const sessionsAtom = useMemo(() => atom<ReadonlyMap<string, Session>>(new Map()), []);
	const store = connector.store;
	const panelRegistry = useMemo<PanelSessionRegistry>(
		() => ({
			register(panelId, session) {
				const next = new Map(store.get(sessionsAtom));
				next.set(panelId, session);
				store.set(sessionsAtom, next);
				return () => {
					const current = store.get(sessionsAtom);
					if (current.get(panelId) === session) {
						const without = new Map(current);
						without.delete(panelId);
						store.set(sessionsAtom, without);
					}
				};
			},
			get(panelId) {
				return store.get(sessionsAtom).get(panelId);
			},
			sessions: sessionsAtom,
		}),
		[sessionsAtom, store],
	);

	// ----- Hover-close context -----------------------------------
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

	// ----- Open-new-panel orchestration --------------------------
	//
	// `mintPanelSession` is the shared "create a session, optionally
	// seed it, then detach the shell's subscription" helper. It
	// returns the new sessionId so the caller can write it into
	// dockview params. Failure paths terminate the orphan if any so
	// no server-side session leaks.
	const mintPanelSession = useCallback(
		async (metadata: JsonValue, seedWriter?: (session: Session) => void): Promise<string> => {
			const session = await connector.createSession(metadata);
			try {
				if (seedWriter !== undefined) {
					seedWriter(session);
				}
				// `detach()` drains any pending seed write before
				// closing our subscription, so the panel's later
				// `attachSession` sees the seed in its initial
				// snapshot.
				await session.detach();
			} catch (err) {
				session.terminate().catch(() => {});
				throw err;
			}
			return session.sessionId;
		},
		[connector],
	);

	const openNewPanel = useCallback(
		async (state: RefPanelChainState, position?: DropPosition): Promise<void> => {
			const api = dockviewRef.current;
			if (api === null) {
				console.warn("[fxtr-ui] openNewPanel called before dockview ready");
				return;
			}

			const panelId = nextPanelId();
			const dvPosition = toDockviewPosition(position);

			// 1. Add panel with sessionId=null. Renders pending body.
			api.addPanel<RefPanelParams>({
				id: panelId,
				component: REF_PANEL_COMPONENT,
				params: { kind: "ref", sessionId: null },
				...(dvPosition !== undefined ? { position: dvPosition } : {}),
			});

			// 2-4. Mint session, write seed, detach.
			let sessionId: string;
			try {
				sessionId = await mintPanelSession({ panel: "ref", panelId }, (session) => {
					session.store.set(session.userState, asUserState(state));
				});
			} catch (err) {
				console.error("[fxtr-ui] openNewPanel session setup failed for", panelId, err);
				api.getPanel(panelId)?.api.close();
				return;
			}

			// 5. Update the panel's params with the real sessionId.
			//    The panel's `usePanelSession` re-fires and attaches.
			const panel = api.getPanel(panelId);
			if (panel === undefined) {
				// Panel was closed mid-flight (rare). The session is
				// now an orphan — terminate via a fresh attach to
				// drop server-side state.
				console.warn(
					"[fxtr-ui] panel closed before session arrived; terminating orphan session",
					panelId,
				);
				const reattached = connector.attachSession(sessionId);
				reattached.terminate().catch(() => {});
				return;
			}
			panel.api.updateParameters({ kind: "ref", sessionId });
		},
		[connector, mintPanelSession, nextPanelId],
	);

	// ----- Registry-driven panel opening -------------------------
	// `id → registration` for find-or-open lookups. Stable while the
	// injected registrations array keeps its identity.
	const registrationsById = useMemo(() => {
		const m = new Map<string, PanelRegistration>();
		for (const reg of panelRegistrations) m.set(reg.id, reg);
		return m;
	}, [panelRegistrations]);

	// Open (or focus) a registered non-ref panel by registration id.
	// Singletons are keyed by `id`; `forSession` panels by
	// `<id>-<sessionId>` (sessionId read from `params`). `params` is
	// passed straight through to the panel; the viewer only inspects
	// `params.sessionId` and the registration's `title` deriver.
	const openPanel = useCallback(
		(id: string, params?: Record<string, unknown>, position?: DropPosition): void => {
			const api = dockviewRef.current;
			if (api === null) {
				console.warn("[fxtr-ui] openPanel called before dockview ready", id);
				return;
			}
			const reg = registrationsById.get(id);
			if (reg === undefined) {
				console.warn("[fxtr-ui] openPanel: no registration for id", id);
				return;
			}

			let panelId = id;
			if (reg.forSession === true) {
				const sessionId = params?.sessionId;
				if (typeof sessionId !== "string") {
					console.warn(
						"[fxtr-ui] openPanel: forSession registration opened without params.sessionId",
						id,
					);
					return;
				}
				panelId = `${id}-${sessionId}`;
			}

			// Find-or-focus.
			const existing = api.getPanel(panelId);
			if (existing !== undefined) {
				existing.api.setActive();
				return;
			}

			const title = typeof reg.title === "function" ? reg.title(params ?? {}) : reg.title;
			const dvPosition = toDockviewPosition(position);
			api.addPanel<Record<string, unknown>>({
				id: panelId,
				component: reg.id,
				title,
				params: params ?? {},
				...(dvPosition !== undefined ? { position: dvPosition } : {}),
			});
		},
		[registrationsById],
	);

	// On an empty boot (no persisted layout to restore), open the app's
	// `defaultPanelId` registration. When the consumer doesn't supply one,
	// the viewer starts blank — it never hardcodes a panel kind.
	const openDefaultPanel = useCallback((): void => {
		if (defaultPanelId === undefined) return;
		openPanel(defaultPanelId);
	}, [defaultPanelId, openPanel]);

	// ----- Dockview ready handler --------------------------------
	//
	// Fires every time DockviewReact mounts (or remounts — e.g.
	// StrictMode dev strict-cleanup-and-setup tears down and
	// recreates dockview's internal layout). On each fire we
	// re-read the persisted layout from localStorage and restore
	// it; if there's no persisted layout AND the dockview is
	// empty, we build the default (the app's default panel).
	//
	// We deliberately don't guard with a "did we already build"
	// flag — that approach miscounted on the second StrictMode
	// onReady, which got a fresh empty dockview that the guard
	// then refused to populate. Re-reading + re-restoring on each
	// call is idempotent (the same persisted layout produces the
	// same dockview state) and correctly handles the remount.
	const onReady = useCallback(
		(event: DockviewReadyEvent) => {
			dockviewRef.current = event.api;
			setDockviewApi(event.api);

			// Re-read persisted layout on every onReady (NOT the
			// at-mount snapshot, which may be stale if a save
			// happened between the previous onReady and this one).
			const persisted = loadPersistedLayout(storage);
			if (persisted !== null) {
				try {
					event.api.fromJSON(persisted.dockview);
					// Bump the counter past any restored panel ids
					// so newly-opened panels don't collide. Use
					// `max(current, restored)` so we don't
					// accidentally go backwards if this is the
					// second onReady of a strict re-mount.
					panelCounterRef.current = Math.max(panelCounterRef.current, maxPanelCounter(event.api));
					return;
				} catch (err) {
					console.warn(
						"[fxtr-ui] fromJSON failed; clearing persisted layout and building default",
						err,
					);
					clearPersistedLayout(storage);
					// Fall through to default layout.
				}
			}
			// No persisted layout (or restore failed). Build the
			// default — but only if dockview is still empty.
			// In a strict re-mount where the previous onReady
			// already added a roots panel and a save fired before
			// this onReady runs, the persisted-load branch above
			// would already have re-restored it, so this fallback
			// only runs on a true first boot.
			if (event.api.panels.length === 0) {
				openDefaultPanel();
			}
		},
		[openDefaultPanel, storage],
	);

	// ----- Read / write helpers used by the API impl -------------
	const readPanelChainState = useCallback(
		(panelId: string): RefPanelChainState | null => {
			const session = panelRegistry.get(panelId);
			if (session === undefined) return null;
			const current = session.store.get(session.userState) as RefPanelChainState | null;
			return current ?? DEFAULT_REF_PANEL_CHAIN_STATE;
		},
		[panelRegistry],
	);

	const writePanelChainState = useCallback(
		(panelId: string, state: RefPanelChainState): boolean => {
			const session = panelRegistry.get(panelId);
			if (session === undefined) return false;
			session.store.set(session.userState, asUserState(state));
			return true;
		},
		[panelRegistry],
	);

	// ----- PanelViewerApi implementation -------------------------
	const followLink = useCallback(
		(event: FollowLinkEvent): void => {
			hoverClose.closeAllHovers();
			const source = readPanelChainState(event.sourcePanelId);
			if (source === null) {
				console.warn(
					"[fxtr-ui] followLink: source panel session not registered",
					event.sourcePanelId,
				);
				return;
			}
			if (event.intent.kind === "replace-in-source") {
				const next = computeNextChainState(source, event.target);
				if (next === source) {
					// Already on this target; still focus the panel.
					dockviewRef.current?.getPanel(event.sourcePanelId)?.api.setActive();
					return;
				}
				writePanelChainState(event.sourcePanelId, next);
				dockviewRef.current?.getPanel(event.sourcePanelId)?.api.setActive();
				return;
			}
			// intent.kind === "open-new"
			const next = computeOpenNewFromLink(source, event.trail);
			void openNewPanel(next, event.intent.position);
		},
		[hoverClose, readPanelChainState, writePanelChainState, openNewPanel],
	);

	const activateChainEntry = useCallback(
		(panelId: string, chainIndex: number): void => {
			const source = readPanelChainState(panelId);
			if (source === null) return;
			if (chainIndex < 0 || chainIndex >= source.chain.length) return;
			if (chainIndex === source.activeIndex) {
				dockviewRef.current?.getPanel(panelId)?.api.setActive();
				return;
			}
			writePanelChainState(panelId, { ...source, activeIndex: chainIndex });
			dockviewRef.current?.getPanel(panelId)?.api.setActive();
		},
		[readPanelChainState, writePanelChainState],
	);

	const restoreIntoNewPanel = useCallback(
		(state: RefPanelChainState, position?: DropPosition): void => {
			hoverClose.closeAllHovers();
			void openNewPanel(state, position);
		},
		[hoverClose, openNewPanel],
	);

	// Terminate-and-remove for a single panel. The ref-panel case
	// is the load-bearing one: closing a ref panel via the tab X
	// or sidebar X should terminate its `Session`
	// server-side. Wiring goes through `closeActionOverride` on
	// the custom tab component and through the sidebar's row
	// close button — both call this helper so they share the
	// terminate-on-close contract. Programmatic dockview disposal
	// (StrictMode teardown, real shell unmount) bypasses this
	// helper entirely, so sessions survive remounts.
	const closePanel = useCallback(
		(panelId: string): void => {
			const api = dockviewRef.current;
			if (api === null) return;
			const panel = api.getPanel(panelId);
			if (panel === undefined) return;
			// Terminate the panel's session (if any) before
			// removing. The session reference lives in the
			// PanelSessionRegistry while the panel is mounted.
			// `terminate()` is fire-and-forget here; the panel's
			// own `usePanelSession` cleanup will drain pending
			// mutations on detach.
			const session = panelRegistry.get(panelId);
			if (session !== undefined) {
				session.terminate().catch((err: unknown) => {
					console.warn("[fxtr-ui] terminate-on-close failed for panel", panelId, err);
				});
			}
			api.removePanel(panel);
		},
		[panelRegistry],
	);

	const workstationApi = useMemo<PanelViewerApi>(
		() => ({
			followLink,
			activateChainEntry,
			restoreIntoNewPanel,
			openPanel,
			closePanel,
		}),
		[followLink, activateChainEntry, restoreIntoNewPanel, openPanel, closePanel],
	);

	// Sidebar buttons derived from the registrations that declare one.
	// Each finds-or-opens its panel via `openPanel`.
	const sidebarButtons = useMemo<readonly SidebarButton[]>(
		() =>
			panelRegistrations
				.filter((r) => r.sidebarButton !== undefined)
				.map((r) => ({
					id: r.id,
					// Narrowed by the filter above.
					label: (r.sidebarButton as { readonly label: string }).label,
					onClick: () => openPanel(r.id),
				})),
		[panelRegistrations, openPanel],
	);

	// ----- Layout persistence ------------------------------------
	// Save side only — the load side lives in `onReady`
	// (`loadPersistedLayout` + `fromJSON`) and in the sidebar
	// state's lazy `useState` initializers. The dispatcher session
	// id has its own `localStorage` key and is written directly
	// in the dispatcher-acquisition effect, so it doesn't ride
	// along here.
	useLayoutPersistence({
		storage,
		api: dockviewApi,
		sidebarCollapsed,
		sidebarWidth,
	});

	// ----- Render gate on dispatcher being connected -------------
	return (
		<WaitForDispatcherReady
			dispatcher={dispatcher}
			storage={storage}
			{...(onClearAndReload !== undefined ? { onClearAndReload } : {})}
		>
			<ConnectorProvider value={connector}>
				<DispatcherProvider dispatcher={dispatcher}>
					<PanelViewerProvider value={workstationApi}>
						<HoverCloseContext.Provider value={hoverClose}>
							<PanelSessionRegistryProvider value={panelRegistry}>
								<ShellRefLinkProvider>
									<div style={rootStyle}>
										<ShellHeader
											connector={connector}
											{...(debugUiUrl !== undefined ? { debugUiUrl } : {})}
										/>
										<div style={rowStyle}>
											{sidebarCollapsed ? (
												<OpenPanelsSidebar
													api={dockviewApi}
													collapsed
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
														api={dockviewApi}
														collapsed={false}
														onToggleCollapsed={toggleSidebarCollapsed}
														sidebarButtons={sidebarButtons}
													/>
												</ResizableSidebar>
											)}
											<div style={dockviewWrapperStyle}>
												<DockviewLayout
													onReady={onReady}
													workstationApi={workstationApi}
													panelRegistrations={panelRegistrations}
												/>
											</div>
										</div>
									</div>
								</ShellRefLinkProvider>
							</PanelSessionRegistryProvider>
						</HoverCloseContext.Provider>
					</PanelViewerProvider>
				</DispatcherProvider>
			</ConnectorProvider>
		</WaitForDispatcherReady>
	);
}

/**
 * Wait for the dispatcher to reach `"connected"` before rendering
 * its children. The dispatch state machines call
 * `ctx.allocateLoad` which throws when the underlying session
 * lifecycle isn't `"connected"` — the easiest way to avoid that
 * race is to wait here.
 *
 *   - `"connected"` → render children.
 *   - `"unavailable"` → render `DispatcherUnavailable`. This is
 *     the genuine "server has dropped the session" case (e.g.
 *     server restart). We don't auto-recover; the user clears
 *     local state and reloads via the body's button.
 *   - Any other state (`"connecting"`, `"stale"`, `"closed"`) →
 *     "Connecting…" placeholder. `"closed"` is treated as
 *     transient because dispatcher-session `"closed"` typically
 *     only happens via the consumer's `dispatcher.detach()` in
 *     teardown; the workstation server never sends
 *     `state_update.closed=true` for the dispatcher session
 *     unprompted.
 */
function WaitForDispatcherReady({
	dispatcher,
	storage,
	onClearAndReload,
	children,
}: {
	readonly dispatcher: Dispatcher;
	readonly storage: Storage;
	readonly onClearAndReload?: () => void;
	readonly children: React.ReactNode;
}): React.ReactElement {
	const status = useAtomValue(dispatcher.status);
	if (status === "unavailable") {
		return (
			<DispatcherUnavailable
				onClearAndReload={() => clearShellLayoutAndReload(storage, onClearAndReload)}
			/>
		);
	}
	if (status !== "connected") {
		return <ShellLoading message="Connecting…" />;
	}
	return <>{children}</>;
}

/**
 * Body rendered when the dispatcher session is unavailable
 * (workstation server has no record of it — typically because
 * the server has restarted since our last visit). Offers a
 * single action: wipe local state and reload, after which the
 * shell creates a fresh session against the now-running server.
 */
function DispatcherUnavailable({
	onClearAndReload,
}: {
	readonly onClearAndReload: () => void;
}): React.ReactElement {
	return (
		<div style={unavailableRootStyle}>
			<div style={unavailableCardStyle}>
				<div style={unavailableHeadingStyle}>Workstation session no longer available</div>
				<div style={unavailableBodyStyle}>
					The dispatcher session this tab was using has been dropped — most likely because the
					workstation server restarted. Click below to clear the saved local state (dispatcher
					session id + dockview layout) and reload.
				</div>
				<button type="button" style={unavailableButtonStyle} onClick={onClearAndReload}>
					Clear local state and reload
				</button>
			</div>
		</div>
	);
}

/**
 * Wipe the shell's own persisted state (panel layout, in the injected
 * `storage`) and defer to the consumer's `onClearAndReload` for any
 * consumer-owned state (dispatcher session id) and the actual page
 * reload. When the consumer didn't supply a callback the shell falls
 * back to reloading the page itself so the user isn't stranded on a
 * broken card; consumers who want different behavior just supply their
 * own callback that handles the reload (or doesn't).
 */
function clearShellLayoutAndReload(storage: Storage, onClearAndReload?: () => void): void {
	clearPersistedLayout(storage);
	if (onClearAndReload !== undefined) {
		onClearAndReload();
		return;
	}
	if (typeof window !== "undefined") {
		window.location.reload();
	}
}

function ShellLoading({ message }: { readonly message: string }) {
	return (
		<div style={loadingStyle}>
			<div style={loadingMessageStyle}>{message}</div>
		</div>
	);
}

const rootStyle: CSSProperties = {
	height: "100%",
	display: "flex",
	flexDirection: "column",
};

const rowStyle: CSSProperties = {
	flex: "1 1 auto",
	minHeight: 0,
	display: "flex",
	flexDirection: "row",
};

const dockviewWrapperStyle: CSSProperties = {
	flex: "1 1 auto",
	minWidth: 0,
	display: "flex",
	flexDirection: "column",
};

const loadingStyle: CSSProperties = {
	height: "100%",
	display: "flex",
	alignItems: "center",
	justifyContent: "center",
	color: "var(--text-muted, #888)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm, 12px)",
	background: "var(--surface-card)",
};

const loadingMessageStyle: CSSProperties = {
	padding: "var(--sp-4)",
};

const unavailableRootStyle: CSSProperties = {
	height: "100%",
	display: "flex",
	alignItems: "center",
	justifyContent: "center",
	padding: "var(--sp-4)",
	background: "var(--surface-card)",
};

const unavailableCardStyle: CSSProperties = {
	maxWidth: "440px",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3)",
	padding: "var(--sp-5, 24px)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body)",
};

const unavailableHeadingStyle: CSSProperties = {
	fontWeight: 600,
	fontSize: "var(--text-body)",
	color: "var(--status-error, #c00)",
};

const unavailableBodyStyle: CSSProperties = {
	color: "var(--text-secondary)",
	lineHeight: 1.5,
	fontSize: "var(--text-body-sm)",
};

const unavailableButtonStyle: CSSProperties = {
	alignSelf: "flex-start",
	padding: "var(--sp-2) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	cursor: "pointer",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
};
