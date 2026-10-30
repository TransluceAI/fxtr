/**
 * The shared published-flake viewer: a full PanelViewer shell over the stateless
 * HTTP connector. Demo-agnostic — it serves any flake published by `fxtr_nix
 * serve` (entities + namespaces + a `dispatcher` ws service).
 *
 * - Mints one module-level `HttpConnector` against the backend base URL, sharing
 *   **one `sessionStorage` handle** with the PanelViewer so the connector's
 *   sessions + shortId table and the viewer's layout age together. The base URL
 *   defaults to `window.location.origin` — the served-static case (`fxtr_nix
 *   serve --web-dist`, same origin as the API) — and falls back to
 *   `VITE_PUBLISHED_BACKEND_URL` for the vite-dev case (HMR pointed at a
 *   separately-running serve).
 * - Discovers the WS `dispatcher` service by name, builds a dynamic dispatcher
 *   over it (resolve + reload-on-break hot reload), and mounts the PanelViewer
 *   with the generic panel registrations, opening on Roots.
 */

import { HttpConnector } from "@fxtr-ui/connector-http";
import {
	PanelViewer,
	clearDispatcherSessionId,
	readDispatcherSessionId,
	writeDispatcherSessionId,
} from "@fxtr-ui/panel-viewer";
import { type Dispatcher, createDynamicDispatcher } from "@fxtr-ui/react-dispatch";
import { Provider as JotaiProvider } from "jotai";
import { useEffect, useState } from "react";
import { ROOTS_PANEL_ID, makePanelRegistrations } from "./panels";

// Same-origin by default (served by `fxtr_nix serve --web-dist`); the env var
// override is for `vite dev` pointed at a separately-running serve.
const BACKEND_URL =
	import.meta.env.VITE_PUBLISHED_BACKEND_URL ??
	(typeof window !== "undefined" ? window.location.origin : "");

const DISPATCHER_SERVICE_NAME = "dispatcher";

// One shared Storage handle for both the connector and the viewer.
// sessionStorage: per-tab, cleared when the tab closes.
const storage: Storage = sessionStorage;

// Module-level singleton (StrictMode-safe — avoids tearing the connector down
// between the dev-mode double-invoke of effects).
const connector = new HttpConnector({ baseUrl: BACKEND_URL, storage });

const panelRegistrations = makePanelRegistrations(storage);

function clearAndReload(): void {
	clearDispatcherSessionId(storage);
	if (typeof window !== "undefined") window.location.reload();
}

export function App() {
	const [dispatcher, setDispatcher] = useState<Dispatcher | null>(null);
	const [error, setError] = useState<unknown>(null);

	useEffect(() => {
		let cancelled = false;
		let built: Dispatcher | null = null;

		void (async () => {
			try {
				const serviceList = connector.listServices();
				const dispatcherRef = await waitForServiceRef(serviceList, DISPATCHER_SERVICE_NAME);
				if (cancelled) return;

				const persistedId = readDispatcherSessionId(storage);
				const session =
					persistedId !== null
						? connector.attachSession(persistedId)
						: await (async () => {
								const s = await connector.createSession({ role: "dispatcher" });
								writeDispatcherSessionId(storage, s.sessionId);
								return s;
							})();
				if (cancelled) return;

				built = createDynamicDispatcher({
					session,
					dispatchServiceRef: dispatcherRef,
					loadEntityData: (ref) => connector.loadEntityData(ref),
				});
				if (cancelled) {
					void built.detach();
					return;
				}
				setDispatcher(built);
			} catch (err) {
				if (!cancelled) {
					console.error("[published-app] dispatcher setup failed:", err);
					setError(err);
				}
			}
		})();

		return () => {
			cancelled = true;
			if (built !== null) void built.detach();
		};
	}, []);

	if (error !== null) {
		return <Centered>Failed to set up dispatcher: {String(error)}</Centered>;
	}
	if (dispatcher === null) {
		return <Centered italic>Connecting…</Centered>;
	}
	return (
		<JotaiProvider store={connector.store}>
			<PanelViewer
				connector={connector}
				dispatcher={dispatcher}
				panelRegistrations={panelRegistrations}
				defaultPanelId={ROOTS_PANEL_ID}
				storage={storage}
				onClearAndReload={clearAndReload}
			/>
		</JotaiProvider>
	);
}

/**
 * Poll the (fetch-on-connect) service list until a record with `name` appears,
 * returning its interned `ServiceRefStub`. The HTTP connector populates the list
 * asynchronously after the discovery fetch.
 */
async function waitForServiceRef(
	list: ReturnType<HttpConnector["listServices"]>,
	name: string,
): Promise<ReturnType<HttpConnector["serviceRef"]>> {
	for (let i = 0; i < 200; i++) {
		const rec = connector.store.get(list.records).find((r) => r.name === name);
		if (rec !== undefined) return rec.ref;
		await new Promise<void>((resolve) => setTimeout(resolve, 50));
	}
	throw new Error(`backend exposed no service named "${name}" within 10s`);
}

function Centered({
	children,
	italic,
}: {
	readonly children: React.ReactNode;
	readonly italic?: boolean;
}) {
	return (
		<div
			style={{
				height: "100%",
				display: "flex",
				alignItems: "center",
				justifyContent: "center",
				color: "#888",
				...(italic ? { fontStyle: "italic" } : {}),
			}}
		>
			{children}
		</div>
	);
}
