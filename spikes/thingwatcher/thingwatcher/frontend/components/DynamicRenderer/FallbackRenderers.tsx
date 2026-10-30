/**
 * Fallback renderers used when custom renderers don't export a variant.
 *
 * These use the same handle/hooks pattern as custom renderers:
 * - Receive an opaque handle
 * - Use hooks (usePayload, useLinks, useThingMetadata, etc.) to access data
 */

import React, { useState, useCallback } from 'react';
import type {
  FullRendererHandle,
  RefRendererHandle,
  PreviewRendererHandle,
} from '../../lib/handleFactory';
import {
  usePayload,
  useLinks,
  useTriggerable,
  useTrigger,
  useDraggableIdMarker,
  useThingMetadata,
} from '../../lib/rendererHooks';
import { InlineNodeRef, NodeRef } from '../../lib/ThingRenderLib';

// =============================================================================
// Fallback Full Renderer
// =============================================================================

interface FallbackFullRendererProps {
  handle: FullRendererHandle;
  args: Record<string, unknown>;
  isTopLevel: boolean;
}

/**
 * Fallback full renderer - shows JSON tree of data, links list.
 * Used when a custom renderer doesn't export FullRenderer.
 */
export function FallbackFullRenderer({
  handle,
}: FallbackFullRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const { shortId, keyJSON, thingType } = useThingMetadata(handle);
  const [triggerable] = useTriggerable(handle);
  const trigger = useTrigger(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const [triggerPayload, setTriggerPayload] = useState('');
  const [triggering, setTriggering] = useState(false);
  const [triggerResult, setTriggerResult] = useState<{ ok: boolean; error?: string } | null>(null);

  const handleTrigger = useCallback(async () => {
    let parsed: unknown = null;
    if (triggerPayload.trim()) {
      try {
        parsed = JSON.parse(triggerPayload);
      } catch {
        setTriggerResult({ ok: false, error: 'Invalid JSON payload' });
        return;
      }
    }
    setTriggering(true);
    const result = await trigger(parsed);
    setTriggerResult(result);
    setTriggering(false);
  }, [triggerPayload, trigger]);

  return (
    <div style={{ padding: 8, background: '#1a1a1a', borderRadius: 4 }}>
      {/* Header */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
        <span style={{ background: '#333', padding: '2px 6px', borderRadius: 3, fontSize: 12 }}>
          {thingType}
        </span>
        <DraggableIDMarker>
          <span style={{ color: '#888', fontSize: 12, cursor: 'grab' }}>#{shortId}</span>
        </DraggableIDMarker>
      </div>

      {/* Key */}
      {keyJSON !== null && keyJSON !== undefined && (
        <div style={{ color: '#666', fontSize: 11, marginBottom: 8 }}>
          Key: {JSON.stringify(keyJSON)}
        </div>
      )}

      {/* Trigger form */}
      {triggerable && (
        <div style={{ marginBottom: 8, padding: 8, background: '#222', borderRadius: 4 }}>
          <div style={{ color: '#888', fontSize: 11, marginBottom: 4 }}>Trigger</div>
          <textarea
            value={triggerPayload}
            onChange={(e) => setTriggerPayload(e.target.value)}
            placeholder="JSON payload (optional)"
            style={{
              width: '100%',
              minHeight: 40,
              background: '#333',
              border: '1px solid #444',
              borderRadius: 3,
              color: '#ccc',
              fontSize: 12,
              padding: 4,
              resize: 'vertical',
            }}
          />
          <button
            onClick={handleTrigger}
            disabled={triggering}
            style={{
              marginTop: 4,
              padding: '4px 8px',
              background: '#2a4a6a',
              border: 'none',
              borderRadius: 3,
              color: '#8cf',
              cursor: triggering ? 'wait' : 'pointer',
            }}
          >
            {triggering ? 'Triggering...' : 'Trigger'}
          </button>
          {triggerResult && (
            <div style={{ marginTop: 4, color: triggerResult.ok ? '#7c7' : '#f77', fontSize: 11 }}>
              {triggerResult.ok ? 'Triggered successfully' : triggerResult.error}
            </div>
          )}
        </div>
      )}

      {/* Data */}
      <div style={{ marginBottom: 8 }}>
        <div style={{ color: '#888', fontSize: 11, marginBottom: 4 }}>Data</div>
        <pre style={{ margin: 0, fontSize: 11, color: '#aaa', overflow: 'auto', maxHeight: 200 }}>
          {JSON.stringify(payload, null, 2)}
        </pre>
      </div>

      {/* Links */}
      {links.length > 0 && (
        <div>
          <div style={{ color: '#888', fontSize: 11, marginBottom: 4 }}>Links ({links.length})</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {links.map((link, i) => (
              <div key={i} style={{ fontSize: 11, color: '#8cf' }}>
                {link.linkType} → <NodeRef thingKey={link.toThingKey} />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Fallback Ref Renderer
// =============================================================================

interface FallbackRefRendererProps {
  handle: RefRendererHandle;
  args: Record<string, unknown>;
}

/**
 * Fallback ref renderer - simple type badge + keyJSON.
 * Used when a custom renderer doesn't export RefRenderer.
 * Returns a block element (div) that fills width.
 */
export function FallbackRefRenderer({
  handle,
}: FallbackRefRendererProps) {
  const { shortId, keyJSON, thingType } = useThingMetadata(handle);

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        padding: '2px 8px',
        background: '#2a4a6a',
        borderRadius: 4,
        fontSize: 12,
        whiteSpace: 'nowrap',
        overflow: 'hidden',
      }}
    >
      <span style={{ background: '#333', padding: '1px 4px', borderRadius: 2, color: '#aaa', flexShrink: 0 }}>
        {thingType}
      </span>
      <span style={{
        color: '#8cf',
        minWidth: 0,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}>
        {keyJSON !== null && keyJSON !== undefined
          ? JSON.stringify(keyJSON)
          : `#${shortId}`}
      </span>
    </div>
  );
}

// =============================================================================
// Fallback Inline Ref Renderer
// =============================================================================

interface FallbackInlineRefRendererProps {
  handle: RefRendererHandle;
  args: Record<string, unknown>;
}

/**
 * Fallback inline ref renderer - minimal badge with type dot + shortId.
 * Used when a custom renderer doesn't export InlineRefRenderer.
 */
export function FallbackInlineRefRenderer({
  handle,
}: FallbackInlineRefRendererProps) {
  const { shortId } = useThingMetadata(handle);

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        color: '#8cf',
        fontSize: 12,
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: 2,
          backgroundColor: '#4a8',
        }}
      />
      #{shortId}
    </span>
  );
}

// =============================================================================
// Fallback Preview Renderer
// =============================================================================

interface FallbackPreviewRendererProps {
  handle: PreviewRendererHandle;
}

/**
 * Fallback preview renderer - shows type badge, keyJSON, and links.
 * Used when a custom renderer doesn't export PreviewRenderer.
 */
export function FallbackPreviewRenderer({
  handle,
}: FallbackPreviewRendererProps) {
  const links = useLinks(handle);
  const { shortId, keyJSON, thingType } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 280 }}>
      {/* Header: type + shortId (draggable together) + key */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <DraggableIDMarker>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'grab' }}>
            <span
              style={{
                background: '#444',
                padding: '2px 6px',
                borderRadius: 3,
                fontSize: 11,
                color: '#ccc',
              }}
            >
              {thingType}
            </span>
            <span style={{ color: '#888', fontSize: 11 }}>#{shortId}</span>
          </span>
        </DraggableIDMarker>
        <span style={{ color: '#8cf', fontSize: 11 }}>
          {keyJSON !== null && keyJSON !== undefined
            ? JSON.stringify(keyJSON)
            : '(binary key)'}
        </span>
      </div>

      {/* Links as InlineNodeRef */}
      {links.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <div style={{ color: '#888', fontSize: 10, marginBottom: 2 }}>
            Links ({links.length})
          </div>
          {links.slice(0, 5).map((link, i) => (
            <div
              key={i}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                fontSize: 11,
              }}
            >
              <span style={{ color: '#888', minWidth: 50 }}>{link.linkType}:</span>
              <InlineNodeRef thingKey={link.toThingKey} />
            </div>
          ))}
          {links.length > 5 && (
            <div style={{ color: '#666', fontSize: 10, fontStyle: 'italic' }}>
              +{links.length - 5} more
            </div>
          )}
        </div>
      )}
    </div>
  );
}
