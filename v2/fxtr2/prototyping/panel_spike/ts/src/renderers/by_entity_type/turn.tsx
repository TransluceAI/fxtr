/**
 * Renderers for Turn entities.
 */

import React from 'react';
import type { FullRendererProps, RefRendererProps, InlineRefRendererProps, PreviewRendererProps } from '../types';
import { getTypeColor, truncateEnd } from '../helpers';
import type { Turn } from '../../data/schema';
import { DraggableID } from '../../render-manager/DraggableID';
import { useEntityData } from '../../render-manager/contexts';
import { InlineRefLink, RefLink } from '../../render-manager/EntityRenderer';

const TYPE_COLOR = getTypeColor('turn');

/**
 * Full renderer for Turn entities.
 * Accepts customArgs.showPreviousTurnLink - if false, hides previous turn link.
 */
export function TurnFull({ entityId, customArgs }: FullRendererProps) {
  const entity = useEntityData(entityId) as Turn | undefined;
  const showPreviousTurnLink = customArgs?.showPreviousTurnLink !== false;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Turn not found: {entityId}</div>;
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
        turn: <DraggableID entityId={entityId} />
      </div>
      <div
        style={{
          color: entity.role === 'user' ? '#58a6ff' : '#7ee787',
          marginBottom: 4,
          fontWeight: 'bold',
          textTransform: 'capitalize',
        }}
      >
        {entity.role}
      </div>
      <div
        style={{
          color: '#ccc',
          whiteSpace: 'pre-wrap',
          fontFamily: 'inherit',
          marginBottom: entity.previousTurn && showPreviousTurnLink ? 8 : 0,
        }}
      >
        {entity.message}
      </div>
      {entity.previousTurn && showPreviousTurnLink && (
        <div style={{ color: '#888', fontSize: '0.9em' }}>
          ↩ previous: <InlineRefLink entityId={entity.previousTurn} />
        </div>
      )}
    </div>
  );
}

/**
 * Block ref renderer for Turn entities.
 * Shows full ID, role, and message preview.
 */
export function TurnRef({ entityId, isOwned }: RefRendererProps) {
  const entity = useEntityData(entityId) as Turn | undefined;

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
        Turn {entityId}
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
          [{entity.role}] {entity.message}
        </span>
      )}
    </div>
  );
}

/**
 * Inline ref renderer for Turn entities.
 * Minimal display with just ID and type styling.
 */
export function TurnInline({ entityId, isOwned }: InlineRefRendererProps) {
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
 * Preview renderer for Turn entities.
 * Compact preview for hover cards.
 */
export function TurnPreview({ entityId }: PreviewRendererProps) {
  const entity = useEntityData(entityId) as Turn | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Turn not found: {entityId}</div>;
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
        turn: <DraggableID entityId={entityId} />
      </div>
      <div
        style={{
          color: entity.role === 'user' ? '#58a6ff' : '#7ee787',
          marginBottom: 4,
          fontWeight: 'bold',
          textTransform: 'capitalize',
          fontSize: '0.85em',
        }}
      >
        {entity.role}
      </div>
      <div
        style={{
          color: '#ccc',
          whiteSpace: 'pre-wrap',
          fontFamily: 'inherit',
          fontSize: '0.85em',
          marginBottom: entity.previousTurn ? 4 : 0,
        }}
      >
        {truncateEnd(entity.message, 100)}
      </div>
      {entity.previousTurn && (
        <div style={{ color: '#888', fontSize: '0.8em' }}>
          ↩ previous: <RefLink entityId={entity.previousTurn} />
        </div>
      )}
    </div>
  );
}

export const turn = { full: TurnFull, ref: TurnRef, inline: TurnInline, preview: TurnPreview };
