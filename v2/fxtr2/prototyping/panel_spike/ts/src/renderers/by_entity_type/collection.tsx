/**
 * Renderers for Collection entities.
 */

import React from 'react';
import type { FullRendererProps, RefRendererProps, InlineRefRendererProps, PreviewRendererProps } from '../types';
import { getTypeColor } from '../helpers';
import type { Collection } from '../../data/schema';
import { DraggableID } from '../../render-manager/DraggableID';
import { useEntityData } from '../../render-manager/contexts';
import { RefLink } from '../../render-manager/EntityRenderer';

const TYPE_COLOR = getTypeColor('collection');

/**
 * Full renderer for Collection entities.
 */
export function CollectionFull({ entityId }: FullRendererProps) {
  const entity = useEntityData(entityId) as Collection | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Collection not found: {entityId}</div>;
  }

  const items = Object.entries(entity.items);

  return (
    <div
      style={{
        border: `2px solid ${TYPE_COLOR}`,
        borderRadius: 4,
        padding: 12,
        background: '#2a2a2a',
      }}
    >
      <div style={{ marginBottom: 8, color: TYPE_COLOR, fontWeight: 'bold' }}>
        collection: <DraggableID entityId={entityId} />
      </div>
      <div style={{ color: '#888', marginBottom: 8 }}>
        {items.length} item{items.length !== 1 ? 's' : ''}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {items.map(([key, itemEntityId]) => (
          <div key={key}>
            <div style={{ color: '#666', fontSize: '0.8em', marginBottom: 2 }}>
              {key}:
            </div>
            <RefLink entityId={itemEntityId} isOwned />
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * Block ref renderer for Collection entities.
 * Shows full ID and item count, expands to fill available space.
 */
export function CollectionRef({ entityId, isOwned }: RefRendererProps) {
  const entity = useEntityData(entityId) as Collection | undefined;
  const itemCount = entity ? Object.keys(entity.items).length : 0;

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '4px 8px',
        // background: '#2a2a2a',
        border: isOwned ? `1px solid ${TYPE_COLOR}` : 'none',
        borderRadius: 4,
        overflow: 'hidden',
      }}
    >
      <span
        style={{
          display: 'inline-block',
          width: 8,
          height: 8,
          backgroundColor: TYPE_COLOR,
          borderRadius: 2,
          flexShrink: 0,
        }}
      />
      <span
        style={{
          color: TYPE_COLOR,
          flexShrink: 0,
          textDecoration: isOwned ? 'none' : 'underline',
        }}
      >
        Collection {entityId}
      </span>
      {entity && (
        <span
          style={{
            color: '#888',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            flex: 1,
            textDecoration: isOwned ? 'none' : 'underline',
          }}
        >
          ({itemCount} item{itemCount !== 1 ? 's' : ''})
        </span>
      )}
    </div>
  );
}

/**
 * Inline ref renderer for Collection entities.
 * Minimal display with just ID and type styling.
 */
export function CollectionInline({ entityId, isOwned }: InlineRefRendererProps) {
  return (
    <span
      style={{
        color: TYPE_COLOR,
        textDecoration: isOwned ? 'none' : 'underline',
        borderBottom: isOwned ? `1px solid ${TYPE_COLOR}` : 'none',
      }}
    >
      <span
        style={{
          display: 'inline-block',
          width: 6,
          height: 6,
          backgroundColor: TYPE_COLOR,
          borderRadius: 2,
          marginRight: 4,
        }}
      />
      {entityId}
    </span>
  );
}

/**
 * Preview renderer for Collection entities.
 * Compact preview for hover cards. Items shown as RefLinks.
 */
export function CollectionPreview({ entityId }: PreviewRendererProps) {
  const entity = useEntityData(entityId) as Collection | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Collection not found: {entityId}</div>;
  }

  const items = Object.entries(entity.items);

  return (
    <div
      style={{
        border: `1px solid ${TYPE_COLOR}`,
        borderRadius: 4,
        padding: 8,
        background: '#2a2a2a',
      }}
    >
      <div style={{ marginBottom: 4, color: TYPE_COLOR, fontWeight: 'bold', fontSize: '0.9em' }}>
        collection: <DraggableID entityId={entityId} />
      </div>
      <div style={{ color: '#888', marginBottom: 4, fontSize: '0.85em' }}>
        {items.length} item{items.length !== 1 ? 's' : ''}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {items.map(([key, itemEntityId]) => (
          <div key={key}>
            <span style={{ color: '#666', fontSize: '0.8em' }}>{key}: </span>
            <RefLink entityId={itemEntityId} isOwned />
          </div>
        ))}
      </div>
    </div>
  );
}

export const collection = { full: CollectionFull, ref: CollectionRef, inline: CollectionInline, preview: CollectionPreview };
