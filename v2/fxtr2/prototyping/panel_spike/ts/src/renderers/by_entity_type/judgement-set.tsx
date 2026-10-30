/**
 * Renderers for JudgementSet entities.
 */

import React from 'react';
import type { FullRendererProps, RefRendererProps, InlineRefRendererProps, PreviewRendererProps } from '../types';
import { getTypeColor } from '../helpers';
import type { JudgementSet } from '../../data/schema';
import { Child } from '../../render-manager/Child';
import { DraggableID } from '../../render-manager/DraggableID';
import { useEntityData } from '../../render-manager/contexts';
import { RefLink } from '../../render-manager/EntityRenderer';

const TYPE_COLOR = getTypeColor('judgement_set');

/**
 * Full renderer for JudgementSet entities.
 */
export function JudgementSetFull({ entityId }: FullRendererProps) {
  const entity = useEntityData(entityId) as JudgementSet | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>JudgementSet not found: {entityId}</div>;
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
        judgement_set: <DraggableID entityId={entityId} />
      </div>
      <div style={{ color: '#888', marginBottom: 8 }}>
        {entity.judgements.length} judgement
        {entity.judgements.length !== 1 ? 's' : ''}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {entity.judgements.map((judgementId) => (
          <Child key={judgementId} entityId={judgementId} />
        ))}
      </div>
    </div>
  );
}

/**
 * Block ref renderer for JudgementSet entities.
 * Shows full ID and judgement count.
 */
export function JudgementSetRef({ entityId, isOwned }: RefRendererProps) {
  const entity = useEntityData(entityId) as JudgementSet | undefined;
  const count = entity?.judgements.length ?? 0;

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
        JudgementSet {entityId}
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
          ({count} judgement{count !== 1 ? 's' : ''})
        </span>
      )}
    </div>
  );
}

/**
 * Inline ref renderer for JudgementSet entities.
 * Minimal display with just ID and type styling.
 */
export function JudgementSetInline({ entityId, isOwned }: InlineRefRendererProps) {
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
 * Preview renderer for JudgementSet entities.
 * Compact preview for hover cards. Judgements shown as RefLinks.
 */
export function JudgementSetPreview({ entityId }: PreviewRendererProps) {
  const entity = useEntityData(entityId) as JudgementSet | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>JudgementSet not found: {entityId}</div>;
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
        judgement_set: <DraggableID entityId={entityId} />
      </div>
      <div style={{ color: '#888', marginBottom: 4, fontSize: '0.85em' }}>
        {entity.judgements.length} judgement{entity.judgements.length !== 1 ? 's' : ''}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {entity.judgements.map((judgementId) => (
          <RefLink key={judgementId} entityId={judgementId} isOwned />
        ))}
      </div>
    </div>
  );
}

export const judgementSet = { full: JudgementSetFull, ref: JudgementSetRef, inline: JudgementSetInline, preview: JudgementSetPreview };
