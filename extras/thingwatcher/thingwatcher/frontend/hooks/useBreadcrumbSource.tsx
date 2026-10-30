/**
 * BreadcrumbSourceContext - Tracks navigation context for creating breadcrumbs
 *
 * The breadcrumb source context tracks a chain of pending breadcrumbs that should
 * be created when the user navigates away. This supports nested structures like
 * ChildNodeView and hover preview cards.
 *
 * How the chain works:
 * - When viewing thing A in a panel: { pendingBreadcrumbs: [], currentKey: A, type: 'link' }
 * - When thing A contains ChildNodeView for B: { pendingBreadcrumbs: [A→B], currentKey: B, type: 'link' }
 * - When B contains ChildNodeView for C: { pendingBreadcrumbs: [A→B, B→C], currentKey: C, type: 'link' }
 * - When user clicks link to D inside C: Creates breadcrumbs A→B, B→C, and C→D all at once
 *
 * Context placement:
 * - ThingPanelContent (renderer section): Wraps with type: 'link' context
 * - ThingPanelContent (backlinks section): Wraps with type: 'backlink' context
 * - ChildNodeView: Extends parent chain with { fromKey: parent.currentKey, toKey: childKey, type: 'link' }
 * - HoverPreviewCard: Extends parent chain (starts new chain if no parent)
 * - BacklinksHoverCard: Same as HoverPreviewCard but with type: 'backlink'
 * - Graph/ThingsList/Roots: No provider - direct selection, no breadcrumbs
 * - BreadcrumbCard NodeRefs: Provider with currentKey: null - explicitly prevent new breadcrumbs
 */

import React, { createContext, useContext, type ReactNode } from 'react';

// =============================================================================
// Types
// =============================================================================

/** Breadcrumb type - how the navigation occurred */
export type BreadcrumbType = 'link' | 'backlink';

/** A pending breadcrumb (without ID - ID is assigned when created) */
export interface PendingBreadcrumb {
  /** The thing we navigated FROM */
  fromKey: string;
  /** The thing we navigated TO */
  toKey: string;
  /** Type of navigation */
  type: BreadcrumbType;
}

/**
 * Breadcrumb source context value.
 *
 * Tracks the current navigation context for creating breadcrumbs.
 */
export interface BreadcrumbSource {
  /** Chain of pending breadcrumbs to create when navigating away */
  pendingBreadcrumbs: PendingBreadcrumb[];
  /** Current thing key (for adding the next step), or null to explicitly prevent breadcrumb creation */
  currentKey: string | null;
  /** Type of breadcrumb to create for the next navigation */
  type: BreadcrumbType;
}

// =============================================================================
// Context
// =============================================================================

const BreadcrumbSourceContext = createContext<BreadcrumbSource | null>(null);

// =============================================================================
// Provider
// =============================================================================

export interface BreadcrumbSourceProviderProps {
  value: BreadcrumbSource | null;
  children: ReactNode;
}

/**
 * Provider for the breadcrumb source context.
 *
 * Components like ThingPanelContent, ChildNodeView, and HoverPreviewCard
 * wrap their content with this provider to establish the navigation context.
 */
export function BreadcrumbSourceProvider({ value, children }: BreadcrumbSourceProviderProps) {
  return (
    <BreadcrumbSourceContext.Provider value={value}>
      {children}
    </BreadcrumbSourceContext.Provider>
  );
}

// =============================================================================
// Hook
// =============================================================================

/**
 * Get the current breadcrumb source context.
 *
 * Returns null if no context is set (e.g., when clicking directly in Graph/ThingsList).
 */
export function useBreadcrumbSource(): BreadcrumbSource | null {
  return useContext(BreadcrumbSourceContext);
}
