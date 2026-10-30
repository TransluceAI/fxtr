/**
 * Shell-side concrete impl of ``RefStubResolver`` (the dispatch-
 * package context surface that lets renderer modules mint
 * connector-interned stubs and disambiguate bare ``shortId``s).
 *
 * Wraps the connector's ``entityRef`` / ``serviceRef`` interning
 * surface and delegates ``classifyShortId`` to the connector (which
 * answers it authoritatively — the workstation by scanning its
 * always-loaded resource lists, the HTTP connector from its shortId
 * table). All three members are on the abstract ``Connector``, so
 * this resolver is connector-agnostic.
 *
 * Mounted by ``<ShellRefLinkProvider>`` alongside
 * ``<RefLinkImplContext.Provider>`` so both shell-only indirections
 * land in the same place.
 */

import type { Connector } from "@fxtr-ui/core";
import { type RefStubResolver, RefStubResolverContext } from "@fxtr-ui/workstation-dispatch";
import { type ReactNode, useMemo } from "react";
import { useConnector } from "../shell/ConnectorContext.js";

/**
 * Build a ``RefStubResolver`` over a connector. The resolver's
 * methods are arrow functions closing over the connector instance;
 * identity is stable for the lifetime of the connector.
 */
function makeShellRefStubResolver(connector: Connector): RefStubResolver {
	return {
		entityRef: (shortId) => connector.entityRef(shortId),
		serviceRef: (shortId) => connector.serviceRef(shortId),
		classifyShortId: (shortId) => connector.classifyShortId(shortId),
	};
}

/**
 * Provider that installs a shell-backed ``RefStubResolver`` into
 * ``RefStubResolverContext``. Reads the connector via
 * ``useConnector()``; must be mounted inside a
 * ``<ConnectorProvider>``.
 */
export function ShellRefStubResolverProvider({ children }: { readonly children: ReactNode }) {
	const connector = useConnector();
	const resolver = useMemo(() => makeShellRefStubResolver(connector), [connector]);
	return (
		<RefStubResolverContext.Provider value={resolver}>{children}</RefStubResolverContext.Provider>
	);
}
