/**
 * Demo top-level. Spins up a single `WorkstationConnector` against
 * the URL the orchestrator (`server.ts`) injected via Vite's
 * `import.meta.env.VITE_WORKSTATION_URL`, wraps the connector-scope
 * UI in a jotai `<Provider store={connector.store}>`, and lays out
 * the three sub-regions: status bar across the top, sidebar on the
 * left, panel grid filling the rest.
 *
 * Also resolves the demo entity once at the App level (by name via
 * `connector.listEntities()`) and threads it down to each panel as
 * `loadTarget`, so individual panels don't redo the lookup.
 */

import { WorkstationConnector } from "@fxtr-ui/connector-workstation";
import type {
	EntityRecord,
	ServiceRecord,
	WorkstationSession,
} from "@fxtr-ui/connector-workstation";
import type { EntityRefStub } from "@fxtr-ui/core";
import { Provider as JotaiProvider, useAtomValue } from "jotai";
import { useMemo, useState } from "react";
import { ConnectionStatusBar } from "./ConnectionStatusBar";
import { type InvokeTarget, type LoadTarget, Panel } from "./Panel";
import { SessionSidebar } from "./SessionSidebar";
import type { _DemoServiceProto } from "./_demo_service_proto";

const DEMO_ENTITY_NAME = "demo-note";
const DEMO_SERVICE_NAME = "demo-service";

const WORKSTATION_URL = import.meta.env.VITE_WORKSTATION_URL;
const WORKSTATION_DEBUG_UI_URL = import.meta.env.VITE_WORKSTATION_DEBUG_UI_URL ?? null;

if (!WORKSTATION_URL) {
	throw new Error(
		"VITE_WORKSTATION_URL is not set. Run via `pnpm dev` so the demo orchestrator injects it.",
	);
}

// Module-level singleton. Keeping the connector out of React state
// avoids StrictMode's dev-only double-invoke of effects tearing it
// down before the WebSocket finishes opening, which would surface as
// "WebSocket is closed before the connection is established" and then
// `ConnectorClosedError` on every subsequent createSession.
// The connector lives for the page's lifetime; the browser closes
// the underlying socket on page-unload.
console.log(`[demo] connecting to workstation: ${WORKSTATION_URL}`);
const connector = new WorkstationConnector({ url: WORKSTATION_URL });

export interface OpenPanel {
	readonly id: number;
	readonly session: WorkstationSession;
}

export function App() {
	const [panels, setPanels] = useState<OpenPanel[]>([]);
	const [nextPanelId, setNextPanelId] = useState(1);

	// Resolve the demo entity once via the connector's resource list.
	// Pre-built `EntityRefStub` lives in `loadTarget`, threaded into
	// each Panel for its load button. `null` until the entity list
	// arrives over the wire (or if the workstation doesn't have a
	// matching entity exposed).
	const entityList = useMemo(() => connector.listEntities(), []);
	const entityRecords = useAtomValue(entityList.records) as readonly EntityRecord[];
	const loadTarget: LoadTarget | null = useMemo(() => {
		const record = entityRecords.find((r) => r.external_names.includes(DEMO_ENTITY_NAME));
		if (!record) return null;
		const ref: EntityRefStub = connector.entityRef(record.short_id);
		return { ref, name: DEMO_ENTITY_NAME, shortId: record.short_id };
	}, [entityRecords]);

	// Same shape for the demo service. Threaded into Panel for the
	// invoke buttons.
	const serviceList = useMemo(() => connector.listServices(), []);
	const serviceRecords = useAtomValue(serviceList.records) as readonly ServiceRecord[];
	const invokeTarget: InvokeTarget | null = useMemo(() => {
		const record = serviceRecords.find((r) => r.external_names.includes(DEMO_SERVICE_NAME));
		if (!record) return null;
		// Typed via `_DemoServiceProto` so `ctx.allocateInvoke(ref,
		// "echo", [42])` checks method name + arg shape + return type
		// at compile time. Mirrors the Python `_DemoServiceProto`.
		const ref = connector.serviceRef<_DemoServiceProto>(record.short_id);
		return { ref, name: DEMO_SERVICE_NAME, shortId: record.short_id };
	}, [serviceRecords]);

	const openSession = (session: WorkstationSession): void => {
		console.log(`[demo] open panel for session ${session.sessionId}`);
		setPanels((p) => [...p, { id: nextPanelId, session }]);
		setNextPanelId((n) => n + 1);
	};
	const closePanel = (id: number): void => {
		setPanels((p) => {
			const panel = p.find((x) => x.id === id);
			if (panel) {
				console.log(`[demo] detach session ${panel.session.sessionId} (panel ${id})`);
				void panel.session.detach();
			}
			return p.filter((x) => x.id !== id);
		});
	};

	return (
		<JotaiProvider store={connector.store}>
			<div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
				<ConnectionStatusBar
					connector={connector}
					url={WORKSTATION_URL}
					debugUiUrl={WORKSTATION_DEBUG_UI_URL}
				/>
				<div style={{ display: "flex", flex: 1, minHeight: 0 }}>
					<SessionSidebar connector={connector} onOpen={openSession} />
					<div
						style={{
							flex: 1,
							padding: "12px",
							overflow: "auto",
							display: "grid",
							gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
							gap: "12px",
							alignContent: "start",
							background: "#fafafa",
						}}
					>
						{panels.length === 0 ? (
							<div style={{ color: "#888", padding: "8px" }}>
								No panels open. Create a session and click "open" in the sidebar.
							</div>
						) : (
							panels.map((p) => (
								<Panel
									key={p.id}
									session={p.session}
									loadTarget={loadTarget}
									invokeTarget={invokeTarget}
									onClose={() => closePanel(p.id)}
								/>
							))
						)}
					</div>
				</div>
			</div>
		</JotaiProvider>
	);
}
