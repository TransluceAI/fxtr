/**
 * Dispatch demo entry. Spins up one `WorkstationConnector`, lazily
 * creates a dedicated dispatcher session, wraps it in a
 * `StaticDispatcher`, and exposes that through
 * `<DispatcherProvider>` so `<DispatchGrid>` can render dispatched
 * cards via `<RenderDispatch>`.
 *
 * The dispatcher session is created lazily on mount (async via
 * `connector.createSession`). Until the create round-trip
 * completes, the page shows a small "creating session…"
 * placeholder. Once the session is in hand we construct a static
 * dispatcher around it and mount the provider for the lifetime of
 * the page.
 *
 * Ownership transfer: `createStaticDispatcher({session})` takes
 * lifecycle ownership of the session. We never call
 * `session.detach()` ourselves once the dispatcher is built;
 * `dispatcher.detach()` does it as part of cleanup.
 *
 * Single connector store is shared across the React tree (via
 * `<JotaiProvider store={connector.store}>`); the dispatcher
 * session reuses that store by default. This satisfies the
 * `@fxtr-ui/react-dispatch` single-store assumption.
 */

import { WorkstationConnector } from "@fxtr-ui/connector-workstation";
import {
	type Dispatcher,
	DispatcherProvider,
	createStaticDispatcher,
} from "@fxtr-ui/react-dispatch";
import { Provider as JotaiProvider } from "jotai";
import { useEffect, useState } from "react";
import { ConnectionStatusBar } from "./ConnectionStatusBar";
import { DispatchGrid } from "./DispatchGrid";
import { dispatchRegistry } from "./dispatch_registry";

const WORKSTATION_URL = import.meta.env.VITE_WORKSTATION_URL;
const WORKSTATION_DEBUG_UI_URL = import.meta.env.VITE_WORKSTATION_DEBUG_UI_URL ?? null;

if (!WORKSTATION_URL) {
	throw new Error(
		"VITE_WORKSTATION_URL is not set. Run via `pnpm dev` so the orchestrator injects it.",
	);
}

// Module-level singleton, same pattern as `@fxtr-ui/demo`. Avoids
// StrictMode dev-only double-invoke effects tearing the connector
// down before its WebSocket finishes opening.
console.log(`[dispatch-demo] connecting to workstation: ${WORKSTATION_URL}`);
const connector = new WorkstationConnector({ url: WORKSTATION_URL });

export function App() {
	const [dispatcher, setDispatcher] = useState<Dispatcher | null>(null);
	const [createError, setCreateError] = useState<unknown>(null);

	useEffect(() => {
		let cancelled = false;
		let built: Dispatcher | null = null;
		connector
			.createSession({})
			.then((session) => {
				console.log(`[dispatch-demo] dispatcher session: ${session.sessionId}`);
				built = createStaticDispatcher({ session, registry: dispatchRegistry });
				if (cancelled) {
					void built.detach();
					return;
				}
				setDispatcher(built);
			})
			.catch((err) => {
				if (cancelled) return;
				console.error("[dispatch-demo] failed to create dispatcher session:", err);
				setCreateError(err);
			});
		return () => {
			cancelled = true;
			if (built !== null) void built.detach();
		};
	}, []);

	return (
		<JotaiProvider store={connector.store}>
			<div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
				<ConnectionStatusBar
					connector={connector}
					url={WORKSTATION_URL}
					debugUiUrl={WORKSTATION_DEBUG_UI_URL}
				/>
				{createError ? (
					<div style={{ padding: "16px", color: "#c00" }}>
						Failed to create dispatcher session: {String(createError)}
					</div>
				) : dispatcher === null ? (
					<div style={{ padding: "16px", color: "#888", fontStyle: "italic" }}>
						creating dispatcher session…
					</div>
				) : (
					<div style={{ flex: 1, overflow: "auto", background: "#fafafa" }}>
						<DispatcherProvider dispatcher={dispatcher}>
							<DispatchGrid connector={connector} />
						</DispatcherProvider>
					</div>
				)}
			</div>
		</JotaiProvider>
	);
}
