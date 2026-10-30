/**
 * ViewRegistry - External store managing views, slots, and panels.
 *
 * Uses the external store pattern for React integration via useSyncExternalStore.
 * Implements granular caching and notification to avoid infinite re-render loops.
 */

import { createHtmlPortalNode, HtmlPortalNode } from 'react-reverse-portal';
import type { DockviewApi, DockviewGroupPanel } from 'dockview';
import { positionToDirection } from 'dockview';

// =============================================================================
// Types
// =============================================================================

export type ViewId = string;
export type SlotId = string;
export type PanelId = string;
export type EntityId = string;

/** Direction for placing new panels relative to a reference panel */
export type PanelDirection = 'left' | 'right' | 'above' | 'below' | 'within';

/**
 * Unified position type for panel creation/movement.
 * Supports both context menu (referencePanel) and DND (referenceGroup) sources.
 */
export type PanelPosition = {
  referencePanel?: string;              // Panel ID (from context menu)
  referenceGroup?: DockviewGroupPanel;  // Group (from DND drop)
  direction?: 'left' | 'right' | 'above' | 'below'; // undefined = within/center
};

// =============================================================================
// Drag and Drop Types
// =============================================================================

/** MIME types for drag data - separate types so we can check during dragover */
export const DRAG_MIME_VIEW = 'application/x-fxtr-view';
export const DRAG_MIME_REF = 'application/x-fxtr-ref';
/** Prefix for entity-specific MIME type (allows placeholders to check compatibility) */
export const DRAG_MIME_ENTITY_PREFIX = 'application/x-fxtr-entity-';

/** Drag payload - identifies what's being dragged */
export type DragData =
  | { type: 'ref'; entityId: EntityId }                      // From RefLink
  | { type: 'view'; viewId: ViewId; entityId: EntityId };    // From DraggableID

/** Serialize drag data for dataTransfer */
export function serializeDragData(data: DragData, dataTransfer: DataTransfer): void {
  const mime = data.type === 'ref' ? DRAG_MIME_REF : DRAG_MIME_VIEW;
  dataTransfer.setData(mime, JSON.stringify(data));
  // For view drags, also set entity-specific MIME so placeholders can check compatibility
  if (data.type === 'view') {
    dataTransfer.setData(DRAG_MIME_ENTITY_PREFIX + data.entityId, '');
  }
}

/** Check what type of drag this is (works during dragover) */
export function getDragType(dataTransfer: DataTransfer | null): 'view' | 'ref' | null {
  if (!dataTransfer) return null;
  // Note: dataTransfer.types is a DOMStringList which uses contains(), not includes()
  const types = Array.from(dataTransfer.types);
  if (types.includes(DRAG_MIME_VIEW)) return 'view';
  if (types.includes(DRAG_MIME_REF)) return 'ref';
  return null;
}

/** Check if the drag is a view for a specific entity (works during dragover) */
export function isDragForEntity(dataTransfer: DataTransfer | null, entityId: EntityId): boolean {
  if (!dataTransfer) return false;
  const types = Array.from(dataTransfer.types);
  return types.includes(DRAG_MIME_ENTITY_PREFIX + entityId);
}

/** Parse drag data from dataTransfer (only works on drop, not dragover) */
export function parseDragData(dataTransfer: DataTransfer | null): DragData | null {
  if (!dataTransfer) return null;

  // Try view first, then ref
  let json = dataTransfer.getData(DRAG_MIME_VIEW);
  if (!json) {
    json = dataTransfer.getData(DRAG_MIME_REF);
  }
  if (!json) return null;

  try {
    return JSON.parse(json) as DragData;
  } catch {
    return null;
  }
}

export interface View {
  viewId: ViewId;
  entityId: EntityId;
  portalNode: HtmlPortalNode;
  ownerSlotId: SlotId | null; // non-null if owned by inline slot
  panelId: PanelId | null; // non-null if owned by panel
}

export interface Slot {
  slotId: SlotId;
  entityId: EntityId;
  parentViewId: ViewId; // the view that contains this slot
}

export interface Panel {
  panelId: PanelId;
  viewId: ViewId;
  isPreview: boolean;
}

// --- Group sidebar types ---

export interface PanelLayout {
  panelId: PanelId;
  title: string;
  isActive: boolean;
  isPreview: boolean;
  entityId?: EntityId;    // For entity panels only
  entityType?: string;    // For entity panels only
}

export interface GroupLayout {
  groupId: string;
  panels: PanelLayout[];
}

export interface ViewRegistryState {
  views: Map<ViewId, View>;
  slots: Map<SlotId, Slot>;
  panels: Map<PanelId, Panel>;
}

/** What getSlotContentSnapshot returns */
export type SlotContent =
  | { type: 'view'; viewId: ViewId; portalNode: HtmlPortalNode }
  | { type: 'placeholder'; viewId: ViewId } // viewId of existing view
  | { type: 'pending' };

// =============================================================================
// ViewRegistry Interface
// =============================================================================

export interface ViewRegistry {
  // --- Views subscription (for useViews hook) ---
  subscribeViews(listener: () => void): () => void;
  getViewsSnapshot(): View[];

  // --- Slot subscription (for useSlotContent hook) ---
  /**
   * Combined subscribe + register: registers slot, subscribes to updates,
   * and unregisters on cleanup. Use with useSyncExternalStore.
   */
  subscribeSlot(
    slotId: SlotId,
    entityId: EntityId,
    parentViewId: ViewId,
    listener: () => void
  ): () => void;
  getSlotContentSnapshot(slotId: SlotId): SlotContent;

  // --- State access (for DebugPanel) ---
  subscribe(listener: () => void): () => void;
  getSnapshot(): ViewRegistryState;

  // --- View access (called by EntityPanel) ---
  getView(viewId: ViewId): View | undefined;
  getViewsForEntity(entityId: EntityId): View[];

  // --- Entity operations (called by click handlers) ---
  openEntity(entityId: EntityId): void;

  // --- Dockview integration ---
  setDockviewApi(api: DockviewApi): void;
  handlePanelClose(panelId: PanelId): void;

  // --- Highlight (Phase 2a) ---
  subscribeHighlight(listener: () => void): () => void;
  getHighlightedViewId(): ViewId | null;
  highlightView(viewId: ViewId, durationMs?: number): void;

  // --- Navigation (Phase 2a) ---
  findViewPanel(viewId: ViewId): PanelId | null;
  jumpToView(viewId: ViewId): void;
  jumpToEntity(entityId: EntityId): boolean;  // Returns true if a view was found

  // --- View manipulation (Phase 2a) ---
  moveViewToSlot(viewId: ViewId, targetSlotId: SlotId): void;
  moveViewToNewPanel(viewId: ViewId, position?: PanelPosition): void;
  replacePlaceholderWithNewView(slotId: SlotId): void;
  createNewViewInPanel(entityId: EntityId, position?: PanelPosition): void;
  /** Duplicate a view including its owned children (recursive) */
  duplicateViewInPanel(sourceViewId: ViewId, position?: PanelPosition): void;
  /** Duplicate a view into a slot (for alt+drop on placeholder) */
  duplicateViewInSlot(sourceViewId: ViewId, targetSlotId: SlotId): void;
  /** Close a slot-owned subview, converting it to a placeholder. Returns false if can't close. */
  closeSubview(viewId: ViewId): boolean;
  /** Handle drop of a ref drag - moves existing view or creates new */
  handleRefDrop(entityId: EntityId, position?: PanelPosition, isAltHeld?: boolean): void;

  // --- Panel info ---
  getActivePanelId(): PanelId | null;

  // --- DND helpers (Phase 2b) ---
  /** Convert PanelDirection (context menu) to PanelPosition */
  getPositionFromDirection(direction?: PanelDirection): PanelPosition | undefined;
  /** Set whether a placeholder is actively being dragged over */
  setPlaceholderDragActive(active: boolean): void;
  /** Check if a placeholder is actively being dragged over */
  isPlaceholderDragActive(): boolean;

  // --- Hover management ---
  /** Register a callback to close a hover preview. Returns unsubscribe function. */
  registerHoverCloseListener(listener: () => void): () => void;
  /** Close all open hover previews (called when panels open) */
  closeAllHovers(): void;

  // --- Preview panels (Phase 2d) ---
  /** Open entity in a preview panel in the specified direction from source panel */
  openInPreviewOrJumpTo(
    entityId: EntityId,
    direction: 'left' | 'right',
    sourcePanelId: PanelId | null,
    options?: { upgradePreview?: boolean }
  ): void;
  /** Upgrade a preview panel to a regular panel */
  upgradePreviewToRegular(panelId: PanelId): void;
  /** Check if a panel is a preview panel */
  isPanelPreview(panelId: PanelId): boolean;

  // --- Group sidebar (Phase 2e) ---
  /** Get current group/panel layout for sidebar display */
  getGroupLayout(): GroupLayout[];
}

// =============================================================================
// Implementation
// =============================================================================

function generateViewId(): ViewId {
  return `view-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export function createViewRegistry(): ViewRegistry {
  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------

  let state: ViewRegistryState = {
    views: new Map(),
    slots: new Map(),
    panels: new Map(),
  };
  let dockviewApi: DockviewApi | null = null;

  // ---------------------------------------------------------------------------
  // Caches (invalidated on state change, repopulated on read)
  // ---------------------------------------------------------------------------

  let viewsArrayCache: View[] | null = null;
  let groupLayoutCache: GroupLayout[] | null = null;
  const slotContentCache = new Map<SlotId, SlotContent>();

  // ---------------------------------------------------------------------------
  // Listeners (granular subscription)
  // ---------------------------------------------------------------------------

  const viewListeners = new Set<() => void>(); // for useViews
  const slotListeners = new Map<SlotId, () => void>(); // one per slot
  const globalListeners = new Set<() => void>(); // for DebugPanel
  const highlightListeners = new Set<() => void>(); // for useIsHighlighted

  // ---------------------------------------------------------------------------
  // Highlight state (Phase 2a)
  // ---------------------------------------------------------------------------

  let highlightedViewId: ViewId | null = null;
  let highlightTimeoutId: ReturnType<typeof setTimeout> | null = null;

  // ---------------------------------------------------------------------------
  // Duplication state (Phase 2a)
  // ---------------------------------------------------------------------------

  // Map: new parent view ID -> (child entity ID -> old child view ID to duplicate from)
  const duplicatingViews = new Map<ViewId, Map<EntityId, ViewId>>();

  // ---------------------------------------------------------------------------
  // DND coordination state (Phase 2b)
  // ---------------------------------------------------------------------------

  // True when a placeholder is being dragged over (to prevent dockview from accepting)
  let placeholderDragActive = false;

  // ---------------------------------------------------------------------------
  // Hover management state
  // ---------------------------------------------------------------------------

  // Callbacks registered by hover previews to close themselves
  const hoverCloseListeners = new Set<() => void>();

  function closeAllHovers() {
    hoverCloseListeners.forEach((listener) => listener());
  }

  // ---------------------------------------------------------------------------
  // Notification functions
  // ---------------------------------------------------------------------------

  function notifyViews() {
    viewsArrayCache = null;
    groupLayoutCache = null;
    viewListeners.forEach((l) => l());
    globalListeners.forEach((l) => l());
  }

  function notifySlot(slotId: SlotId) {
    slotContentCache.delete(slotId);
    slotListeners.get(slotId)?.();
  }

  function notifySlotsForEntity(entityId: EntityId) {
    for (const [slotId, slot] of state.slots) {
      if (slot.entityId === entityId) {
        notifySlot(slotId);
      }
    }
    groupLayoutCache = null;
    globalListeners.forEach((l) => l());
  }

  function notifyHighlight() {
    highlightListeners.forEach((l) => l());
  }

  // ---------------------------------------------------------------------------
  // Snapshot functions (cached)
  // ---------------------------------------------------------------------------

  function getViewsSnapshot(): View[] {
    if (viewsArrayCache === null) {
      viewsArrayCache = Array.from(state.views.values());
    }
    return viewsArrayCache;
  }

  function computeSlotContent(slotId: SlotId): SlotContent {
    const slot = state.slots.get(slotId);

    if (!slot) {
      // Slot not registered yet (first render before subscription)
      return { type: 'pending' };
    }

    // Check if this slot owns a view
    const ownedView = Array.from(state.views.values()).find(
      (v) => v.ownerSlotId === slotId
    );

    if (ownedView) {
      return { type: 'view', viewId: ownedView.viewId, portalNode: ownedView.portalNode };
    }

    // This slot is a placeholder - find the view that exists
    const existingView = Array.from(state.views.values()).find(
      (v) => v.entityId === slot.entityId
    );

    if (existingView) {
      return { type: 'placeholder', viewId: existingView.viewId };
    }

    // Shouldn't happen, but handle gracefully
    return { type: 'pending' };
  }

  function getSlotContentSnapshot(slotId: SlotId): SlotContent {
    if (!slotContentCache.has(slotId)) {
      slotContentCache.set(slotId, computeSlotContent(slotId));
    }
    return slotContentCache.get(slotId)!;
  }

  // ---------------------------------------------------------------------------
  // Subscribe functions
  // ---------------------------------------------------------------------------

  function subscribeViews(listener: () => void): () => void {
    viewListeners.add(listener);
    return () => viewListeners.delete(listener);
  }

  function subscribe(listener: () => void): () => void {
    globalListeners.add(listener);
    return () => globalListeners.delete(listener);
  }

  function getSnapshot(): ViewRegistryState {
    return state;
  }

  // ---------------------------------------------------------------------------
  // Helper functions
  // ---------------------------------------------------------------------------

  function getViewsForEntity(entityId: EntityId): View[] {
    return Array.from(state.views.values()).filter(
      (v) => v.entityId === entityId
    );
  }

  // ---------------------------------------------------------------------------
  // Internal: setupDuplicationContext
  // ---------------------------------------------------------------------------

  /**
   * Records which child views should be duplicated when a new parent view renders.
   * Called when duplicating a view to set up recursive child duplication.
   */
  function setupDuplicationContext(oldViewId: ViewId, newViewId: ViewId) {
    const childMap = new Map<EntityId, ViewId>();

    // Find all slots owned by the old view (slots where parentViewId === oldViewId)
    for (const slot of state.slots.values()) {
      if (slot.parentViewId === oldViewId) {
        // Find if this slot owns a view
        const ownedView = Array.from(state.views.values()).find(
          (v) => v.ownerSlotId === slot.slotId
        );
        if (ownedView) {
          childMap.set(slot.entityId, ownedView.viewId);
        }
      }
    }

    if (childMap.size > 0) {
      duplicatingViews.set(newViewId, childMap);
    }
  }

  // ---------------------------------------------------------------------------
  // Internal: registerSlotInternal
  // ---------------------------------------------------------------------------

  function registerSlotInternal(
    slotId: SlotId,
    entityId: EntityId,
    parentViewId: ViewId
  ): { viewCreated: boolean } {
    // Create the slot
    const slot: Slot = { slotId, entityId, parentViewId };

    // Check for duplication context (are we duplicating from an existing view?)
    const dupContext = duplicatingViews.get(parentViewId);
    if (dupContext?.has(entityId)) {
      const oldChildViewId = dupContext.get(entityId)!;

      // Remove from pending (cleanup)
      dupContext.delete(entityId);
      if (dupContext.size === 0) {
        duplicatingViews.delete(parentViewId);
      }

      // Create new view (duplicate)
      const viewId = generateViewId();
      const portalNode = createHtmlPortalNode();
      const view: View = {
        viewId,
        entityId,
        portalNode,
        ownerSlotId: slotId,
        panelId: null,
      };

      // Recursively set up duplication for grandchildren
      setupDuplicationContext(oldChildViewId, viewId);

      state = {
        ...state,
        views: new Map(state.views).set(viewId, view),
        slots: new Map(state.slots).set(slotId, slot),
      };
      return { viewCreated: true };
    }

    // Check if this is the first slot for this entity
    const existingViews = getViewsForEntity(entityId);

    if (existingViews.length === 0) {
      // First slot: create a view and bind it to this slot
      const viewId = generateViewId();
      const portalNode = createHtmlPortalNode();
      const view: View = {
        viewId,
        entityId,
        portalNode,
        ownerSlotId: slotId,
        panelId: null,
      };
      state = {
        ...state,
        views: new Map(state.views).set(viewId, view),
        slots: new Map(state.slots).set(slotId, slot),
      };
      return { viewCreated: true };
    } else {
      // Not first: this slot is a placeholder
      state = {
        ...state,
        slots: new Map(state.slots).set(slotId, slot),
      };
      return { viewCreated: false };
    }
  }

  // ---------------------------------------------------------------------------
  // Internal: unregisterSlotInternal
  // ---------------------------------------------------------------------------

  function unregisterSlotInternal(slotId: SlotId): {
    viewDestroyed: boolean;
    viewRebound: boolean;
    entityId: EntityId | null;
  } {
    const slot = state.slots.get(slotId);
    if (!slot) return { viewDestroyed: false, viewRebound: false, entityId: null };

    const entityId = slot.entityId;

    // Find if this slot owns a view
    const ownedView = Array.from(state.views.values()).find(
      (v) => v.ownerSlotId === slotId
    );

    // Remove the slot
    const newSlots = new Map(state.slots);
    newSlots.delete(slotId);

    if (ownedView) {
      // This slot owned a view - need to rebind or destroy
      const otherSlots = Array.from(newSlots.values()).filter(
        (s) => s.entityId === entityId
      );

      // Check if this is the last view for this entity
      const allViewsForEntity = getViewsForEntity(entityId);
      const isLastView = allViewsForEntity.length === 1;

      if (isLastView && otherSlots.length > 0) {
        // Last view and other slots want it - rebind to another slot
        const newOwner = otherSlots[0];
        const updatedView: View = {
          ...ownedView,
          ownerSlotId: newOwner.slotId,
        };
        state = {
          ...state,
          slots: newSlots,
          views: new Map(state.views).set(ownedView.viewId, updatedView),
        };
        return { viewDestroyed: false, viewRebound: true, entityId };
      } else {
        // Not the last view, or no other slots - destroy view
        const newViews = new Map(state.views);
        newViews.delete(ownedView.viewId);
        state = {
          ...state,
          slots: newSlots,
          views: newViews,
        };
        return { viewDestroyed: true, viewRebound: false, entityId };
      }
    } else {
      state = { ...state, slots: newSlots };
      return { viewDestroyed: false, viewRebound: false, entityId };
    }
  }

  // ---------------------------------------------------------------------------
  // subscribeSlot - Combined registration and subscription
  // ---------------------------------------------------------------------------

  function subscribeSlot(
    slotId: SlotId,
    entityId: EntityId,
    parentViewId: ViewId,
    listener: () => void
  ): () => void {
    // Register the slot
    const { viewCreated } = registerSlotInternal(slotId, entityId, parentViewId);

    // Subscribe this slot's listener
    slotListeners.set(slotId, listener);

    // Notify appropriately
    if (viewCreated) {
      notifyViews();
    }
    notifySlot(slotId);

    // Return cleanup function
    return () => {
      slotListeners.delete(slotId);
      slotContentCache.delete(slotId);
      const { viewDestroyed, viewRebound, entityId: eid } = unregisterSlotInternal(slotId);
      if (viewDestroyed || viewRebound) {
        notifyViews();
      }
      if (viewRebound && eid) {
        notifySlotsForEntity(eid);
      }
    };
  }

  // ---------------------------------------------------------------------------
  // getView
  // ---------------------------------------------------------------------------

  function getView(viewId: ViewId): View | undefined {
    return state.views.get(viewId);
  }

  // ---------------------------------------------------------------------------
  // openEntity
  // ---------------------------------------------------------------------------

  /**
   * Choose the best view to jump to from a list of views.
   * Prefers: active panel > any visible panel > any panel > first view
   */
  function chooseBestViewToJump(views: View[]): View | null {
    if (views.length === 0) return null;
    if (views.length === 1) return views[0];

    // Try to find a view in the active panel
    if (dockviewApi) {
      const activePanel = dockviewApi.activePanel;
      if (activePanel) {
        const viewInActivePanel = views.find((v) => v.panelId === activePanel.id);
        if (viewInActivePanel) return viewInActivePanel;

        // Check if view is nested inside the active panel
        for (const view of views) {
          if (view.ownerSlotId) {
            const panelId = findViewPanel(view.viewId);
            if (panelId === activePanel.id) return view;
          }
        }
      }

      // Try to find a view in any visible panel
      for (const view of views) {
        if (view.panelId) {
          const panel = dockviewApi.getPanel(view.panelId);
          if (panel?.api.isVisible) return view;
        }
      }

      // Check nested views in visible panels
      for (const view of views) {
        if (view.ownerSlotId) {
          const panelId = findViewPanel(view.viewId);
          if (panelId) {
            const panel = dockviewApi.getPanel(panelId);
            if (panel?.api.isVisible) return view;
          }
        }
      }
    }

    // Fall back to any panel-owned view, then first view
    const panelView = views.find((v) => v.panelId !== null);
    return panelView ?? views[0];
  }

  function openEntity(entityId: EntityId) {
    const existingViews = getViewsForEntity(entityId);

    if (existingViews.length > 0) {
      // View exists - jump to the best one
      const viewToJump = chooseBestViewToJump(existingViews);
      if (viewToJump) {
        jumpToView(viewToJump.viewId);
      }
      return;
    }

    // No view exists - create one in a new panel
    closeAllHovers();
    const viewId = generateViewId();
    const panelId = `panel-${viewId}`;
    const portalNode = createHtmlPortalNode();

    const view: View = {
      viewId,
      entityId,
      portalNode,
      ownerSlotId: null,
      panelId,
    };

    const panel: Panel = {
      panelId,
      viewId,
      isPreview: false,
    };

    state = {
      ...state,
      views: new Map(state.views).set(viewId, view),
      panels: new Map(state.panels).set(panelId, panel),
    };

    notifyViews();
    notifySlotsForEntity(entityId); // Existing placeholders may need to update

    // Open the panel in dockview
    if (dockviewApi) {
      dockviewApi.addPanel({
        id: panelId,
        component: 'entity',
        tabComponent: 'entity',
        title: entityId,
        params: { viewId, isPreview: false },
      });
    }
  }

  // ---------------------------------------------------------------------------
  // setDockviewApi
  // ---------------------------------------------------------------------------

  function setDockviewApi(api: DockviewApi) {
    dockviewApi = api;

    // Subscribe to dockview events to update sidebar
    const notifyLayout = () => {
      groupLayoutCache = null;
      globalListeners.forEach((l) => l());
    };
    api.onDidAddGroup(notifyLayout);
    api.onDidRemoveGroup(notifyLayout);
    api.onDidAddPanel(notifyLayout);
    api.onDidActivePanelChange(notifyLayout);
    // Note: onDidRemovePanel is handled by handlePanelClose which already notifies
  }

  // ---------------------------------------------------------------------------
  // handlePanelClose
  // ---------------------------------------------------------------------------

  function handlePanelClose(panelId: PanelId) {
    const panel = state.panels.get(panelId);
    if (!panel) return;

    const view = state.views.get(panel.viewId);
    if (!view) return;

    const entityId = view.entityId;

    // Remove the panel
    const newPanels = new Map(state.panels);
    newPanels.delete(panelId);

    // Check if this is the last view for this entity
    const allViewsForEntity = getViewsForEntity(entityId);
    const isLastView = allViewsForEntity.length === 1;

    // Check if there are slots that could adopt this view
    const eligibleSlots = Array.from(state.slots.values()).filter(
      (s) => s.entityId === entityId
    );

    if (isLastView && eligibleSlots.length > 0) {
      // Last view and slots want it - rebind to a slot
      const newOwner = eligibleSlots[0];
      const updatedView: View = {
        ...view,
        ownerSlotId: newOwner.slotId,
        panelId: null,
      };
      state = {
        ...state,
        panels: newPanels,
        views: new Map(state.views).set(view.viewId, updatedView),
      };
      notifyViews();
      notifySlotsForEntity(entityId);
    } else {
      // Not the last view, or no eligible slots - destroy view
      const newViews = new Map(state.views);
      newViews.delete(view.viewId);
      state = {
        ...state,
        panels: newPanels,
        views: newViews,
      };
      notifyViews();
      // If there are slots, they may need to update their placeholder status
      if (eligibleSlots.length > 0) {
        notifySlotsForEntity(entityId);
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Highlight functions (Phase 2a)
  // ---------------------------------------------------------------------------

  function subscribeHighlight(listener: () => void): () => void {
    highlightListeners.add(listener);
    return () => highlightListeners.delete(listener);
  }

  function getHighlightedViewId(): ViewId | null {
    return highlightedViewId;
  }

  function highlightView(viewId: ViewId, durationMs: number = 1500) {
    if (highlightTimeoutId) {
      clearTimeout(highlightTimeoutId);
    }
    highlightedViewId = viewId;
    notifyHighlight();

    highlightTimeoutId = setTimeout(() => {
      highlightedViewId = null;
      notifyHighlight();
      highlightTimeoutId = null;
    }, durationMs);
  }

  // ---------------------------------------------------------------------------
  // Navigation functions (Phase 2a)
  // ---------------------------------------------------------------------------

  function findViewPanel(viewId: ViewId): PanelId | null {
    const view = state.views.get(viewId);
    if (!view) return null;

    // Panel-owned view
    if (view.panelId) return view.panelId;

    // Slot-owned view - trace up through parent views
    if (view.ownerSlotId) {
      const slot = state.slots.get(view.ownerSlotId);
      if (slot) {
        return findViewPanel(slot.parentViewId);
      }
    }

    return null;
  }

  function jumpToView(viewId: ViewId) {
    closeAllHovers();
    const panelId = findViewPanel(viewId);

    // Focus the panel
    if (panelId && dockviewApi) {
      const panel = dockviewApi.getPanel(panelId);
      panel?.focus();
    }

    // Scroll to element (after a small delay to let focus happen)
    setTimeout(() => {
      const element = document.querySelector(`[data-view-id="${viewId}"]`);
      element?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 50);

    // Highlight
    highlightView(viewId);
  }

  function jumpToEntity(entityId: EntityId): boolean {
    const views = getViewsForEntity(entityId);
    const viewToJump = chooseBestViewToJump(views);
    if (viewToJump) {
      jumpToView(viewToJump.viewId);
      return true;
    }
    return false;
  }

  // ---------------------------------------------------------------------------
  // View manipulation functions (Phase 2a)
  // ---------------------------------------------------------------------------

  function moveViewToSlot(viewId: ViewId, targetSlotId: SlotId) {
    const view = state.views.get(viewId);
    if (!view) return;

    const targetSlot = state.slots.get(targetSlotId);
    if (!targetSlot) return;

    const entityId = view.entityId;

    // If view is panel-owned, close that panel
    if (view.panelId && dockviewApi) {
      const panel = dockviewApi.getPanel(view.panelId);
      if (panel) {
        // Remove from dockview (this will trigger handlePanelClose, so we need to
        // update state first to prevent the default close behavior)
        const newPanels = new Map(state.panels);
        newPanels.delete(view.panelId);
        state = { ...state, panels: newPanels };
        panel.api.close();
      }
    }

    // Update view to be owned by target slot
    const updatedView: View = {
      ...view,
      ownerSlotId: targetSlotId,
      panelId: null,
    };
    state = {
      ...state,
      views: new Map(state.views).set(viewId, updatedView),
    };

    notifyViews();
    notifySlotsForEntity(entityId);
  }

  function getActivePanelId(): PanelId | null {
    return dockviewApi?.activePanel?.id ?? null;
  }

  /**
   * Convert PanelDirection (from context menu) to PanelPosition.
   */
  function getPositionFromDirection(direction?: PanelDirection): PanelPosition | undefined {
    if (!direction || !dockviewApi) return undefined;
    const activePanel = dockviewApi.activePanel;
    if (!activePanel) return undefined;

    if (direction === 'within') {
      // Add to same group as active panel (no direction = same group)
      return { referencePanel: activePanel.id };
    }

    return {
      referencePanel: activePanel.id,
      direction: direction as 'left' | 'right' | 'above' | 'below',
    };
  }

  /**
   * Convert PanelPosition to dockview addPanel position config.
   * Returns a proper discriminated union that dockview expects.
   */
  function toDockviewPosition(position?: PanelPosition) {
    if (!position) return undefined;

    // Prefer referenceGroup (from DND) over referencePanel (from context menu)
    if (position.referenceGroup) {
      // RelativeGroup type - referenceGroup is required
      return {
        referenceGroup: position.referenceGroup,
        direction: position.direction,
      };
    }

    if (position.referencePanel) {
      // RelativePanel type - referencePanel is required
      return {
        referencePanel: position.referencePanel,
        direction: position.direction,
      };
    }

    return undefined;
  }

  function moveViewToNewPanel(viewId: ViewId, position?: PanelPosition) {
    closeAllHovers();
    const view = state.views.get(viewId);
    if (!view) return;

    const entityId = view.entityId;

    if (view.panelId) {
      // Panel-owned view - close old panel and open new one at target position
      const oldPanelId = view.panelId;
      const newPanelId = `panel-${viewId}-${Date.now()}`;

      // Update panel entry - moving upgrades preview to regular
      const newPanels = new Map(state.panels);
      newPanels.delete(oldPanelId);
      newPanels.set(newPanelId, { panelId: newPanelId, viewId, isPreview: false });

      // Update view
      const updatedView: View = { ...view, panelId: newPanelId };

      state = {
        ...state,
        views: new Map(state.views).set(viewId, updatedView),
        panels: newPanels,
      };

      notifyViews();

      // Close old panel and open new one
      if (dockviewApi) {
        const oldPanel = dockviewApi.getPanel(oldPanelId);
        dockviewApi.addPanel({
          id: newPanelId,
          component: 'entity',
          tabComponent: 'entity',
          title: entityId,
          params: { viewId, isPreview: false },
          position: toDockviewPosition(position),
        });
        // close the old panel _after_ opening the new one, in case the new panel is being placed
        // relative to the old one (this can happen if you try to move a view into itself)
        if (oldPanel) {
          oldPanel.api.close();
        }
      }
      return;
    }

    // Slot-owned view - detach from slot and create panel
    if (!view.ownerSlotId) return;

    const panelId = `panel-${viewId}`;

    // Create panel entry
    const panel: Panel = { panelId, viewId, isPreview: false };

    // Update view
    const updatedView: View = {
      ...view,
      ownerSlotId: null,
      panelId,
    };

    state = {
      ...state,
      views: new Map(state.views).set(viewId, updatedView),
      panels: new Map(state.panels).set(panelId, panel),
    };

    notifyViews();
    notifySlotsForEntity(entityId); // The old slot becomes a placeholder

    // Add panel to dockview
    if (dockviewApi) {
      dockviewApi.addPanel({
        id: panelId,
        component: 'entity',
        tabComponent: 'entity',
        title: entityId,
        params: { viewId, isPreview: false },
        position: toDockviewPosition(position),
      });
    }
  }

  function replacePlaceholderWithNewView(slotId: SlotId) {
    const slot = state.slots.get(slotId);
    if (!slot) return;

    // Verify this slot is a placeholder (doesn't own a view)
    const existingOwnedView = Array.from(state.views.values()).find(
      (v) => v.ownerSlotId === slotId
    );
    if (existingOwnedView) {
      console.warn('replacePlaceholderWithNewView called on slot that already owns a view');
      return;
    }

    const entityId = slot.entityId;

    // Create new view owned by this slot
    const viewId = generateViewId();
    const portalNode = createHtmlPortalNode();
    const view: View = {
      viewId,
      entityId,
      portalNode,
      ownerSlotId: slotId,
      panelId: null,
    };

    state = {
      ...state,
      views: new Map(state.views).set(viewId, view),
    };

    notifyViews();
    notifySlotsForEntity(entityId);
  }

  function createNewViewInPanel(entityId: EntityId, position?: PanelPosition) {
    // Always create a new view in a new panel, even if views already exist
    closeAllHovers();
    const viewId = generateViewId();
    const panelId = `panel-${viewId}`;
    const portalNode = createHtmlPortalNode();

    const view: View = {
      viewId,
      entityId,
      portalNode,
      ownerSlotId: null,
      panelId,
    };

    const panel: Panel = {
      panelId,
      viewId,
      isPreview: false,
    };

    state = {
      ...state,
      views: new Map(state.views).set(viewId, view),
      panels: new Map(state.panels).set(panelId, panel),
    };

    notifyViews();
    notifySlotsForEntity(entityId);

    // Open the panel in dockview
    if (dockviewApi) {
      dockviewApi.addPanel({
        id: panelId,
        component: 'entity',
        tabComponent: 'entity',
        title: entityId,
        params: { viewId, isPreview: false },
        position: toDockviewPosition(position),
      });
    }
  }

  function duplicateViewInPanel(sourceViewId: ViewId, position?: PanelPosition) {
    closeAllHovers();
    const sourceView = state.views.get(sourceViewId);
    if (!sourceView) return;

    const entityId = sourceView.entityId;

    // Clear any stale duplication contexts
    duplicatingViews.clear();

    // Create the new view
    const viewId = generateViewId();
    const panelId = `panel-${viewId}`;
    const portalNode = createHtmlPortalNode();

    const view: View = {
      viewId,
      entityId,
      portalNode,
      ownerSlotId: null,
      panelId,
    };

    const panel: Panel = {
      panelId,
      viewId,
      isPreview: false,
    };

    // Set up duplication context for children
    setupDuplicationContext(sourceViewId, viewId);

    state = {
      ...state,
      views: new Map(state.views).set(viewId, view),
      panels: new Map(state.panels).set(panelId, panel),
    };

    notifyViews();
    notifySlotsForEntity(entityId);

    // Open the panel in dockview
    if (dockviewApi) {
      dockviewApi.addPanel({
        id: panelId,
        component: 'entity',
        tabComponent: 'entity',
        title: entityId,
        params: { viewId, isPreview: false },
        position: toDockviewPosition(position),
      });
    }
  }

  function duplicateViewInSlot(sourceViewId: ViewId, targetSlotId: SlotId) {
    const sourceView = state.views.get(sourceViewId);
    if (!sourceView) return;

    const targetSlot = state.slots.get(targetSlotId);
    if (!targetSlot) return;

    // Verify the slot is for the same entity
    if (targetSlot.entityId !== sourceView.entityId) return;

    // Verify this slot is a placeholder (doesn't own a view)
    const existingOwnedView = Array.from(state.views.values()).find(
      (v) => v.ownerSlotId === targetSlotId
    );
    if (existingOwnedView) return;

    const entityId = sourceView.entityId;

    // Clear any stale duplication contexts
    duplicatingViews.clear();

    // Create the new view owned by the slot
    const viewId = generateViewId();
    const portalNode = createHtmlPortalNode();

    const view: View = {
      viewId,
      entityId,
      portalNode,
      ownerSlotId: targetSlotId,
      panelId: null,
    };

    // Set up duplication context for children
    setupDuplicationContext(sourceViewId, viewId);

    state = {
      ...state,
      views: new Map(state.views).set(viewId, view),
    };

    notifyViews();
    notifySlotsForEntity(entityId);
  }

  function closeSubview(viewId: ViewId): boolean {
    const view = state.views.get(viewId);
    if (!view) return false;

    // Must be slot-owned (not a panel root)
    if (!view.ownerSlotId) return false;

    const entityId = view.entityId;

    // Must have other views (don't close the last one)
    const allViews = getViewsForEntity(entityId);
    if (allViews.length <= 1) return false;

    // Destroy the view - slot remains and will re-render as placeholder
    const newViews = new Map(state.views);
    newViews.delete(viewId);
    state = { ...state, views: newViews };

    notifyViews();
    notifySlotsForEntity(entityId);

    return true;
  }

  /**
   * Handle drop of a ref drag (from RefLink, InlineRefLink, or hover-preview DraggableID).
   * Without alt: moves existing view to position, or creates new if none exists.
   * With alt: always creates a fresh new view (no duplication context).
   */
  function handleRefDrop(
    entityId: EntityId,
    position?: PanelPosition,
    isAltHeld: boolean = false
  ): void {
    if (isAltHeld) {
      // Alt+drag: always create fresh new view (no duplication context)
      createNewViewInPanel(entityId, position);
      return;
    }

    // No alt: try to move existing view, or create new if none exists
    const existingViews = getViewsForEntity(entityId);
    if (existingViews.length > 0) {
      // Move the best view (same logic as click-to-jump)
      const viewToMove = chooseBestViewToJump(existingViews);
      if (viewToMove) {
        moveViewToNewPanel(viewToMove.viewId, position);
        return;
      }
    }

    // No existing view - create new
    createNewViewInPanel(entityId, position);
  }

  // ---------------------------------------------------------------------------
  // Preview Panel Helpers (Phase 2d)
  // ---------------------------------------------------------------------------

  /**
   * Find a group adjacent to the source panel's group in the specified direction.
   * Returns null if no adjacent group found.
   */
  function findAdjacentGroup(
    sourcePanelId: PanelId,
    direction: 'left' | 'right'
  ): DockviewGroupPanel | null {
    console.log('[findAdjacentGroup] Looking for group', direction, 'of panel', sourcePanelId);

    if (!dockviewApi) {
      console.log('[findAdjacentGroup] No dockviewApi');
      return null;
    }

    const sourcePanel = dockviewApi.getPanel(sourcePanelId);
    if (!sourcePanel) {
      console.log('[findAdjacentGroup] Source panel not found:', sourcePanelId);
      return null;
    }

    const sourceGroup = sourcePanel.group;
    if (!sourceGroup) {
      console.log('[findAdjacentGroup] Source panel has no group');
      return null;
    }

    const sourceRect = sourceGroup.element.getBoundingClientRect();
    const sourceEdge = direction === 'right' ? sourceRect.right : sourceRect.left;
    const sourceVCenter = sourceRect.top + sourceRect.height / 2;

    console.log('[findAdjacentGroup] Source group:', sourceGroup.id,
      'rect:', { left: sourceRect.left, right: sourceRect.right, top: sourceRect.top, bottom: sourceRect.bottom },
      'looking for edge at:', sourceEdge);

    // Find groups whose edge aligns with source's opposite edge
    const candidates: DockviewGroupPanel[] = [];
    for (const group of dockviewApi.groups) {
      if (group.id === sourceGroup.id) continue;

      const rect = group.element.getBoundingClientRect();
      const targetEdge = direction === 'right' ? rect.left : rect.right;

      console.log('[findAdjacentGroup] Checking group:', group.id,
        'rect:', { left: rect.left, right: rect.right },
        'targetEdge:', targetEdge,
        'diff:', Math.abs(targetEdge - sourceEdge));

      // Check if edges are adjacent (within 5px tolerance)
      if (Math.abs(targetEdge - sourceEdge) < 5) {
        candidates.push(group);
      }
    }

    console.log('[findAdjacentGroup] Found', candidates.length, 'candidates');

    if (candidates.length === 0) return null;

    // Pick the one with closest vertical center
    const result = candidates.reduce((best, g) => {
      const rect = g.element.getBoundingClientRect();
      const vCenter = rect.top + rect.height / 2;
      const bestRect = best.element.getBoundingClientRect();
      const bestVCenter = bestRect.top + bestRect.height / 2;
      return Math.abs(vCenter - sourceVCenter) < Math.abs(bestVCenter - sourceVCenter)
        ? g
        : best;
    });

    console.log('[findAdjacentGroup] Selected group:', result.id);
    return result;
  }

  /**
   * Find the preview panel in a group, if any.
   */
  function findPreviewPanelInGroup(group: DockviewGroupPanel): PanelId | null {
    for (const panel of group.panels) {
      const ourPanel = state.panels.get(panel.id);
      if (ourPanel?.isPreview) {
        return panel.id;
      }
    }
    return null;
  }

  /**
   * Replace the contents of a preview panel with a new entity.
   */
  function replacePreviewContents(previewPanelId: PanelId, entityId: EntityId): void {
    const panel = state.panels.get(previewPanelId);
    if (!panel || !panel.isPreview) return;

    const oldView = state.views.get(panel.viewId);
    if (!oldView) return;

    const oldEntityId = oldView.entityId;

    // Create new view for the new entity
    const viewId = generateViewId();
    const portalNode = createHtmlPortalNode();

    const newView: View = {
      viewId,
      entityId,
      portalNode,
      ownerSlotId: null,
      panelId: previewPanelId,
    };

    // Update panel to point to new view
    const updatedPanel: Panel = {
      ...panel,
      viewId,
    };

    // Destroy old view
    const newViews = new Map(state.views);
    newViews.delete(oldView.viewId);
    newViews.set(viewId, newView);

    state = {
      ...state,
      views: newViews,
      panels: new Map(state.panels).set(previewPanelId, updatedPanel),
    };

    notifyViews();
    notifySlotsForEntity(oldEntityId); // Old entity's slots may need update
    notifySlotsForEntity(entityId); // New entity's slots may need update

    // Update dockview panel params and title
    if (dockviewApi) {
      const dockviewPanel = dockviewApi.getPanel(previewPanelId);
      if (dockviewPanel) {
        dockviewPanel.api.updateParameters({ viewId, isPreview: true });
        dockviewPanel.api.setTitle(entityId);
      }
    }
  }

  /**
   * Open entity in a preview panel in the specified direction from source panel.
   * @param options.upgradePreview - If true and jumping to a preview panel, upgrade it to regular
   */
  function openInPreviewOrJumpTo(
    entityId: EntityId,
    direction: 'left' | 'right',
    sourcePanelId: PanelId | null,
    options?: { upgradePreview?: boolean }
  ): void {
    const { upgradePreview = false } = options ?? {};
    console.log('[openInPreviewOrJumpTo] Opening', entityId, 'direction:', direction, 'from panel:', sourcePanelId, 'upgradePreview:', upgradePreview);

    // If views exist for entity, jump to best one
    const existingViews = getViewsForEntity(entityId);
    if (existingViews.length > 0) {
      console.log('[openInPreviewOrJumpTo] Entity already has views, jumping');
      const viewToJump = chooseBestViewToJump(existingViews);
      if (viewToJump) {
        // If upgradePreview is true and the view is in a preview panel, upgrade it to regular (pins it)
        if (upgradePreview) {
          const panelId = findViewPanel(viewToJump.viewId);
          if (panelId && isPanelPreview(panelId)) {
            console.log('[openInPreviewOrJumpTo] Upgrading preview panel on jump:', panelId);
            upgradePreviewToRegular(panelId);
          }
        }
        jumpToView(viewToJump.viewId);
      }
      return;
    }

    // If no source panel, fall back to current behavior
    if (!sourcePanelId || !dockviewApi) {
      console.log('[openInPreviewOrJumpTo] No source panel or API, falling back to openEntity');
      openEntity(entityId);
      return;
    }

    closeAllHovers();

    // Find adjacent group in the specified direction
    const adjacentGroup = findAdjacentGroup(sourcePanelId, direction);
    console.log('[openInPreviewOrJumpTo] Adjacent group:', adjacentGroup?.id ?? 'none');

    if (adjacentGroup) {
      // Check for existing preview panel in that group
      const existingPreviewId = findPreviewPanelInGroup(adjacentGroup);
      console.log('[openInPreviewOrJumpTo] Existing preview in group:', existingPreviewId ?? 'none');
      if (existingPreviewId) {
        // Replace preview contents
        console.log('[openInPreviewOrJumpTo] Replacing preview contents');
        replacePreviewContents(existingPreviewId, entityId);
        // Focus the panel
        const panel = dockviewApi.getPanel(existingPreviewId);
        panel?.focus();
        return;
      }
    }

    // Create new preview panel
    console.log('[openInPreviewOrJumpTo] Creating new preview panel');
    const viewId = generateViewId();
    const panelId = `panel-${viewId}`;
    const portalNode = createHtmlPortalNode();

    const view: View = {
      viewId,
      entityId,
      portalNode,
      ownerSlotId: null,
      panelId,
    };

    const panel: Panel = {
      panelId,
      viewId,
      isPreview: true,
    };

    state = {
      ...state,
      views: new Map(state.views).set(viewId, view),
      panels: new Map(state.panels).set(panelId, panel),
    };

    notifyViews();
    notifySlotsForEntity(entityId);

    // Open panel in dockview
    if (dockviewApi) {
      const sourcePanel = dockviewApi.getPanel(sourcePanelId);
      const position = adjacentGroup
        ? { referenceGroup: adjacentGroup }  // Add to existing adjacent group
        : sourcePanel
          ? { referencePanel: sourcePanelId, direction }  // Create new group in direction
          : undefined;

      console.log('[openInPreviewOrJumpTo] Adding panel with position:',
        adjacentGroup ? `referenceGroup: ${adjacentGroup.id}` :
        sourcePanel ? `referencePanel: ${sourcePanelId}, direction: ${direction}` :
        'none');

      dockviewApi.addPanel({
        id: panelId,
        component: 'entity',
        tabComponent: 'entity',
        title: entityId,
        params: { viewId, isPreview: true },
        position,
      });
    }
  }

  /**
   * Upgrade a preview panel to a regular panel.
   */
  function upgradePreviewToRegular(panelId: PanelId): void {
    const panel = state.panels.get(panelId);
    if (!panel || !panel.isPreview) return;

    // Update panel to not be a preview
    const updatedPanel: Panel = {
      ...panel,
      isPreview: false,
    };

    state = {
      ...state,
      panels: new Map(state.panels).set(panelId, updatedPanel),
    };

    // Update params in dockview to trigger tab re-render
    if (dockviewApi) {
      const view = state.views.get(panel.viewId);
      const dockviewPanel = dockviewApi.getPanel(panelId);
      if (dockviewPanel && view) {
        dockviewPanel.api.updateParameters({ viewId: panel.viewId, isPreview: false });
      }
    }

    groupLayoutCache = null;
    globalListeners.forEach((l) => l());
  }

  /**
   * Check if a panel is a preview panel.
   */
  function isPanelPreview(panelId: PanelId): boolean {
    return state.panels.get(panelId)?.isPreview ?? false;
  }

  // ---------------------------------------------------------------------------
  // Group sidebar support (Phase 2e)
  // ---------------------------------------------------------------------------

  /**
   * Get current group/panel layout for sidebar display.
   * Cached to avoid infinite loops with useSyncExternalStore.
   */
  function getGroupLayout(): GroupLayout[] {
    if (groupLayoutCache !== null) {
      return groupLayoutCache;
    }

    if (!dockviewApi) {
      groupLayoutCache = [];
      return groupLayoutCache;
    }

    const activePanelId = dockviewApi.activePanel?.id ?? null;
    const result: GroupLayout[] = [];

    for (const group of dockviewApi.groups) {
      const panels: PanelLayout[] = [];

      for (const dockPanel of group.panels) {
        const panelId = dockPanel.id;
        const ourPanel = state.panels.get(panelId);

        // Build panel layout info
        const layout: PanelLayout = {
          panelId,
          title: dockPanel.title ?? panelId,
          isActive: panelId === activePanelId,
          isPreview: ourPanel?.isPreview ?? false,
        };

        // For entity panels (ones we track), add entity info
        if (ourPanel) {
          const view = state.views.get(ourPanel.viewId);
          if (view) {
            layout.entityId = view.entityId;
            // entityType will be looked up by the sidebar component from DataBundle
          }
        }

        panels.push(layout);
      }

      result.push({
        groupId: group.id,
        panels,
      });
    }

    groupLayoutCache = result;
    return groupLayoutCache;
  }

  // ---------------------------------------------------------------------------
  // Return the registry
  // ---------------------------------------------------------------------------

  return {
    subscribeViews,
    getViewsSnapshot,
    subscribeSlot,
    getSlotContentSnapshot,
    subscribe,
    getSnapshot,
    getView,
    getViewsForEntity,
    openEntity,
    setDockviewApi,
    handlePanelClose,
    // Phase 2a
    subscribeHighlight,
    getHighlightedViewId,
    highlightView,
    findViewPanel,
    jumpToView,
    jumpToEntity,
    moveViewToSlot,
    moveViewToNewPanel,
    replacePlaceholderWithNewView,
    createNewViewInPanel,
    duplicateViewInPanel,
    duplicateViewInSlot,
    closeSubview,
    handleRefDrop,
    getActivePanelId,
    // Phase 2b
    getPositionFromDirection,
    setPlaceholderDragActive: (active: boolean) => {
      placeholderDragActive = active;
    },
    isPlaceholderDragActive: () => placeholderDragActive,
    // Hover management
    registerHoverCloseListener: (listener: () => void) => {
      hoverCloseListeners.add(listener);
      return () => hoverCloseListeners.delete(listener);
    },
    closeAllHovers,
    // Phase 2d - Preview panels
    openInPreviewOrJumpTo,
    upgradePreviewToRegular,
    isPanelPreview,
    // Phase 2e - Group sidebar
    getGroupLayout,
  };
}
