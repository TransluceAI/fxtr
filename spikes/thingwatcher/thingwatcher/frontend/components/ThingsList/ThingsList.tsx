import React, { useState, useMemo, useCallback } from 'react';
import { Pin } from 'lucide-react';
import type { Thing, ChangeInfo } from '../../types';
import { useThingWatcher, usePinnedThings } from '../../hooks';
import { NodeRef } from '../../lib/ThingRenderLib';
import styles from './ThingsList.module.css';

// =============================================================================
// PinnedThingItem - Component for each pinned thing
// =============================================================================

interface PinnedThingItemProps {
  thingKey: string;
  selectedThingKey: string | null;
  onSelectThing: (key: string) => void;
  onUnpin: () => void;
}

/**
 * Renders a pinned thing item.
 * Uses NodeRef which handles loading/not-found states internally.
 */
function PinnedThingItem({
  thingKey,
  selectedThingKey,
  onSelectThing,
  onUnpin,
}: PinnedThingItemProps) {
  const handleUnpinClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onUnpin();
  };

  return (
    <div
      className={`${styles.pinnedItem} ${
        thingKey === selectedThingKey ? styles.selected : ''
      }`}
      onClick={() => onSelectThing(thingKey)}
    >
      <div className={styles.pinnedItemContent}>
        <NodeRef thingKey={thingKey} />
      </div>
      <button
        className={styles.unpinButton}
        onClick={handleUnpinClick}
        title="Unpin"
      >
        <Pin size={14} />
      </button>
    </div>
  );
}

// =============================================================================
// ThingsList - Main component
// =============================================================================

interface ThingsListProps {
  things: Thing[];
  changeInfo: ChangeInfo;
  selectedThingKey: string | null;  // b64 key - canonical identifier
  onSelectThing: (key: string) => void;
  isLive: boolean;
  triggeredShortIds: Set<number>;
}

type SortMode = 'shortId' | 'type' | 'change';

export function ThingsList({
  things,
  changeInfo,
  selectedThingKey,
  onSelectThing,
  isLive,
  triggeredShortIds,
}: ThingsListProps) {
  const [search, setSearch] = useState('');
  const [showChangesOnly, setShowChangesOnly] = useState(false);
  const [showTriggerableOnly, setShowTriggerableOnly] = useState(false);
  const [sortMode, setSortMode] = useState<SortMode>('shortId');

  // Get triggerableKeys from context
  const { triggerableKeys } = useThingWatcher();
  const { pinnedKeys, unpin } = usePinnedThings();

  // Create lookup for things by key
  const thingsByKey = useMemo(() => {
    const map = new Map<string, Thing>();
    for (const thing of things) {
      map.set(thing.key, thing);
    }
    return map;
  }, [things]);

  // Helper to check if a key has any change status
  const hasChange = useCallback((key: string): boolean => {
    return changeInfo.addedKeys.has(key) ||
           changeInfo.modifiedKeys.has(key) ||
           changeInfo.disappearingKeys.has(key);
  }, [changeInfo]);

  // Get sort priority for a thing (lower = higher priority)
  const getSortPriority = useCallback((key: string): number => {
    const isAdded = changeInfo.addedKeys.has(key);
    const isModified = changeInfo.modifiedKeys.has(key);
    const isDisappearing = changeInfo.disappearingKeys.has(key);

    if (isAdded && !isDisappearing) return 0;           // added only
    if (isAdded && isDisappearing) return 1;            // added + disappearing
    if (isModified && !isDisappearing) return 2;        // modified only
    if (isModified && isDisappearing) return 3;         // modified + disappearing
    if (isDisappearing) return 4;                       // disappearing only
    return 5;                                           // unchanged
  }, [changeInfo]);

  // Get border class for combined states
  const getBorderClass = useCallback((key: string): string => {
    const isAdded = changeInfo.addedKeys.has(key);
    const isModified = changeInfo.modifiedKeys.has(key);
    const isDisappearing = changeInfo.disappearingKeys.has(key);

    if (isAdded && isDisappearing) return styles.addedDisappearing;
    if (isModified && isDisappearing) return styles.modifiedDisappearing;
    if (isAdded) return styles.added;
    if (isModified) return styles.modified;
    if (isDisappearing) return styles.disappearing;
    return '';
  }, [changeInfo]);

  // Filter and sort things
  const filteredThings = useMemo(() => {
    let result = [...things];

    // Apply search filter
    if (search) {
      const searchLower = search.toLowerCase();
      result = result.filter(t =>
        t.thingType.toLowerCase().includes(searchLower) ||
        t.key.toLowerCase().includes(searchLower) ||
        String(t.shortId).includes(searchLower)
      );
    }

    // Apply changes filter
    if (showChangesOnly) {
      result = result.filter(t => hasChange(t.key));
    }

    // Apply triggerable filter
    if (showTriggerableOnly) {
      result = result.filter(t => triggerableKeys.has(t.key));
    }

    // Sort
    result.sort((a, b) => {
      switch (sortMode) {
        case 'shortId':
          return a.shortId - b.shortId;
        case 'type':
          return a.thingType.localeCompare(b.thingType);
        case 'change': {
          return getSortPriority(a.key) - getSortPriority(b.key);
        }
        default:
          return 0;
      }
    });

    return result;
  }, [things, search, showChangesOnly, showTriggerableOnly, sortMode, hasChange, getSortPriority, triggerableKeys]);

  // Get pinned keys as array for rendering
  const pinnedKeysArray = useMemo(() => Array.from(pinnedKeys), [pinnedKeys]);
  const hasPinnedThings = pinnedKeysArray.length > 0;

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <h3 className={styles.title}>Things</h3>
        <span className={styles.count}>{filteredThings.length}</span>
      </div>

      <div className={styles.filters}>
        <input
          type="text"
          className={styles.search}
          placeholder="Search by type or key..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className={styles.filterRow}>
          <label className={styles.checkbox}>
            <input
              type="checkbox"
              checked={showChangesOnly}
              onChange={(e) => setShowChangesOnly(e.target.checked)}
            />
            Changes only
          </label>
          <label className={styles.checkbox}>
            <input
              type="checkbox"
              checked={showTriggerableOnly}
              onChange={(e) => setShowTriggerableOnly(e.target.checked)}
            />
            Triggerable
          </label>
        </div>
        <div className={styles.filterRow}>
          <span className={styles.sortLabel}>Sort:</span>
          <select
            className={styles.sortSelect}
            value={sortMode}
            onChange={(e) => setSortMode(e.target.value as SortMode)}
          >
            <option value="shortId">By ID</option>
            <option value="type">By Type</option>
            <option value="change">By Change</option>
          </select>
        </div>
      </div>

      <div className={styles.list}>
        {/* Pinned section */}
        {hasPinnedThings && (
          <div className={styles.pinnedSection}>
            <div className={styles.pinnedHeader}>
              <Pin size={12} />
              <span>Pinned ({pinnedKeysArray.length})</span>
            </div>
            {pinnedKeysArray.map(key => (
              <PinnedThingItem
                key={key}
                thingKey={key}
                selectedThingKey={selectedThingKey}
                onSelectThing={onSelectThing}
                onUnpin={() => unpin(key)}
              />
            ))}
          </div>
        )}

        {/* Main things list */}
        {filteredThings.length === 0 ? (
          <div className={styles.empty}>
            {things.length === 0 ? 'No things' : 'No matches'}
          </div>
        ) : (
          filteredThings.map(thing => {
            const isThingTriggerable = triggerableKeys.has(thing.key);
            const isPinned = pinnedKeys.has(thing.key);
            return (
              <div
                key={thing.key}
                className={`${styles.item} ${getBorderClass(thing.key)} ${
                  thing.key === selectedThingKey ? styles.selected : ''
                }`}
                onClick={() => onSelectThing(thing.key)}
              >
                <NodeRef thingKey={thing.key} />
                {/* Pin indicator */}
                {isPinned && (
                  <span className={styles.pinnedBadge} title="Pinned">
                    <Pin size={10} />
                  </span>
                )}
                {/* Change and status badges - can show both status AND disappearing */}
                {changeInfo.addedKeys.has(thing.key) && (
                  <span className={styles.addedBadge} title="Added">+</span>
                )}
                {changeInfo.modifiedKeys.has(thing.key) && (
                  <span className={styles.modifiedBadge} title="Modified">M</span>
                )}
                {changeInfo.disappearingKeys.has(thing.key) && (
                  <span className={styles.disappearingBadge} title="About to disappear">-</span>
                )}
                {triggeredShortIds.has(thing.shortId) && (
                  <span className={styles.triggeredBadge} title="Triggered at this tick">
                    T
                  </span>
                )}
                {isThingTriggerable && !triggeredShortIds.has(thing.shortId) && (
                  <span
                    className={`${styles.triggerBadge} ${!isLive ? styles.triggerBadgeInactive : ''}`}
                    title={isLive ? "Triggerable" : "Triggerable (view live to trigger)"}
                  >
                    T
                  </span>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
