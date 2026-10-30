/**
 * `RefLinkImplContext` — the React-context indirection that lets
 * the surrounding shell (or any other consumer) install custom
 * behaviour around dispatched ref links without
 * `@fxtr-ui/workstation-dispatch` needing to know about the shell's
 * `WorkstationApi` / `PanelContext` / `@base-ui/react` machinery.
 *
 * The context value is a `React.ComponentType<RefLinkProps>` — the
 * impl receives the user's `<RefLink>` props directly and is free
 * to render whatever it wants around the dispatched content
 * (drag handlers, hover preview, context menu, telemetry,
 * watermarks, …). The default impl just dispatches via
 * `<RenderDispatch>` with no extra wrappers, suitable for tests
 * and headless rendering.
 *
 * The shell-side impl lives in `@fxtr-ui/workstation`'s
 * `ShellRefLinkImpl.tsx` and is mounted via
 * `<RefLinkImplContext.Provider>` near the top of the
 * `<WorkstationShell>` tree.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { type ReactNode, createContext } from "react";
import { BlockLink, InlineLink, type LinkProps } from "../dispatch/keys.js";

export interface RefLinkProps extends LinkProps {
	readonly stub: EntityRefStub | ServiceRefStub;
	readonly as?: "inline" | "block";
}

/**
 * A `RefLinkImpl` is a React component that takes `RefLinkProps`
 * and renders the ref. Implementations are free to wrap the
 * dispatched output with whatever JSX they want (drag handlers,
 * hover popovers, context menus, etc.) — `RefLink` just forwards
 * its props to whichever impl is currently in context.
 */
export type RefLinkImpl = (props: RefLinkProps) => ReactNode;

/**
 * Default impl: render the dispatched link bare, no interaction
 * wrappers. Active when no `<RefLinkImplContext.Provider>` is
 * present in the tree (e.g. in unit tests, or in a renderer
 * module loaded outside a shell). `as="block"` (the default)
 * routes to `BlockLink`; `as="inline"` routes to `InlineLink`.
 */
export const DefaultRefLinkImpl: RefLinkImpl = (props) => {
	const { stub, as = "block", ...linkProps } = props;
	const key = as === "inline" ? InlineLink : BlockLink;
	return <RenderDispatch stub={stub} as={key} {...linkProps} />;
};

/**
 * React context carrying the active `RefLinkImpl`. The default
 * value is `DefaultRefLinkImpl`; consumers (typically the
 * workstation shell) override via
 * `<RefLinkImplContext.Provider value={ShellRefLinkImpl}>`.
 */
export const RefLinkImplContext = createContext<RefLinkImpl>(DefaultRefLinkImpl);
