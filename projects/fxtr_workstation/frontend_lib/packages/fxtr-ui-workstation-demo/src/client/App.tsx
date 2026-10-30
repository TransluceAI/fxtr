/**
 * Top-level component. Mints a single module-level
 * `WorkstationConnector` against the env-injected `/client` URL,
 * acquires + persists a dispatcher session, wraps it in a
 * `StaticDispatcher`, and renders the workstation shell with the
 * demo's dispatch registry.
 *
 * Dispatcher session lifecycle:
 *
 *   - On mount, read the dispatcher session id from `localStorage`
 *     (via `readDispatcherSessionId` from `@fxtr-ui/workstation`).
 *   - If present, attach synchronously. The server-side session
 *     survives reloads as long as someone keeps re-subscribing
 *     within the workstation's retention window.
 *   - If absent, async `createSession({role: "dispatcher"})` and
 *     persist the new id via `writeDispatcherSessionId` so the
 *     next reload re-attaches.
 *   - StrictMode-safe: an in-flight `createSession` that gets
 *     cancelled (effect cleanup re-fires) wraps its result in a
 *     dispatcher and immediately detaches — no leaked server-side
 *     session.
 *   - Cleanup: `dispatcher.detach()` (which detaches the session
 *     too, via the dispatcher's ownership transfer model).
 *
 * If the persisted id is dead server-side (server restarted while
 * the tab was closed), the session lifecycle reaches
 * `"unavailable"` and the shell renders a `DispatcherUnavailable`
 * card. Its "Clear local state and reload" button is wired to our
 * `onClearAndReload` callback, which clears the persisted id and
 * reloads — the next mount creates a fresh session.
 */

import { WorkstationConnector, type WorkstationSession } from "@fxtr-ui/connector-workstation";
import { type Dispatcher, createStaticDispatcher } from "@fxtr-ui/react-dispatch";
import {
	PanelViewer,
	ROOTS_PANEL_COMPONENT,
	clearDispatcherSessionId,
	readDispatcherSessionId,
	workstationPanelRegistrations,
	writeDispatcherSessionId,
} from "@fxtr-ui/workstation";
import { Provider as JotaiProvider } from "jotai";
import { useEffect, useState } from "react";
import { dispatchRegistry } from "./dispatch_registry";

const WORKSTATION_URL = import.meta.env.VITE_WORKSTATION_URL;

if (typeof WORKSTATION_URL !== "string" || WORKSTATION_URL.length === 0) {
	throw new Error(
		"VITE_WORKSTATION_URL is not set. Run via `pnpm dev` so the orchestrator injects it.",
	);
}

/**
 * Derive the workstation server's debug-UI URL by transforming
 * `ws://host:port/client` → `http://host:port/debug`. Returns
 * `null` if the URL doesn't parse. The shell renders the top
 * bar only when this is non-null; if the Python side didn't
 * actually mount the debug UI, the link returns a 404 but
 * there's no harm in showing it.
 */
function deriveDebugUiUrl(workstationUrl: string): string | null {
	try {
		const url = new URL(workstationUrl);
		if (url.pathname.endsWith("/client")) {
			url.pathname = url.pathname.slice(0, -"/client".length);
		}
		url.protocol = url.protocol === "wss:" ? "https:" : "http:";
		const base = url.toString().replace(/\/$/, "");
		return `${base}/debug`;
	} catch {
		return null;
	}
}

// Module-level singleton — same pattern as the sibling demos.
// Avoids StrictMode dev-only double-invoke effects tearing the
// connector down before its WebSocket finishes opening.
console.log(`[workstation-demo] connecting to workstation: ${WORKSTATION_URL}`);
const connector = new WorkstationConnector({ url: WORKSTATION_URL });
const DEBUG_UI_URL = deriveDebugUiUrl(WORKSTATION_URL);

/**
 * Wired into the shell's `DispatcherUnavailable` card. Clears the
 * persisted dispatcher session id and reloads — the next mount
 * starts fresh against the running server.
 */
function clearAndReload(): void {
	clearDispatcherSessionId(localStorage);
	if (typeof window !== "undefined") {
		window.location.reload();
	}
}

export function App() {
	const [dispatcher, setDispatcher] = useState<Dispatcher | null>(null);

	useEffect(() => {
		let cancelled = false;
		let built: Dispatcher | null = null;

		const persistedId = readDispatcherSessionId(localStorage);
		if (persistedId !== null) {
			const session = connector.attachSession(persistedId);
			built = createStaticDispatcher({ session, registry: dispatchRegistry });
			setDispatcher(built);
		} else {
			void (async () => {
				let session: WorkstationSession;
				try {
					session = await connector.createSession({ role: "dispatcher" });
				} catch (err) {
					console.error("[workstation-demo] dispatcher createSession failed:", err);
					return;
				}
				writeDispatcherSessionId(localStorage, session.sessionId);
				built = createStaticDispatcher({ session, registry: dispatchRegistry });
				if (cancelled) {
					// Orphan from a cancelled effect run; detach so we
					// don't leak the server-side session.
					void built.detach();
					return;
				}
				setDispatcher(built);
			})();
		}

		return () => {
			cancelled = true;
			if (built !== null) {
				void built.detach();
			}
		};
	}, []);

	return (
		<JotaiProvider store={connector.store}>
			{dispatcher === null ? (
				// Brief gap between mount and the dispatcher being
				// available — for persisted-id attach this is a single
				// frame; for fresh `createSession` it's the round-trip
				// duration. Once dispatcher is non-null, the shell's
				// own status-driven loading UI takes over.
				<div
					style={{
						height: "100%",
						display: "flex",
						alignItems: "center",
						justifyContent: "center",
						color: "#888",
						fontStyle: "italic",
					}}
				>
					Connecting…
				</div>
			) : (
				<PanelViewer
					connector={connector}
					dispatcher={dispatcher}
					panelRegistrations={workstationPanelRegistrations}
					defaultPanelId={ROOTS_PANEL_COMPONENT}
					storage={localStorage}
					onClearAndReload={clearAndReload}
					{...(DEBUG_UI_URL !== null ? { debugUiUrl: DEBUG_UI_URL } : {})}
				/>
			)}
		</JotaiProvider>
	);
}
