/**
 * ConnectorContext — exposes the shell's `Connector` to
 * descendants. `PanelWrapper` reads it to mint / attach panel
 * sessions; `RootsPanel` reads it to subscribe to the connector's
 * resource lists; `RefLink` doesn't (the connector is reached
 * through dispatch via the abstract `Session` already in scope).
 *
 * Provider: `PanelViewer` (planned). Until then,
 * `useConnector()` throws — descendants that depend on it can
 * only be exercised inside a shell-mounted tree.
 *
 * Kept as a separate small context so consumers who only need
 * the connector (e.g. an integration test wrapping a `PanelWrapper`
 * directly) can supply just this provider without standing up the
 * whole shell.
 */

import type { Connector } from "@fxtr-ui/core";
import { type ReactNode, createContext, useContext } from "react";

const ConnectorContext = createContext<Connector | null>(null);

export interface ConnectorProviderProps {
	readonly value: Connector;
	readonly children: ReactNode;
}

export function ConnectorProvider({ value, children }: ConnectorProviderProps) {
	return <ConnectorContext.Provider value={value}>{children}</ConnectorContext.Provider>;
}

export function useConnector(): Connector {
	const ctx = useContext(ConnectorContext);
	if (ctx === null) {
		throw new Error("useConnector must be used inside a <PanelViewer>");
	}
	return ctx;
}
