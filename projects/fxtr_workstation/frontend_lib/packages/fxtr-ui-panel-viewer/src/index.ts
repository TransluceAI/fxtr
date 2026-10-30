/**
 * Public surface of `@fxtr-ui/panel-viewer`.
 *
 * The generic, connector-agnostic dockview panel viewer: per-panel
 * session lifecycle, chain/breadcrumb/history navigation, ref-link
 * routing + interaction impls, dispatch rendering, the sidebar, layout
 * persistence, and the injected-panel-registration mechanism. Types
 * against the abstract `Connector` / `Session` from `@fxtr-ui/core`;
 * the concrete connector flows in at runtime via `<ConnectorProvider>`.
 *
 * App-specific panels (roots / debug / custom) are NOT here — they are
 * injected as `PanelRegistration`s (see `@fxtr-ui/workstation` for the
 * workstation set).
 */

// ----- Shared primitives -----
export type { PanelViewDisplay, PanelViewDragData, PanelViewEntry, StubKind } from "./types.js";
export { dragDataOfEntry, entryKind, entryOfDragData, panelViewEntryOf } from "./types.js";

// ----- Dispatch keys + Props types -----
// Canonical identities live in @fxtr-ui/workstation-dispatch (which
// the dynamic-dispatch system externalises so runtime-loaded
// renderer modules import the same key objects + fallback set as
// the host shell — single identity per UUID). Re-exported here for
// convenience.
export {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
} from "@fxtr-ui/workstation-dispatch";

// ----- Dispatch registry builder -----
export {
	createWorkstationDispatchRegistry,
	type WorkstationDispatchRegistryOptions,
	type WorkstationDispatchSide,
} from "@fxtr-ui/workstation-dispatch";

// ----- Default fallback components -----
// Exported so consumers who build their own registry (skipping
// `createWorkstationDispatchRegistry`) can still reach the shipped
// defaults for some slots.
export {
	DispatchErrorFallback,
	EntityBlockLinkFallback,
	EntityFullPanelFallback,
	EntityFullPanelLoadingFallback,
	EntityHoverPreviewFallback,
	EntityHoverPreviewLoadingFallback,
	EntityInlineLinkFallback,
	ServiceBlockLinkFallback,
	ServiceFullPanelFallback,
	ServiceFullPanelLoadingFallback,
	ServiceHoverPreviewFallback,
	ServiceHoverPreviewLoadingFallback,
	ServiceInlineLinkFallback,
} from "@fxtr-ui/workstation-dispatch";

// ----- Error boundary -----
export {
	PanelErrorBoundary,
	type PanelErrorBoundaryProps,
} from "./fallbacks/PanelErrorBoundary.js";

// ----- Navigation surface -----
export type {
	DropPosition,
	FollowLinkEvent,
	FollowLinkIntent,
	PanelViewerApi,
} from "./shell/PanelViewerApi.js";
export {
	PanelViewerProvider,
	type PanelViewerProviderProps,
	usePanelViewer,
} from "./shell/PanelViewerContext.js";
export {
	ConnectorProvider,
	type ConnectorProviderProps,
	useConnector,
} from "./shell/ConnectorContext.js";
export {
	PanelSessionRegistryProvider,
	type PanelSessionRegistry,
	type PanelSessionRegistryProviderProps,
	usePanelSessionRegistry,
} from "./shell/PanelSessionRegistry.js";
export { DockviewLayout, type DockviewLayoutProps } from "./shell/DockviewLayout.js";
export type { PanelRegistration } from "./shell/PanelRegistration.js";
export {
	PanelViewer,
	type PanelViewerProps,
} from "./shell/PanelViewer.js";
export { ShellHeader, type ShellHeaderProps } from "./shell/ShellHeader.js";

// ----- Dispatcher session id persistence -----
// Helpers for the consumer's dispatcher-session-acquire effect.
// The shell no longer owns dispatcher session lifecycle, but
// re-exports these so consumers don't have to roll their own
// localStorage scheme and so the shell's `DispatcherUnavailable`
// "Clear local state and reload" path can be wired up by the
// consumer to clear the same key.
export {
	DISPATCHER_SESSION_ID_KEY,
	clearDispatcherSessionId,
	readDispatcherSessionId,
	writeDispatcherSessionId,
} from "./persistence/dispatcher_session_id.js";

// ----- Layout persistence ------------------------------------------------
export {
	clearPersistedLayout,
	LAYOUT_STORAGE_KEY,
	LAYOUT_STORAGE_VERSION,
	loadPersistedLayout,
	type PersistedLayoutV6,
	type PersistedSidebarState,
	savePersistedLayout,
} from "./persistence/layout_v6.js";
export {
	useLayoutPersistence,
	type UseLayoutPersistenceOptions,
} from "./persistence/useLayoutPersistence.js";

// ----- Open-panels sidebar -----------------------------------------------
export {
	OpenPanelsSidebar,
	type OpenPanelsSidebarProps,
	type SidebarButton,
} from "./sidebar/OpenPanelsSidebar.js";
export {
	ResizableSidebar,
	type ResizableSidebarProps,
} from "./sidebar/ResizableSidebar.js";
export {
	SidebarTooltip,
	type SidebarTooltipProps,
	SidebarTooltipProvider,
} from "./sidebar/SidebarTooltip.js";
export {
	type SidebarGroup,
	type SidebarPanel,
	usePanelLayout,
} from "./sidebar/usePanelLayout.js";

// ----- Panel session lifecycle + wrapper -----
export {
	REF_PANEL_COMPONENT,
	type RefPanelParams,
} from "./panel/RefPanelParams.js";
export {
	type PanelSessionState,
	type PanelSessionStatus,
	type UsePanelSessionOptions,
	usePanelSession,
} from "./panel/PanelSessionLifecycle.js";
export { SessionUnavailable, type SessionUnavailableProps } from "./panel/SessionUnavailable.js";
export { PanelWrapper } from "./panel/PanelWrapper.js";
export { RefPanelTab } from "./panel/RefPanelTab.js";

// ----- Panel context -----
export {
	PanelContextProvider,
	type PanelContextProviderProps,
	type PanelContextValue,
	type PanelKind,
	usePanelContext,
} from "./panel/PanelContext.js";

// ----- Breadcrumb types + pure transforms -----
export type {
	Breadcrumb,
	BreadcrumbDragData,
	BreadcrumbSource,
	BreadcrumbVia,
	HistoryRoot,
	RefPanelChainState,
} from "./breadcrumb/types.js";
export {
	breadcrumbOfDragData,
	buildTrail,
	dragDataOfBreadcrumb,
} from "./breadcrumb/types.js";
export {
	BreadcrumbSourceProvider,
	type BreadcrumbSourceProviderProps,
	useBreadcrumbSource,
} from "./breadcrumb/BreadcrumbSourceContext.js";
export {
	computeNextChainState,
	computeOpenNewFromChainEntry,
	computeOpenNewFromHistoryEntry,
	computeOpenNewFromLink,
	computeSnapshotFromTrail,
	type FlatHistoryEntry,
} from "./breadcrumb/history.js";
export { flattenHistoryRoot } from "./breadcrumb/flatten.js";
export { BreadcrumbBar, type BreadcrumbBarProps } from "./breadcrumb/BreadcrumbBar.js";
export { BreadcrumbEntry, type BreadcrumbEntryProps } from "./breadcrumb/BreadcrumbEntry.js";
export { HistoryRootMenu, type HistoryRootMenuProps } from "./breadcrumb/HistoryRootMenu.js";
export { HistoryRootEntry, type HistoryRootEntryProps } from "./breadcrumb/HistoryRootEntry.js";

// ----- Drag-and-drop -----
export {
	DRAG_MIME_FXTR_REF,
	type DragPayload,
	dragDataOfHistoryRoot,
	hasDragData,
	type HistoryRootDragData,
	historyRootOfDragData,
	type LinkDragPayload,
	parseDragData,
	refPanelChainStateOfDragData,
	setDragData,
	type SnapshotChainDragData,
	type SnapshotDragPayload,
	snapshotChainDragDataOf,
} from "./reflink/drag.js";

// ----- Hover-card coordination contexts -----
export {
	HoverCloseContext,
	type HoverCloseContextValue,
	HoverKeptOpenContext,
	useCloseAllHovers,
	useHoverClose,
	useSetKeptOpen,
} from "./reflink/HoverContext.js";

// ----- Interaction hooks -----
export {
	DraggableRefHandle,
	type DraggableRefHandleProps,
} from "./reflink/DraggableRefHandle.js";
export { useDraggableChainEntry } from "./reflink/useDraggableChainEntry.js";
export { useDraggableHistoryEntry } from "./reflink/useDraggableHistoryEntry.js";
export { useDraggableRef } from "./reflink/useDraggableRef.js";
export { useRefClick } from "./reflink/useRefClick.js";
export { useRefContextMenu } from "./reflink/useRefContextMenu.js";
export { useRefHoverCard } from "./reflink/useRefHoverCard.js";

// ----- RefLink — re-export from @fxtr-ui/workstation-dispatch -----
// The canonical component (one identity per import-map externalization)
// lives in the dispatch package. The viewer installs its own
// `RefLinkImpl` via `ShellRefLinkProvider` (mounted by `<PanelViewer>`)
// so the same component picks up drag/click/hover/context-menu behaviour
// inside the viewer.
export { RefLink, type RefLinkProps } from "@fxtr-ui/workstation-dispatch";

// ----- Shell-side RefLink impl + provider -----
export { ShellRefLinkImpl, ShellRefLinkProvider } from "./reflink/ShellRefLinkImpl.js";

// ----- DebugRefChip — short-id chip for debug surfaces -----
export { DebugRefChip, type DebugRefChipProps } from "./reflink/DebugRefChip.js";

// ----- Metadata view — re-exported from @fxtr-ui/workstation-dispatch -----
export {
	EntityMetadataView,
	type EntityMetadataViewProps,
	JsonRefTree,
	type JsonRefTreeProps,
	type RefLeafRenderer,
	ServiceMetadataBody,
	ServiceMetadataView,
	type ServiceMetadataViewProps,
} from "@fxtr-ui/workstation-dispatch";

// ----- System-type renderers ----------------------------------
// Default renderers for fxtr system-defined entity types
// (currently just `FxtrBasicType` — the type label every user
// type carries as its `$fxtr.type`). Consumers register them
// at the relevant id_strings to display these system types
// distinctly from regular data.
export {
	BASIC_TYPE_ID_STRING,
	BasicTypeBlockLink,
	BasicTypeInlineLink,
} from "./system_renderers/index.js";
