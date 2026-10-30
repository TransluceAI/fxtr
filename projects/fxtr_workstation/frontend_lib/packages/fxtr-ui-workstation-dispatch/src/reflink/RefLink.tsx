/**
 * `RefLink` — the user-facing component for rendering an entity or
 * service ref as a link.
 *
 * Thin forwarder: reads a `RefLinkImpl` out of
 * `RefLinkImplContext` and renders it with the props it received.
 * The impl decides what to render around the dispatched content
 * (drag handlers, hover preview, context menu, …). The default
 * impl renders the dispatched content bare (see
 * `impl_context.tsx`).
 *
 * Consumers who want pure display without any interactions can
 * skip this component entirely and call `<RenderDispatch>` with
 * `BlockLink` / `InlineLink` directly — the same wire shape, just
 * without the context indirection.
 */

import { useContext } from "react";
import type { ReactNode } from "react";
import { RefLinkImplContext, type RefLinkProps } from "./impl_context.js";

export type { RefLinkProps } from "./impl_context.js";

export function RefLink(props: RefLinkProps): ReactNode {
	const Impl = useContext(RefLinkImplContext);
	return <Impl {...props} />;
}
