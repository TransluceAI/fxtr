/**
 * NamespaceContext — provides the `FxtrReadNamespace` the workstation
 * was mounted with to descendants.
 *
 * Motivation: dockview panel params are round-tripped through JSON when
 * the layout is persisted + restored, which strips class methods. We
 * therefore keep rich class instances (like the namespace) out of panel
 * params entirely and route them through React context instead.
 */

import type { FxtrReadNamespace } from "@fxtr/core";
import { type ReactNode, createContext, useContext } from "react";

const NamespaceContext = createContext<FxtrReadNamespace | null>(null);

export function NamespaceContextProvider({
	namespace,
	children,
}: {
	namespace: FxtrReadNamespace;
	children: ReactNode;
}) {
	return <NamespaceContext.Provider value={namespace}>{children}</NamespaceContext.Provider>;
}

export function useNamespace(): FxtrReadNamespace {
	const ns = useContext(NamespaceContext);
	if (ns === null) {
		throw new Error(
			"useNamespace() called outside a <NamespaceContextProvider>. " +
				"The workstation shell should wrap its subtree in one.",
		);
	}
	return ns;
}
