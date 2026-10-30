/**
 * BreadcrumbCard - Shows breadcrumbs pointing to a thing
 *
 * Displays a collapsible card showing all the breadcrumbs that lead to this thing.
 * Each breadcrumb shows:
 * - A NodeRef to the source thing (where we navigated from)
 * - The breadcrumb type (link or backlink)
 * - A remove button to delete the breadcrumb
 *
 * NodeRefs inside the card are wrapped with a null breadcrumb source context
 * to prevent creating new breadcrumbs when clicking them (following an existing
 * breadcrumb backwards should not create a new one).
 */

import React from 'react';
import {
  useBreadcrumbsFor,
  usePinnedThings,
  BreadcrumbSourceProvider,
} from '../../hooks';
import { NodeRef } from '../../lib/ThingRenderLib';
import styles from './BreadcrumbCard.module.css';

// =============================================================================
// Types
// =============================================================================

export interface BreadcrumbCardProps {
  /** The thing key to show breadcrumbs for */
  thingKey: string;
}

// =============================================================================
// Component
// =============================================================================

/**
 * BreadcrumbCard - Shows breadcrumbs pointing to a thing.
 *
 * Returns null if there are no breadcrumbs.
 */
export function BreadcrumbCard({ thingKey }: BreadcrumbCardProps) {
  const breadcrumbs = useBreadcrumbsFor(thingKey);
  const { removeBreadcrumb } = usePinnedThings();

  if (breadcrumbs.length === 0) {
    return null;
  }

  // Null context prevents NodeRefs inside from creating new breadcrumbs
  const nullContext = {
    pendingBreadcrumbs: [],
    currentKey: null,
    type: 'link' as const,
  };

  return (
    <BreadcrumbSourceProvider value={nullContext}>
      <details className={styles.breadcrumbCard} open>
        <summary className={styles.breadcrumbCardHeader}>
          <span className={styles.breadcrumbCardArrow}>
            {/* Arrow rotates when open via CSS */}
          </span>
          <span>Reached from: ({breadcrumbs.length})</span>
        </summary>
        <div className={styles.breadcrumbList}>
          {breadcrumbs.map((bc) => (
            <div key={bc.id} className={styles.breadcrumbEntry}>
              <div className={styles.breadcrumbNodeRef}>
                <NodeRef thingKey={bc.fromKey} />
              </div>
              <span className={styles.breadcrumbType}>via {bc.type}</span>
              <button
                className={styles.breadcrumbRemove}
                onClick={() => removeBreadcrumb(bc.id)}
                title="Remove this breadcrumb"
              >
                &times;
              </button>
            </div>
          ))}
        </div>
      </details>
    </BreadcrumbSourceProvider>
  );
}
