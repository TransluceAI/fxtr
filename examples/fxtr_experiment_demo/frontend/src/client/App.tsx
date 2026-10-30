/**
 * Top-level component. Wires up a :class:`WorkstationConnector`
 * against the env-injected ``/client`` URL, waits for the demo's
 * renderer dispatcher to appear under its well-known external
 * name, allocates a :class:`Dispatcher` via
 * :func:`createDynamicDispatcher` against that service, and
 * renders :class:`PanelViewer`.
 *
 * Same overall lifecycle shape as
 * ``@fxtr-ui/dynamic-dispatch-demo/ShellApp`` — attach to a
 * persisted dispatcher session when present, else create a fresh
 * one; on detach, ``dispatcher.detach()`` tears down the
 * underlying ``WorkstationSession``.
 */

import { WorkstationConnector, type WorkstationSession } from "@fxtr-ui/connector-workstation";
import { type Dispatcher, createDynamicDispatcher } from "@fxtr-ui/react-dispatch";
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

const WORKSTATION_URL = import.meta.env.VITE_WORKSTATION_URL;
const DEBUG_UI_URL = import.meta.env.VITE_WORKSTATION_DEBUG_UI_URL;
const RENDERER_DISPATCHER_NAME = import.meta.env.VITE_RENDERER_DISPATCHER_NAME;

if (typeof WORKSTATION_URL !== "string" || WORKSTATION_URL.length === 0) {
	throw new Error(
		"VITE_WORKSTATION_URL is not set. Run via the orchestrator (`pnpm dev` or `./start_demo.sh`).",
	);
}
if (typeof RENDERER_DISPATCHER_NAME !== "string" || RENDERER_DISPATCHER_NAME.length === 0) {
	throw new Error(
		"VITE_RENDERER_DISPATCHER_NAME is not set. Run via the orchestrator (`pnpm dev` or `./start_demo.sh`).",
	);
}

console.log(`[demo] connecting to workstation: ${WORKSTATION_URL}`);
console.log(`[demo] renderer dispatcher external name: ${RENDERER_DISPATCHER_NAME}`);
const connector = new WorkstationConnector({ url: WORKSTATION_URL });

function clearAndReload(): void {
	clearDispatcherSessionId(localStorage);
	if (typeof window !== "undefined") {
		window.location.reload();
	}
}

export function App() {
	const [dispatcher, setDispatcher] = useState<Dispatcher | null>(null);
	const [createError, setCreateError] = useState<unknown>(null);

	useEffect(() => {
		let cancelled = false;
		let built: Dispatcher | null = null;

		(async () => {
			try {
				// Wait for the renderer-dispatcher service to appear
				// in the workstation's service list. The connector's
				// list populates asynchronously after the WS handshake.
				const serviceList = connector.listServices();
				await waitForRecord(serviceList, RENDERER_DISPATCHER_NAME);
				const records = currentRecords(serviceList);
				const dispatcherRecord = records.find((r) =>
					r.external_names.includes(RENDERER_DISPATCHER_NAME),
				);
				if (dispatcherRecord === undefined) {
					throw new Error(
						`Could not find a service registered under external name "${RENDERER_DISPATCHER_NAME}" — the workstation backend didn't mount the renderer dispatcher.`,
					);
				}
				const dispatchServiceRef = connector.serviceRef(dispatcherRecord.short_id);

				// Dispatcher session lifecycle: attach to the
				// persisted id if present, else create fresh.
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
				console.error("[demo] failed to set up dispatcher:", err);
				setCreateError(err);
			}
		})();

		return () => {
			cancelled = true;
			if (built !== null) {
				void built.detach();
			}
		};
	}, []);

	return (
		<JotaiProvider store={connector.store}>
			{createError !== null ? (
				<div
					style={{
						height: "100%",
						display: "flex",
						alignItems: "center",
						justifyContent: "center",
						color: "#c00",
						padding: 16,
					}}
				>
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
					{...(typeof DEBUG_UI_URL === "string" && DEBUG_UI_URL.length > 0
						? { debugUiUrl: DEBUG_UI_URL }
						: {})}
				/>
			)}
		</JotaiProvider>
	);
}

// ---------- Helpers (forked from dynamic-dispatch-demo) ----------

async function waitForRecord(
	list: ReturnType<WorkstationConnector["listServices"]>,
	externalName: string,
): Promise<void> {
	const store = connector.store;
	for (let i = 0; i < 100; i++) {
		const records = store.get(list.records) as readonly {
			readonly external_names: readonly string[];
		}[];
		if (records.some((r) => r.external_names.includes(externalName))) return;
		await new Promise<void>((resolve) => setTimeout(resolve, 50));
	}
	throw new Error(
		`Workstation didn't expose a service under external name "${externalName}" within 5 seconds.`,
	);
}

function currentRecords(
	list: ReturnType<WorkstationConnector["listServices"]>,
): readonly { readonly short_id: string; readonly external_names: readonly string[] }[] {
	return connector.store.get(list.records) as readonly {
		readonly short_id: string;
		readonly external_names: readonly string[];
	}[];
}
