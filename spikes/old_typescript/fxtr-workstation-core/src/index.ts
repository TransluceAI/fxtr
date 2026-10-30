// @fxtr-workstation/core — workstation panel viewer over fxtr entities.

export {
	BlockLink,
	FullPanel,
	HoverPreview,
	InlineLink,
	type FullPanelProps,
	type HoverPreviewProps,
	type LinkProps,
	type SetPanelTitle,
} from "./keys.js";

export { shortId } from "./shortId.js";

export { JsonTree, type JsonTreeProps } from "./components/JsonTree.js";

export { BlockLinkFallback } from "./fallbacks/BlockLinkFallback.js";
export { FullPanelFallback } from "./fallbacks/FullPanelFallback.js";
export { HoverPreviewFallback } from "./fallbacks/HoverPreviewFallback.js";
export { InlineLinkFallback } from "./fallbacks/InlineLinkFallback.js";

export { createWorkstationDispatchHandler } from "./registry.js";

export { DockviewLayout, PANEL_COMPONENT_ENTITY } from "./DockviewLayout.js";
export { EntityPanelWrapper, type EntityPanelParams } from "./EntityPanelWrapper.js";
export { WorkstationShell, type WorkstationShellProps } from "./WorkstationShell.js";

// EntityLink — public interactive ref component (composition of
// dispatch + interaction hooks).
export { EntityLink, type EntityLinkProps } from "./EntityLink.js";

// Breadcrumb + navigation
export {
	type Breadcrumb,
	type BreadcrumbVia,
	type BreadcrumbSource,
	type HistoryRoot,
	buildTrail,
} from "./breadcrumb/types.js";
export {
	BreadcrumbSourceProvider,
	useBreadcrumbSource,
} from "./breadcrumb/BreadcrumbSourceContext.js";
export {
	type FlatHistoryEntry,
	computeNextChainState,
	computeOpenNewFromChainEntry,
	computeOpenNewFromHistoryEntry,
	computeOpenNewFromLink,
	computeSnapshotFromTrail,
} from "./breadcrumb/history.js";
export { flattenHistoryRoot } from "./breadcrumb/flatten.js";
export { BreadcrumbBar, type BreadcrumbBarProps } from "./breadcrumb/BreadcrumbBar.js";
export { BreadcrumbEntry, type BreadcrumbEntryProps } from "./breadcrumb/BreadcrumbEntry.js";
export {
	HistoryRootMenu,
	type HistoryRootMenuProps,
} from "./breadcrumb/HistoryRootMenu.js";
export {
	HistoryRootEntry,
	type HistoryRootEntryProps,
} from "./breadcrumb/HistoryRootEntry.js";
export type {
	DropPosition,
	FollowLinkEvent,
	FollowLinkIntent,
	WorkstationApi,
} from "./workstation/navigate.js";
export {
	WorkstationProvider,
	useWorkstation,
} from "./workstation/WorkstationContext.js";
export {
	PanelContextProvider,
	usePanelContext,
	type PanelContextValue,
	type PanelKind,
} from "./workstation/PanelContext.js";

// Interactions — the individual hooks are exported for composing
// custom layouts; `EntityLink` covers the common case.
export {
	DRAG_MIME_FXTR_ENTITY,
	hasDragData,
	parseDragData,
	setDragData,
	type DragPayload,
	type LinkDragPayload,
	type SnapshotDragPayload,
} from "./interactions/drag.js";
export {
	HoverCloseContext,
	HoverKeptOpenContext,
	useCloseAllHovers,
	useHoverClose,
	useSetKeptOpen,
	type HoverCloseContextValue,
} from "./interactions/HoverContext.js";
export { useDraggableEntity } from "./interactions/useDraggableEntity.js";
export { useDraggableChainEntry } from "./interactions/useDraggableChainEntry.js";
export { useDraggableHistoryEntry } from "./interactions/useDraggableHistoryEntry.js";
export { useEntityClick } from "./interactions/useEntityClick.js";
export { useEntityHoverCard } from "./interactions/useEntityHoverCard.js";
export { useEntityContextMenu } from "./interactions/useEntityContextMenu.js";
export {
	DraggableEntityHandle,
	type DraggableEntityHandleProps,
} from "./interactions/DraggableEntityHandle.js";

// Namespace panel
export {
	NamespacePanel,
	type NamespacePanelParams,
} from "./panels/NamespacePanel.js";
export {
	NAMESPACE_PANEL_ID,
	PANEL_COMPONENT_NAMESPACE,
} from "./panels/namespacePanelComponent.js";

// Open-panels sidebar
export {
	OpenPanelsSidebar,
	type OpenPanelsSidebarProps,
} from "./sidebar/OpenPanelsSidebar.js";
export {
	SidebarTooltip,
	SidebarTooltipProvider,
	type SidebarTooltipProps,
} from "./sidebar/SidebarTooltip.js";
export {
	usePanelLayout,
	type SidebarGroup,
	type SidebarPanel,
} from "./sidebar/usePanelLayout.js";
