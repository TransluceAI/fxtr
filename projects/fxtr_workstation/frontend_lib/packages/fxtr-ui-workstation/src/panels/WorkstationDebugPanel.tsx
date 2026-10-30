/**
 * `WorkstationDebugPanel` — dockview content renderer for the
 * workstation-level debug surface.
 *
 * Stateless: subscribes to the four connector resource lists in
 * `debug=true` mode and renders one table each. The sessions
 * table's "open debug" button and the "Open dispatcher debug"
 * button open their panels via the registry-driven
 * `workstation.openPanel(id, params?)` API (keyed by the panel's
 * registration component id).
 *
 * Does NOT provide `PanelContext`. Click / drag / right-click on
 * `DebugRefChip` instances inside the tables therefore route
 * through `restoreIntoNewPanel`, opening a new ref panel rather
 * than mutating any source panel's chain.
 */

import { usePanelViewer } from "@fxtr-ui/panel-viewer";
import { useDispatcher } from "@fxtr-ui/react-dispatch";
import type { IDockviewPanelProps } from "dockview";
import { useAtomValue } from "jotai";
import { type CSSProperties, useMemo } from "react";
import { useWorkstationConnector } from "../useWorkstationConnector.js";
import { DISPATCHER_DEBUG_PANEL_COMPONENT } from "./DispatcherDebugPanelParams.js";
import { SESSION_DEBUG_PANEL_COMPONENT } from "./SessionDebugPanelParams.js";
import type { WorkstationDebugPanelParams } from "./WorkstationDebugPanelParams.js";
import { EntitiesDebugTable } from "./tables/EntitiesDebugTable.js";
import { RpcConnectionsDebugTable } from "./tables/RpcConnectionsDebugTable.js";
import { ServicesDebugTable } from "./tables/ServicesDebugTable.js";
import { SessionsDebugTable } from "./tables/SessionsDebugTable.js";

export function WorkstationDebugPanel(_props: IDockviewPanelProps<WorkstationDebugPanelParams>) {
	const connector = useWorkstationConnector();
	const workstation = usePanelViewer();
	const dispatcher = useDispatcher();

	const rpcList = useMemo(() => connector.listRpcConnections({ debug: true }), [connector]);
	const serviceList = useMemo(() => connector.listServices({ debug: true }), [connector]);
	const entityList = useMemo(() => connector.listEntities({ debug: true }), [connector]);
	const sessionList = useMemo(() => connector.listSessions({ debug: true }), [connector]);

	const rpcConnections = useAtomValue(rpcList.records);
	const services = useAtomValue(serviceList.records);
	const entities = useAtomValue(entityList.records);
	const sessions = useAtomValue(sessionList.records);

	// Only surface the dispatcher-debug affordance when the
	// dispatcher actually exposes a debug surface. Static
	// dispatchers always do; dynamic dispatchers always do; future
	// dispatchers may omit it.
	const hasDispatcherDebug = dispatcher.debugView !== undefined;

	return (
		<div className="fw-panel" style={bodyStyle}>
			<h2 style={titleStyle}>Workstation debug</h2>
			{hasDispatcherDebug && (
				<div>
					<button
						type="button"
						style={debugButtonStyle}
						onClick={() => workstation.openPanel(DISPATCHER_DEBUG_PANEL_COMPONENT)}
						data-testid="open-dispatcher-debug"
					>
						Open dispatcher debug
					</button>
				</div>
			)}
			<RpcConnectionsDebugTable records={rpcConnections} />
			<ServicesDebugTable records={services} />
			<EntitiesDebugTable records={entities} />
			<SessionsDebugTable
				records={sessions}
				onOpenSessionDebug={(sessionId) =>
					workstation.openPanel(SESSION_DEBUG_PANEL_COMPONENT, { kind: "session_debug", sessionId })
				}
			/>
		</div>
	);
}

const debugButtonStyle: CSSProperties = {
	padding: "var(--sp-2) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	cursor: "pointer",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
	alignSelf: "flex-start",
};

const bodyStyle: CSSProperties = {
	height: "100%",
	overflow: "auto",
	padding: "var(--sp-4)",
	fontFamily: "var(--font-prose)",
	color: "var(--text-primary)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-5, 20px)",
};

const titleStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 18px)",
	color: "var(--text-primary)",
};
