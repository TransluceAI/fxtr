/**
 * DropRegion renderer - Drop zone that accepts dragged things
 *
 * Demonstrates:
 * - useThingDropRegion hook to create drop zones
 * - ThingDropRegion component to suppress dockview drops
 * - Mime type filtering for accepting specific thing types
 * - Visual feedback during drag
 * - Persistent state for pending drops
 * - Trigger to confirm drops
 */

import { useState, useCallback } from 'react';
import {
  usePayload,
  useLinks,
  useThingMetadata,
  useTriggerable,
  useTrigger,
  usePersistentThingState,
  useThingDropRegion,
  NodeRef,
} from 'thingwatcher-bindings';
import type {
  ThingWatcherRenderConfig,
  FullRendererProps,
  RefRendererProps,
  ThingDropInfo,
  TriggerResult,
} from 'thingwatcher-bindings';

interface DropRegionPayload {
  name: string;
  filter_mode: string; // "any", "type:Apple", "type:Orange", etc.
}

function DropRegionFull({ handle }: FullRendererProps) {
  const data = usePayload<DropRegionPayload>(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);
  const [triggerable] = useTriggerable(handle);
  const trigger = useTrigger(handle);

  // Persistent state for pending drop (survives panel close/reopen)
  const [pendingDrop, setPendingDrop] = usePersistentThingState<ThingDropInfo>(handle, 'pending');

  // UI state for drag feedback
  const [isDragOver, setIsDragOver] = useState(false);
  const [isValidDrag, setIsValidDrag] = useState(false);

  // Trigger state
  const [isTriggering, setIsTriggering] = useState(false);
  const [triggerResult, setTriggerResult] = useState<TriggerResult | null>(null);

  // Get drop region helpers
  const {
    ThingDropRegion,
    mimeTypeForAnyThing,
    mimeTypeForThingType,
    mimeTypeForSpecificThing,
    extractDroppedThing,
  } = useThingDropRegion(handle);

  // Parse filter mode
  const filterMode = data?.filter_mode || 'any';
  const filterType = filterMode.startsWith('type:') ? filterMode.slice(5) : null;
  const filterKey = filterMode.startsWith('key:') ? filterMode.slice(4) : null;

  // Check if a drag is acceptable based on filter
  const checkDragAcceptable = useCallback(
    (dataTransfer: DataTransfer): boolean => {
      const types = Array.from(dataTransfer.types);
      console.log('[DropRegion] checkDragAcceptable, types:', types);
      console.log('[DropRegion] filterType:', filterType, 'filterKey:', filterKey, 'filterMode:', filterMode);

      // Check if it's a thing at all
      if (!dataTransfer.types.includes(mimeTypeForAnyThing)) {
        console.log('[DropRegion] Not a thing drag');
        return false;
      }

      // Apply type filter
      if (filterType) {
        const expectedMime = mimeTypeForThingType(filterType);
        const hasType = dataTransfer.types.includes(expectedMime);
        console.log('[DropRegion] Checking for type mime:', expectedMime, 'found:', hasType);
        return hasType;
      }

      // Apply key filter
      if (filterKey) {
        const expectedMime = mimeTypeForSpecificThing(filterKey);
        const hasKey = dataTransfer.types.includes(expectedMime);
        console.log('[DropRegion] Checking for key mime:', expectedMime, 'found:', hasKey);
        return hasKey;
      }

      // "any" mode accepts all things
      console.log('[DropRegion] Any mode, accepting');
      return true;
    },
    [mimeTypeForAnyThing, mimeTypeForThingType, mimeTypeForSpecificThing, filterType, filterKey, filterMode]
  );

  // Drag event handlers
  const handleDragOver = useCallback(
    (e: React.DragEvent) => {
      console.log('[DropRegion] handleDragOver called, types:', Array.from(e.dataTransfer.types));
      const acceptable = checkDragAcceptable(e.dataTransfer);
      console.log('[DropRegion] acceptable:', acceptable);
      setIsDragOver(true);
      setIsValidDrag(acceptable);

      if (acceptable) {
        e.preventDefault(); // Allow drop
        e.dataTransfer.dropEffect = 'copy';
      }
    },
    [checkDragAcceptable]
  );

  const handleDragLeave = useCallback(() => {
    setIsDragOver(false);
    setIsValidDrag(false);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);
      setIsValidDrag(false);

      const dropInfo = extractDroppedThing(e.dataTransfer);
      if (dropInfo && checkDragAcceptable(e.dataTransfer)) {
        setPendingDrop(dropInfo);
        setTriggerResult(null);
      }
    },
    [extractDroppedThing, checkDragAcceptable, setPendingDrop]
  );

  // Confirm drop - trigger with the thing key
  const handleConfirm = useCallback(async () => {
    if (!pendingDrop) return;

    setIsTriggering(true);
    setTriggerResult(null);

    const result = await trigger({
      thingKey: pendingDrop.key,
      thingType: pendingDrop.thingType,
    });

    setIsTriggering(false);
    setTriggerResult(result);

    if (result.ok) {
      // Clear pending drop on success
      setPendingDrop(undefined);
    }
  }, [pendingDrop, trigger, setPendingDrop]);

  // Clear pending drop
  const handleClear = useCallback(() => {
    setPendingDrop(undefined);
    setTriggerResult(null);
  }, [setPendingDrop]);

  // Compute border style based on drag state
  let borderColor = '#333';
  let borderStyle = 'dashed';
  if (isDragOver) {
    if (isValidDrag) {
      borderColor = '#22c55e'; // Green for valid
    } else {
      borderColor = '#ef4444'; // Red for invalid
    }
    borderStyle = 'solid';
  }

  return (
    <div style={{ padding: '16px' }}>
      {/* Header */}
      <div style={{ marginBottom: '12px' }}>
        <div style={{ fontSize: '16px', fontWeight: 'bold', color: '#3498db' }}>
          Drop Region: {data?.name}
        </div>
        <div style={{ fontSize: '12px', color: '#888', marginTop: '4px' }}>
          Filter: {filterMode === 'any' ? 'Any thing' : filterType ? `${filterType} only` : 'Specific thing only'}
          {' \u2022 '}#{shortId}
        </div>
      </div>

      {/* Drop Zone */}
      <ThingDropRegion>
        <div
          style={{
            border: `2px ${borderStyle} ${borderColor}`,
            borderRadius: '8px',
            padding: '24px',
            textAlign: 'center',
            background: isDragOver ? (isValidDrag ? 'rgba(34, 197, 94, 0.1)' : 'rgba(239, 68, 68, 0.1)') : '#1a1a2e',
            transition: 'all 0.15s ease',
            minHeight: '100px',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            alignItems: 'center',
            gap: '12px',
          }}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          {pendingDrop ? (
            <>
              <div style={{ color: '#888', fontSize: '12px' }}>Pending:</div>
              <NodeRef thingKey={pendingDrop.key} />
              <div style={{ display: 'flex', gap: '8px', marginTop: '8px' }}>
                <button
                  type="button"
                  onClick={handleConfirm}
                  disabled={!triggerable || isTriggering}
                  style={{
                    padding: '8px 16px',
                    background: triggerable ? '#22c55e' : '#444',
                    border: 'none',
                    borderRadius: '4px',
                    color: '#fff',
                    cursor: triggerable && !isTriggering ? 'pointer' : 'not-allowed',
                    opacity: isTriggering ? 0.6 : 1,
                  }}
                >
                  {isTriggering ? 'Confirming...' : 'Confirm'}
                </button>
                <button
                  type="button"
                  onClick={handleClear}
                  style={{
                    padding: '8px 16px',
                    background: '#666',
                    border: 'none',
                    borderRadius: '4px',
                    color: '#fff',
                    cursor: 'pointer',
                  }}
                >
                  Clear
                </button>
              </div>
              {!triggerable && (
                <div style={{ fontSize: '11px', color: '#888' }}>
                  (Not triggerable at this tick)
                </div>
              )}
            </>
          ) : (
            <>
              <div style={{ fontSize: '24px' }}>{'\u{1F4E5}'}</div>
              <div style={{ color: isDragOver ? (isValidDrag ? '#22c55e' : '#ef4444') : '#888' }}>
                {isDragOver
                  ? isValidDrag
                    ? 'Drop to add!'
                    : filterType
                      ? `Only accepts ${filterType}`
                      : 'Only accepts a specific thing'
                  : 'Drag a thing here'}
              </div>
            </>
          )}
        </div>
      </ThingDropRegion>

      {/* Trigger result */}
      {triggerResult && (
        <div
          style={{
            marginTop: '12px',
            padding: '8px',
            background: triggerResult.ok ? 'rgba(34, 197, 94, 0.2)' : 'rgba(239, 68, 68, 0.2)',
            border: `1px solid ${triggerResult.ok ? '#22c55e' : '#ef4444'}`,
            borderRadius: '4px',
            fontSize: '12px',
            color: triggerResult.ok ? '#22c55e' : '#ef4444',
          }}
        >
          {triggerResult.ok ? 'Confirmed!' : triggerResult.error || 'Error'}
        </div>
      )}

      {/* Confirmed links */}
      {links.length > 0 && (
        <div style={{ marginTop: '16px' }}>
          <div style={{ fontSize: '14px', fontWeight: '600', marginBottom: '8px' }}>
            Confirmed ({links.length}):
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {links.map((link) => (
              <NodeRef key={link.linkKey} thingKey={link.toThingKey} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function DropRegionRef({ handle }: RefRendererProps) {
  const data = usePayload<DropRegionPayload>(handle);
  const links = useLinks(handle);

  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '4px 10px',
        background: '#3498db',
        borderRadius: '4px',
        color: '#fff',
        fontSize: '13px',
        fontWeight: 600,
      }}
    >
      <span>{'\u{1F4E5}'}</span>
      <span>{data?.name}</span>
      {links.length > 0 && (
        <span style={{ opacity: 0.7 }}>({links.length})</span>
      )}
    </div>
  );
}

export default {
  dialect: '0.1',
  renderers: {
    DropRegion: {
      full: DropRegionFull,
      ref: DropRegionRef,
    },
  },
} as ThingWatcherRenderConfig;
