/**
 * ``RefStubResolver`` — context-level indirection that lets renderer
 * modules mint connector-interned ``EntityRefStub`` /
 * ``ServiceRefStub`` instances by ``shortId``, and disambiguate a
 * bare ``shortId`` between entity and service namespaces.
 *
 * Today only the connector (``WorkstationConnector``) exposes these
 * affordances; the connector itself lives in
 * ``@fxtr-ui/connector-workstation`` and isn't reachable from
 * externalised renderer modules. The shell installs a concrete
 * impl (``ShellRefStubResolver``) that wraps the connector and
 * exposes the methods through this context.
 *
 * Mirrors the ``RefLinkImplContext`` indirection pattern: dispatch
 * package owns the surface, shell owns the impl, renderer modules
 * consume via the hook.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { createContext, useContext } from "react";

export interface RefStubResolver {
	/** Return the connector-interned entity stub for ``shortId``. */
	readonly entityRef: (shortId: string) => EntityRefStub;
	/** Return the connector-interned service stub for ``shortId``. */
	readonly serviceRef: (shortId: string) => ServiceRefStub;
	/**
	 * Look up which namespace a ``shortId`` belongs to. Returns
	 * ``null`` if it's not in either of the connector's resource
	 * lists at the time of call (e.g. the workstation doesn't know
	 * about it, or the resource lists haven't loaded yet).
	 */
	readonly classifyShortId: (shortId: string) => "entity" | "service" | null;
}

/**
 * Null default: outside a ``<RefStubResolverContext.Provider>``,
 * ``useRefStubResolver()`` returns ``null`` and consumer code should
 * degrade gracefully (e.g. render unresolved ``@[shortid]`` text
 * literally rather than crashing). Renderer modules used inside the
 * workstation shell always see a non-null value.
 */
export const RefStubResolverContext = createContext<RefStubResolver | null>(null);

/**
 * Hook returning the active ``RefStubResolver``, or ``null`` outside
 * a provider. Renderer modules are expected to handle both cases.
 */
export function useRefStubResolver(): RefStubResolver | null {
	return useContext(RefStubResolverContext);
}
