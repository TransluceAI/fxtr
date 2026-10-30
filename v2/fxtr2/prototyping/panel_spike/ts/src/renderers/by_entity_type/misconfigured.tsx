/**
 * Renderers for MisconfiguredObject entities.
 *
 * NOTE: This entity type is intentionally misconfigured for testing edge cases.
 * The renderer treats ownedTurns as refs and refJudgements as owned,
 * to test mismatch detection.
 */

import React from 'react';
import type { FullRendererProps, RefRendererProps, InlineRefRendererProps, PreviewRendererProps } from '../types';
import { getTypeColor } from '../helpers';
import type { MisconfiguredObject } from '../../data/schema';
import { Child } from '../../render-manager/Child';
import { DraggableID } from '../../render-manager/DraggableID';
import { useEntityData } from '../../render-manager/contexts';
import { RefLink } from '../../render-manager/EntityRenderer';

const TYPE_COLOR = getTypeColor('misconfigured');

/**
 * Full renderer for MisconfiguredObject entities.
 */
export function MisconfiguredFull({ entityId }: FullRendererProps) {
  const entity = useEntityData(entityId) as MisconfiguredObject | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>MisconfiguredObject not found: {entityId}</div>;
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
        misconfigured: <DraggableID entityId={entityId} />
      </div>

      <div
        style={{
          color: '#f88',
          marginBottom: 8,
          padding: 4,
          background: '#400',
          borderRadius: 4,
        }}
      >
        This entity is intentionally misconfigured for testing
      </div>

      <div style={{ marginBottom: 8 }}>
        <div style={{ color: '#888', marginBottom: 4 }}>
          refJudgements (rendered as owned - WRONG):
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {entity.refJudgements.map((id) => (
            <Child key={id} entityId={id} />
          ))}
        </div>
      </div>

      <div>
        <div style={{ color: '#888', marginBottom: 4 }}>
          ownedTurns (rendered as refs - WRONG):
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {entity.ownedTurns.map((id) => (
            <RefLink key={id} entityId={id} />
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * Block ref renderer for MisconfiguredObject entities.
 * Shows full ID and misconfigured warning, expands to fill available space.
 */
export function MisconfiguredRef({ entityId, isOwned }: RefRendererProps) {
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
        Misconfigured {entityId}
      </span>
      <span
        style={{
          color: '#f88',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          flex: 1,
          textDecoration: isOwned ? 'none' : 'underline',
        }}
      >
        (misconfigured)
      </span>
    </div>
  );
}

/**
 * Inline ref renderer for MisconfiguredObject entities.
 * Minimal display with just ID and type styling.
 */
export function MisconfiguredInline({ entityId, isOwned }: InlineRefRendererProps) {
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
 * Preview renderer for MisconfiguredObject entities.
 * Compact preview for hover cards.
 */
export function MisconfiguredPreview({ entityId }: PreviewRendererProps) {
  const entity = useEntityData(entityId) as MisconfiguredObject | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>MisconfiguredObject not found: {entityId}</div>;
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
        misconfigured: <DraggableID entityId={entityId} />
      </div>
      <div
        style={{
          color: '#f88',
          marginBottom: 4,
          padding: 4,
          background: '#400',
          borderRadius: 4,
          fontSize: '0.8em',
        }}
      >
        Intentionally misconfigured
      </div>
      <div style={{ fontSize: '0.85em', color: '#888' }}>
        {entity.refJudgements.length} judgement refs, {entity.ownedTurns.length} owned turns
      </div>
    </div>
  );
}

export const misconfigured = { full: MisconfiguredFull, ref: MisconfiguredRef, inline: MisconfiguredInline, preview: MisconfiguredPreview };
