/**
 * ThingPanelContent - Shared panel content component
 *
 * This component provides the full panel chrome for displaying a thing:
 * - Breadcrumb card (if any breadcrumbs point to this thing)
 * - Source info header (where the thing came from + pin button)
 * - Meta header (type badge, shortId, key display)
 * - LowLevelNodeView (the actual renderer)
 * - Backlinks section
 * - Trigger records section
 *
 * Used by DetailsPanel, ThingViewPanel, and EmbedApp.
 */

import React, { useMemo } from 'react';
import { Pin, PinOff, Database, Archive } from 'lucide-react';
import {
  useThing,
  useThingWatcher,
  usePinnedThings,
  useRegisterActiveMark,
  BreadcrumbSourceProvider,
  type BreadcrumbSource,
} from '../../hooks';
import type { Link, TriggerRecord, ThingSource } from '../../types';
import type { MarkSource } from '../../hooks/useActiveMarks';
import { LowLevelNodeView } from '../../lib/LowLevelNodeView';
import { NodeRef } from '../../lib/ThingRenderLib';
import { ThingContextMenu } from '../ThingContextMenu';
import { JsonTree } from '../common/JsonTree';
import { BreadcrumbCard } from './BreadcrumbCard';
import styles from './ThingPanelContent.module.css';

// =============================================================================
// Types
// =============================================================================

export interface ThingPanelContentProps {
  /** Thing key as base64 string */
  thingKey: string;
  /** Callback when user clicks a link/backlink to navigate to another thing */
  onSelectThing: (key: string) => void;
  /** Trigger records at the current tick for this thing */
  triggerRecords: TriggerRecord[];
  /** Callback after a successful trigger (to refetch trigger records) */
  onTrigger: () => void;
  /** Whether this thing will disappear in the next tick */
  isDisappearing?: boolean;
  /** Source of the active mark for this panel (for tracking active things) */
  markSource: MarkSource;
  /** Optional label for debugging (e.g., panel ID) */
  markLabel?: string;
}

// =============================================================================
// Component
// =============================================================================

export function ThingPanelContent({
  thingKey,
  onSelectThing,
  triggerRecords,
  onTrigger,
  isDisappearing = false,
  markSource,
  markLabel,
}: ThingPanelContentProps) {
  const { isLive, resolvedTick } = useThingWatcher();
  const { isPinned, togglePin } = usePinnedThings();

  // Register this thing as active while the panel is mounted
  useRegisterActiveMark(thingKey, markSource, markLabel);

  const pinned = isPinned(thingKey);

  // Breadcrumb contexts for different sections
  // Main content (renderer) uses 'link' type
  const linkContext = useMemo((): BreadcrumbSource => ({
    pendingBreadcrumbs: [],
    currentKey: thingKey,
    type: 'link',
  }), [thingKey]);

  // Backlinks section uses 'backlink' type
  const backlinkContext = useMemo((): BreadcrumbSource => ({
    pendingBreadcrumbs: [],
    currentKey: thingKey,
    type: 'backlink',
  }), [thingKey]);

  // Fetch thing data for meta info and backlinks
  // Note: LowLevelNodeView will also fetch, but data is cached via graphcache
  const { thing, loading, error } = useThing({
    keyInput: { key: thingKey },
    includeData: true,
  });

  if (loading && !thing) {
    return (
      <div className={styles.content}>
        <div className={styles.loading}>Loading...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.content}>
        <div className={styles.error}>{error.message}</div>
      </div>
    );
  }

  if (!thing) {
    // Show truncated key for identification
    const keyDisplay = thingKey.length > 30 ? `${thingKey.slice(0, 30)}...` : thingKey;

    return (
      <div className={styles.content}>
        {/* Source/pin header even for not-found things */}
        <div className={styles.sourceHeader}>
          <div className={styles.sourceInfo}>
            <span className={styles.notPresentBadge}>
              ⚠️ Not present at tick {resolvedTick}
            </span>
          </div>
          <button
            className={`${styles.pinButton} ${pinned ? styles.pinned : ''}`}
            onClick={() => togglePin(thingKey)}
            title={pinned ? 'Unpin this thing' : 'Pin this thing'}
          >
            {pinned ? <Pin size={16} /> : <PinOff size={16} />}
          </button>
        </div>
        <div className={styles.empty}>
          <div>Thing not found</div>
          <div className={styles.notFoundKey} title={thingKey}>
            Key: {keyDisplay}
          </div>
        </div>
      </div>
    );
  }

  // Build source display info
  const sources = thing.sources ?? [];
  const primarySource = sources[0];
  const secondarySources = sources.slice(1);

  return (
    <div className={styles.content}>
      {/* Breadcrumb card - shows navigation history to this thing */}
      <BreadcrumbCard thingKey={thingKey} />

      {/* Source info header with pin button */}
      <div className={styles.sourceHeader}>
        <div className={styles.sourceInfo}>
          {primarySource?.kind === 'SNAPSHOT' && (
            <span className={styles.sourceBadge}>
              <Database size={14} />
              <span>Captured in snapshot</span>
            </span>
          )}
          {primarySource?.kind === 'STORE' && (
            <span className={styles.sourceBadge}>
              <Archive size={14} />
              <span>From store: {primarySource.storeName}</span>
            </span>
          )}
          {secondarySources.map((source, i) => (
            <span
              key={i}
              className={`${styles.secondarySource} ${source.overridden ? styles.overridden : ''}`}
            >
              <Archive size={12} />
              <span>
                Also in: {source.storeName}
                {source.overridden && ' (overridden)'}
              </span>
            </span>
          ))}
        </div>
        <button
          className={`${styles.pinButton} ${pinned ? styles.pinned : ''}`}
          onClick={() => togglePin(thingKey)}
          title={pinned ? 'Unpin this thing' : 'Pin this thing'}
        >
          {pinned ? <Pin size={16} /> : <PinOff size={16} />}
        </button>
      </div>

      {/* Meta info section */}
      <div className={styles.section}>
        <div className={styles.metaHeader}>
          <span className={styles.thingType}>{thing.thingType}</span>
          {isDisappearing && (
            <span className={styles.disappearingBadge} title="This thing will disappear in the next tick">
              Disappears next tick
            </span>
          )}
        </div>
        <div className={styles.meta}>
          <span className={styles.shortId}>#{thing.shortId}</span>
          <span className={styles.key} title={thing.key}>
            {thing.keyJSON !== null && thing.keyJSON !== undefined
              ? JSON.stringify(thing.keyJSON)
              : thing.key.length > 30
                ? `${thing.key.slice(0, 30)}...`
                : thing.key}
          </span>
        </div>
      </div>

      {/* Renderer section - wrapped with link context for breadcrumbs */}
      <BreadcrumbSourceProvider value={linkContext}>
        <div className={styles.section}>
          <LowLevelNodeView
            thingKey={thingKey}
            isTopLevel={true}
            args={{}}
          />
        </div>
      </BreadcrumbSourceProvider>

      {/* Trigger records at this tick */}
      {triggerRecords.length > 0 && (
        <div className={styles.section}>
          <h4 className={styles.sectionTitle}>Triggered at this tick</h4>
          <div className={styles.triggerRecords}>
            {triggerRecords.map((tr) => (
              <div key={tr.id} className={styles.triggerRecord}>
                <span className={styles.triggerRecordId}>#{tr.id}</span>
                {tr.payload !== null && (
                  <div className={styles.triggerRecordPayload}>
                    <JsonTree data={tr.payload} />
                  </div>
                )}
                {tr.payload === null && (
                  <span className={styles.noData}>(no payload)</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Backlinks section - wrapped with backlink context for breadcrumbs */}
      {thing.backlinks && thing.backlinks.length > 0 && (
        <BreadcrumbSourceProvider value={backlinkContext}>
          <div className={styles.section}>
            <h4 className={styles.sectionTitle}>Backlinks ({thing.backlinks.length})</h4>
            <div className={styles.backlinks}>
              {thing.backlinks.map((link: Link, i: number) => (
                <div key={i} className={styles.backlinkRow}>
                  <div className={styles.backlinkNodeRef}>
                    <NodeRef thingKey={link.fromThing.key} />
                  </div>
                  <div className={styles.backlinkMeta}>
                    <span className={styles.linkArrow}>→</span>
                    <span className={styles.linkType}>{link.linkType}</span>
                    <span className={styles.linkKey} title={link.linkKey}>
                      [
                      {link.linkKeyJSON !== null && link.linkKeyJSON !== undefined
                        ? JSON.stringify(link.linkKeyJSON)
                        : link.linkKey.length > 20
                          ? `${link.linkKey.slice(0, 20)}...`
                          : link.linkKey}
                      ]
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </BreadcrumbSourceProvider>
      )}
    </div>
  );
}
