/**
 * Public surface of `@fxtr-ui/workstation-dispatch`.
 *
 * The renderer-author-facing dispatch surface for the workstation:
 * canonical `DispatchKey` objects + Props types + fallback
 * components, the per-key registry builder, the `RefLink` /
 * `JsonRefTree` / metadata-view primitives, and the
 * `RefLinkImplContext` that lets the surrounding shell (or any
 * other consumer) install richer interaction behaviour around
 * dispatched links.
 *
 * Externalised by the dynamic-dispatch system (see
 * ``DEFAULT_EXTERNALS`` in
 * ``fxtr_workstation/dynamic_dispatch/basic_dynamic_dispatch/compile.py``),
 * so runtime-loaded renderer modules and the host workstation
 * share a single identity per UUID — one ``FullPanel`` object,
 * one fallback set, one ``RefLink`` component.
 *
 * Depends only on ``react`` + ``@fxtr-ui/core`` +
 * ``@fxtr-ui/react`` + ``@fxtr-ui/react-dispatch`` +
 * ``@fxtr-ui/state-util`` + ``jotai`` — all already in the
 * externals list. No ``@base-ui/react``, no ``dockview``, no shell
 * internals.
 */

// ----- Dispatch keys + Props types -----
export {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
} from "./dispatch/keys.js";

// ----- Dispatch registry builder -----
export {
	createWorkstationDispatchRegistry,
	type WorkstationDispatchRegistryOptions,
	type WorkstationDispatchSide,
} from "./dispatch/registry.js";

// ----- Default fallback components -----
export { DispatchErrorFallback } from "./fallbacks/DispatchErrorFallback.js";
export { EntityBlockLinkFallback } from "./fallbacks/EntityBlockLinkFallback.js";
export { EntityFullPanelBuiltinFallback } from "./fallbacks/EntityFullPanelBuiltinFallback.js";
export { EntityFullPanelFallback } from "./fallbacks/EntityFullPanelFallback.js";
export { EntityFullPanelLoadingFallback } from "./fallbacks/EntityFullPanelLoadingFallback.js";
export { EntityHoverPreviewFallback } from "./fallbacks/EntityHoverPreviewFallback.js";
export { EntityHoverPreviewLoadingFallback } from "./fallbacks/EntityHoverPreviewLoadingFallback.js";
export { EntityInlineLinkFallback } from "./fallbacks/EntityInlineLinkFallback.js";
export { ServiceBlockLinkFallback } from "./fallbacks/ServiceBlockLinkFallback.js";
export { ServiceFullPanelFallback } from "./fallbacks/ServiceFullPanelFallback.js";
export { ServiceFullPanelLoadingFallback } from "./fallbacks/ServiceFullPanelLoadingFallback.js";
export { ServiceHoverPreviewFallback } from "./fallbacks/ServiceHoverPreviewFallback.js";
export { ServiceHoverPreviewLoadingFallback } from "./fallbacks/ServiceHoverPreviewLoadingFallback.js";
export { ServiceInlineLinkFallback } from "./fallbacks/ServiceInlineLinkFallback.js";

// ----- RefLink + impl context -----
export { RefLink, type RefLinkProps } from "./reflink/RefLink.js";
export {
	DefaultRefLinkImpl,
	type RefLinkImpl,
	RefLinkImplContext,
} from "./reflink/impl_context.js";

// ----- Ref-stub resolver (shell-installed; null outside a shell) -----
export {
	type RefStubResolver,
	RefStubResolverContext,
	useRefStubResolver,
} from "./reflink/ref_stub_resolver.js";

// ----- Drag-and-drop primitives -----
export {
	DRAG_MIME_FXTR_REF,
	parseRefDragData,
	type RefDragData,
	RefDropRegionContext,
	type RefDropRegionContextValue,
	type RefDropRegionProps,
	RefDropRegionProvider,
	useRefDropRegion,
	type UseRefDropRegionResult,
} from "./dnd/index.js";

// ----- Navigation context -----
export {
	type UseWorkstationNavigate,
	useWorkstationNavigate,
	WorkstationNavigateContext,
	type WorkstationNavigateFn,
} from "./navigate_context.js";

// ----- Metadata views -----
export {
	EntityMetadataView,
	type EntityMetadataViewProps,
	JsonRefTree,
	type JsonRefTreeProps,
	type RefLeafRenderer,
	ServiceMetadataBody,
	ServiceMetadataView,
	type ServiceMetadataViewProps,
} from "./metadata/index.js";
