/**
 * React contexts and hooks for the render manager.
 */

import { createContext, useCallback, useContext, useSyncExternalStore } from 'react';
import type { ViewRegistry, ViewId, View, SlotContent, SlotId, EntityId, GroupLayout } from './ViewRegistry';
import type { DataBundle } from '../data/ownership';
import { getData } from '../data/ownership';
import type { Entity } from '../data/schema';

// =============================================================================
// ViewRegistryContext
// =============================================================================

/**
 * Context for accessing the ViewRegistry.
 * The registry is provided at the app root and used by Child components
 * and click handlers.
 */
export const ViewRegistryContext = createContext<ViewRegistry | null>(null);

/**
 * Hook to access the ViewRegistry.
 * Must be used within a ViewRegistryContext.Provider.
 */
export function useViewRegistry(): ViewRegistry {
  const registry = useContext(ViewRegistryContext);
  if (!registry) {
    throw new Error(
      'useViewRegistry must be used within ViewRegistryContext.Provider'
    );
  }
  return registry;
}

// =============================================================================
// CurrentViewContext
// =============================================================================

/**
 * Value for CurrentViewContext.
 * - viewId: The current view ID (null in hover previews but preserved for source panel lookup)
 * - inHoverPreview: True when inside a hover preview popup (disables drag-to-move)
 */
export interface ViewContextValue {
  viewId: ViewId | null;
  inHoverPreview: boolean;
}

/**
 * Context providing view information to child components.
 * This allows Child components to know their parentViewId and whether they're in a hover preview.
 */
export const CurrentViewContext = createContext<ViewContextValue | null>(null);

/**
 * Hook to get the current view ID.
 * Must be used within a CurrentViewContext.Provider with a non-null viewId (i.e., within a view).
 */
export function useCurrentViewId(): ViewId {
  const ctx = useContext(CurrentViewContext);
  if (!ctx?.viewId) {
    throw new Error('useCurrentViewId must be used within a view');
  }
  return ctx.viewId;
}

/**
 * Hook to get the current view ID if available.
 * Returns null if not within a view context or if viewId is null (e.g., in some preview cards).
 */
export function useMaybeCurrentViewId(): ViewId | null {
  const ctx = useContext(CurrentViewContext);
  return ctx?.viewId ?? null;
}

/**
 * Hook to get the full view context value.
 * Use this when you need to check inHoverPreview or access the preserved viewId.
 */
export function useViewContext(): ViewContextValue | null {
  return useContext(CurrentViewContext);
}

// =============================================================================
// useViews hook
// =============================================================================

/**
 * Hook to subscribe to the array of views.
 * Uses cached snapshot to avoid infinite re-render loops.
 */
export function useViews(registry: ViewRegistry): View[] {
  return useSyncExternalStore(
    registry.subscribeViews,
    registry.getViewsSnapshot
  );
}

// =============================================================================
// useGroupLayout hook
// =============================================================================

/**
 * Hook to subscribe to the group/panel layout for the sidebar.
 * Returns an array of groups with their panels.
 */
export function useGroupLayout(registry: ViewRegistry): GroupLayout[] {
  return useSyncExternalStore(
    registry.subscribe,
    registry.getGroupLayout
  );
}

// =============================================================================
// useSlotContent hook
// =============================================================================

/**
 * Hook to subscribe to a slot's content.
 * Combines slot registration with subscription.
 * Uses cached snapshot to avoid infinite re-render loops.
 */
export function useSlotContent(
  registry: ViewRegistry,
  slotId: SlotId,
  entityId: EntityId,
  parentViewId: ViewId
): SlotContent {
  const subscribeToSlot = useCallback(
    (listener: () => void) =>
      registry.subscribeSlot(slotId, entityId, parentViewId, listener),
    [registry, slotId, entityId, parentViewId]
  );

  return useSyncExternalStore(
    subscribeToSlot,
    () => registry.getSlotContentSnapshot(slotId)
  );
}

// =============================================================================
// useIsHighlighted hook
// =============================================================================

/**
 * Hook to check if a view is currently highlighted.
 * Used for applying highlight CSS.
 */
export function useIsHighlighted(viewId: ViewId): boolean {
  const registry = useViewRegistry();
  return useSyncExternalStore(
    registry.subscribeHighlight,
    () => registry.getHighlightedViewId() === viewId
  );
}

// =============================================================================
// DataBundleContext
// =============================================================================

/**
 * Context for accessing the DataBundle.
 * Provided by RenderRoot and used by panels to access entity data.
 */
export const DataBundleContext = createContext<DataBundle | null>(null);

/**
 * Hook to access the DataBundle.
 * Must be used within a DataBundleContext.Provider.
 */
export function useDataBundle(): DataBundle {
  const data = useContext(DataBundleContext);
  if (!data) {
    throw new Error(
      'useDataBundle must be used within DataBundleContext.Provider'
    );
  }
  return data;
}

// =============================================================================
// useEntityData hook
// =============================================================================

/**
 * Hook to get entity data by ID.
 * Uses the DataBundle context internally.
 */
export function useEntityData(entityId: EntityId): Entity | undefined {
  const data = useDataBundle();
  return getData(data, entityId);
}

// =============================================================================
// HoverKeptOpenContext - for nested hover preview propagation
// =============================================================================

/**
 * Context for propagating hover state to parent preview cards.
 * When a child preview card opens, it calls setKeptOpen(true) to keep
 * the parent preview card open. When it closes, it calls setKeptOpen(false).
 */
export const HoverKeptOpenContext = createContext<((open: boolean) => void) | undefined>(undefined);

/**
 * Hook to get the setKeptOpen callback from the parent preview card.
 * Returns undefined if not within a preview card context.
 */
export function useSetKeptOpen(): ((open: boolean) => void) | undefined {
  return useContext(HoverKeptOpenContext);
}
