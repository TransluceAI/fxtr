/**
 * Dynamic-dispatch demo entry. Serves two routes:
 *
 *   - Default (no query string): the legacy card-grid layout that
 *     renders each demo entity through the demo's `CardKey`. The
 *     workstation-side `dynamic_integration.test.ts` exercises
 *     this path — polling `noteEntry.componentAtom` for the
 *     compiled NoteCard / PhotoCard sentinel.
 *
 *   - `?shell=1`: the workstation-shell layout. Constructs the
 *     same `DynamicDispatcher` and hands it to `<PanelViewer>`
 *     from `@fxtr-ui/workstation`. Dispatched entities render
 *     through the four shell dispatch keys (FullPanel / BlockLink
 *     / InlineLink / HoverPreview) using the same dynamic
 *     renderer modules — the templates export entries for both
 *     the legacy CardKey and the shell keys.
 *
 * Both routes share the module-level `WorkstationConnector`
 * (StrictMode-safe singleton). Each route owns its own dispatcher
 * session lifecycle.
 *
 * Ownership transfer: `createDynamicDispatcher` takes the session;
 * we never call `session.detach()` ourselves once the dispatcher
 * is built — `dispatcher.detach()` does that on cleanup.
 *
 * Externals are installed in `main.tsx` BEFORE this module loads
 * (so the import-map is in the document before the first
 * renderer-bytes import).
 */

import { WorkstationConnector, type WorkstationSession } from "@fxtr-ui/connector-workstation";
import {
	type Dispatcher,
	DispatcherProvider,
	createDynamicDispatcher,
} from "@fxtr-ui/react-dispatch";
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
import { DispatchGrid } from "./DispatchGrid";

const WORKSTATION_URL = import.meta.env.VITE_WORKSTATION_URL;
const RENDERER_DISPATCHER_NAME = import.meta.env.VITE_RENDERER_DISPATCHER_NAME;

if (!WORKSTATION_URL) {
	throw new Error(
		"VITE_WORKSTATION_URL is not set. Run via `pnpm dev` so the orchestrator injects it.",
	);
}
if (!RENDERER_DISPATCHER_NAME) {
	throw new Error(
		"VITE_RENDERER_DISPATCHER_NAME is not set. Run via `pnpm dev` so the orchestrator injects it.",
	);
}

console.log(`[dynamic-dispatch-demo] connecting to workstation: ${WORKSTATION_URL}`);
const connector = new WorkstationConnector({ url: WORKSTATION_URL });

/**
 * Read `?shell=1` (or `?shell=true`) from the URL. SSR-safe: when
 * `window` is undefined, default to the legacy grid.
 */
function useShellRouteFlag(): boolean {
	if (typeof window === "undefined") return false;
	const params = new URLSearchParams(window.location.search);
	const flag = params.get("shell");
	return flag === "1" || flag === "true";
}

/**
 * Derive the workstation server's debug-UI URL by transforming
 * `ws://host:port/client` → `http://host:port/debug`. Returns
 * `null` if the URL doesn't parse.
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

const DEBUG_UI_URL = deriveDebugUiUrl(WORKSTATION_URL);

export function App() {
	const useShell = useShellRouteFlag();
	return useShell ? <ShellApp /> : <GridApp />;
}

// --- Legacy grid route ------------------------------------------------

function GridApp() {
	const [dispatcher, setDispatcher] = useState<Dispatcher | null>(null);
	const [createError, setCreateError] = useState<unknown>(null);

	useEffect(() => {
		let cancelled = false;
		let built: Dispatcher | null = null;
		(async () => {
			try {
				const serviceList = connector.listServices();
				await waitForRecord(serviceList, RENDERER_DISPATCHER_NAME);
				const records = await currentRecords(serviceList);
				const dispatcherRecord = records.find((r) =>
					r.external_names.includes(RENDERER_DISPATCHER_NAME),
				);
				if (dispatcherRecord === undefined) {
					throw new Error(
						`Could not find a service registered under external name "${RENDERER_DISPATCHER_NAME}" — the workstation backend didn't mount the renderer dispatcher.`,
					);
				}
				const dispatchServiceRef = connector.serviceRef(dispatcherRecord.short_id);
				const session = await connector.createSession({});
				console.log(`[dynamic-dispatch-demo] dispatcher session: ${session.sessionId}`);
				if (cancelled) {
					await session.detach();
					return;
				}
				built = createDynamicDispatcher({
					session,
					dispatchServiceRef,
					loadEntityData: (ref) => connector.loadEntityData(ref),
				});
				if (cancelled) {
					await built.detach();
					return;
				}
				setDispatcher(built);
			} catch (err) {
				if (cancelled) return;
				console.error("[dynamic-dispatch-demo] failed to set up dispatcher:", err);
				setCreateError(err);
			}
		})();
		return () => {
			cancelled = true;
			if (built !== null) void built.detach();
		};
	}, []);

	return (
		<JotaiProvider store={connector.store}>
			<div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
				<div
					style={{
						padding: "8px 12px",
						borderBottom: "1px solid #ddd",
						background: "#fafafa",
						fontSize: 12,
					}}
				>
					<code style={{ fontFamily: "ui-monospace, monospace" }}>{WORKSTATION_URL}</code> —
					dispatcher service: <code>{RENDERER_DISPATCHER_NAME}</code>
					{" — "}
					<a href="?shell=1">switch to shell</a>
				</div>
				{createError ? (
					<div style={{ padding: "16px", color: "#c00" }}>
						Failed to set up dispatcher: {String(createError)}
					</div>
				) : dispatcher === null ? (
					<div style={{ padding: "16px", color: "#888", fontStyle: "italic" }}>
						creating dispatcher session…
					</div>
				) : (
					<div style={{ flex: 1, overflow: "auto", background: "#fff" }}>
						<DispatcherProvider dispatcher={dispatcher}>
							<DispatchGrid connector={connector} />
						</DispatcherProvider>
					</div>
				)}
			</div>
		</JotaiProvider>
	);
}

// --- Workstation shell route -----------------------------------------

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

function ShellApp() {
	const [dispatcher, setDispatcher] = useState<Dispatcher | null>(null);
	const [createError, setCreateError] = useState<unknown>(null);

	useEffect(() => {
		let cancelled = false;
		let built: Dispatcher | null = null;
		(async () => {
			try {
				const serviceList = connector.listServices();
				await waitForRecord(serviceList, RENDERER_DISPATCHER_NAME);
				const records = await currentRecords(serviceList);
				const dispatcherRecord = records.find((r) =>
					r.external_names.includes(RENDERER_DISPATCHER_NAME),
				);
				if (dispatcherRecord === undefined) {
					throw new Error(
						`Could not find a service registered under external name "${RENDERER_DISPATCHER_NAME}" — the workstation backend didn't mount the renderer dispatcher.`,
					);
				}
				const dispatchServiceRef = connector.serviceRef(dispatcherRecord.short_id);

				// Dispatcher session lifecycle parallels the
				// workstation-demo's App.tsx: attach to persisted id
				// when present, else create a fresh one.
				let session: WorkstationSession;
				const persistedId = readDispatcherSessionId(localStorage);
				if (persistedId !== null) {
					session = connector.attachSession(persistedId);
				} else {
					session = await connector.createSession({ role: "dispatcher" });
					writeDispatcherSessionId(localStorage, session.sessionId);
				}
				if (cancelled) {
					await session.detach();
					return;
				}
				built = createDynamicDispatcher({
					session,
					dispatchServiceRef,
					loadEntityData: (ref) => connector.loadEntityData(ref),
				});
				if (cancelled) {
					await built.detach();
					return;
				}
				setDispatcher(built);
			} catch (err) {
				if (cancelled) return;
				console.error("[dynamic-dispatch-demo/shell] failed to set up dispatcher:", err);
				setCreateError(err);
			}
		})();
		return () => {
			cancelled = true;
			if (built !== null) void built.detach();
		};
	}, []);

	return (
		<JotaiProvider store={connector.store}>
			{createError ? (
				<div style={{ padding: "16px", color: "#c00" }}>
					Failed to set up dispatcher: {String(createError)}
				</div>
			) : dispatcher === null ? (
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

// --- Helpers ---------------------------------------------------------

/**
 * Wait until the given resource list has a record whose
 * `external_names` array contains the supplied name. The
 * connector's lists populate asynchronously after the WS
 * connection comes up.
 */
async function waitForRecord(
	list: ReturnType<WorkstationConnector["listServices"]>,
	externalName: string,
): Promise<void> {
	const store = connector.store;
	for (let i = 0; i < 100; i++) {
		const records = store.get(list.records);
		const hit = records.find((r) => r.external_names.includes(externalName));
		if (hit !== undefined) return;
		await new Promise<void>((resolve) => setTimeout(resolve, 50));
	}
	throw new Error(
		`Workstation didn't expose a service under external name "${externalName}" within 5 seconds.`,
	);
}

async function currentRecords(
	list: ReturnType<WorkstationConnector["listServices"]>,
): Promise<readonly { readonly short_id: string; readonly external_names: readonly string[] }[]> {
	const store = connector.store;
	return store.get(list.records) as readonly {
		readonly short_id: string;
		readonly external_names: readonly string[];
	}[];
}
