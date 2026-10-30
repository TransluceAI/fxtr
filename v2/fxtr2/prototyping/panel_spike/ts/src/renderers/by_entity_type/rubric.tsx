/**
 * Renderers for Rubric entities.
 */

import React from 'react';
import type { FullRendererProps, RefRendererProps, InlineRefRendererProps, PreviewRendererProps } from '../types';
import { getTypeColor, truncateEnd } from '../helpers';
import type { Rubric } from '../../data/schema';
import { DraggableID } from '../../render-manager/DraggableID';
import { useEntityData } from '../../render-manager/contexts';

const TYPE_COLOR = getTypeColor('rubric');

/**
 * Full renderer for Rubric entities.
 */
export function RubricFull({ entityId }: FullRendererProps) {
  const entity = useEntityData(entityId) as Rubric | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Rubric not found: {entityId}</div>;
  }

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
        rubric: <DraggableID entityId={entityId} />
      </div>
      <div style={{ color: '#ccc', fontWeight: 'bold', marginBottom: 4 }}>
        {entity.name}
      </div>
      <div style={{ color: '#888' }}>{entity.description}</div>
    </div>
  );
}

/**
 * Block ref renderer for Rubric entities.
 * Shows full ID and name, expands to fill available space.
 */
export function RubricRef({ entityId, isOwned }: RefRendererProps) {
  const entity = useEntityData(entityId) as Rubric | undefined;

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
        Rubric {entityId}
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
          {entity.name}
        </span>
      )}
    </div>
  );
}

/**
 * Inline ref renderer for Rubric entities.
 * Minimal display with just ID and type styling.
 */
export function RubricInline({ entityId, isOwned }: InlineRefRendererProps) {
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
 * Preview renderer for Rubric entities.
 * Compact preview for hover cards.
 */
export function RubricPreview({ entityId }: PreviewRendererProps) {
  const entity = useEntityData(entityId) as Rubric | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Rubric not found: {entityId}</div>;
  }

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
        rubric: <DraggableID entityId={entityId} />
      </div>
      <div style={{ color: '#ccc', fontWeight: 'bold', marginBottom: 4, fontSize: '0.85em' }}>
        {entity.name}
      </div>
      <div style={{ color: '#888', fontSize: '0.85em' }}>
        {truncateEnd(entity.description, 100)}
      </div>
    </div>
  );
}

export const rubric = { full: RubricFull, ref: RubricRef, inline: RubricInline, preview: RubricPreview };
