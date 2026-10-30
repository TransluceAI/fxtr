/**
 * Renderers for Judgement entities.
 */

import React from 'react';
import type { FullRendererProps, RefRendererProps, InlineRefRendererProps, PreviewRendererProps } from '../types';
import { getTypeColor, truncateEnd } from '../helpers';
import type { Judgement } from '../../data/schema';
import { DraggableID } from '../../render-manager/DraggableID';
import { useEntityData } from '../../render-manager/contexts';
import { RefLink } from '../../render-manager/EntityRenderer';

const TYPE_COLOR = getTypeColor('judgement');

/**
 * Full renderer for Judgement entities.
 */
export function JudgementFull({ entityId }: FullRendererProps) {
  const entity = useEntityData(entityId) as Judgement | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Judgement not found: {entityId}</div>;
  }

  const scoreColor = entity.score >= 0.7 ? '#7ee787' : entity.score >= 0.4 ? '#fa0' : '#f88';

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
        judgement: <DraggableID entityId={entityId} />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
        <div style={{ flexShrink: 0 }}>
          <span style={{ color: '#888' }}>Score:</span>{' '}
          <span style={{ color: scoreColor, fontWeight: 'bold' }}>
            {entity.score.toFixed(2)}
          </span>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <RefLink entityId={entity.rubric} />
        </div>
      </div>
      <div style={{ marginBottom: 8 }}>
        <RefLink entityId={entity.turn} />
      </div>
      <div style={{ color: '#ccc', fontStyle: 'italic' }}>
        "{entity.explanation}"
      </div>
    </div>
  );
}

/**
 * Block ref renderer for Judgement entities.
 * Shows full ID, score, and explanation preview.
 */
export function JudgementRef({ entityId, isOwned }: RefRendererProps) {
  const entity = useEntityData(entityId) as Judgement | undefined;
  const scoreColor = entity
    ? entity.score >= 0.7 ? '#7ee787' : entity.score >= 0.4 ? '#fa0' : '#f88'
    : '#888';

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
        Judgement {entityId}
      </span>
      {entity && (
        <>
          <span
            style={{
              color: scoreColor,
              flexShrink: 0,
              textDecoration: isOwned ? 'none' : 'underline',
            }}
          >
            {entity.score.toFixed(2)}
          </span>
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
            {entity.explanation}
          </span>
        </>
      )}
    </div>
  );
}

/**
 * Inline ref renderer for Judgement entities.
 * Minimal display with just ID and type styling.
 */
export function JudgementInline({ entityId, isOwned }: InlineRefRendererProps) {
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
 * Preview renderer for Judgement entities.
 * Compact preview for hover cards.
 */
export function JudgementPreview({ entityId }: PreviewRendererProps) {
  const entity = useEntityData(entityId) as Judgement | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Judgement not found: {entityId}</div>;
  }

  const scoreColor = entity.score >= 0.7 ? '#7ee787' : entity.score >= 0.4 ? '#fa0' : '#f88';

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
        judgement: <DraggableID entityId={entityId} />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <span style={{ color: '#888', fontSize: '0.85em' }}>Score:</span>
        <span style={{ color: scoreColor, fontWeight: 'bold', fontSize: '0.85em' }}>
          {entity.score.toFixed(2)}
        </span>
      </div>
      <div style={{ marginBottom: 4 }}>
        <RefLink entityId={entity.rubric} />
      </div>
      <div style={{ marginBottom: 4 }}>
        <RefLink entityId={entity.turn} />
      </div>
      <div style={{ color: '#ccc', fontStyle: 'italic', fontSize: '0.85em' }}>
        "{truncateEnd(entity.explanation, 80)}"
      </div>
    </div>
  );
}

export const judgement = { full: JudgementFull, ref: JudgementRef, inline: JudgementInline, preview: JudgementPreview };
