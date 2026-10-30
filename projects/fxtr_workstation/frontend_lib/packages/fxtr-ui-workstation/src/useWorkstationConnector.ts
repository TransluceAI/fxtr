/**
 * `useWorkstationConnector` — narrow the panel-viewer's abstract
 * `Connector` (provided via `useConnector()`) to the concrete
 * `WorkstationConnector` the workstation panels require.
 *
 * The generic viewer types its `ConnectorContext` against the abstract
 * `Connector`, but the workstation panels reach members beyond that
 * surface — the four `listX` resource lists and the raw `connection`
 * handle (used by the per-session debug panel's subscriptions). Those
 * panels only ever mount inside a `<PanelViewer>` given a
 * `WorkstationConnector`, so the downcast is sound.
 */

import type { WorkstationConnector } from "@fxtr-ui/connector-workstation";
import { useConnector } from "@fxtr-ui/panel-viewer";

export function useWorkstationConnector(): WorkstationConnector {
	return useConnector() as WorkstationConnector;
}
