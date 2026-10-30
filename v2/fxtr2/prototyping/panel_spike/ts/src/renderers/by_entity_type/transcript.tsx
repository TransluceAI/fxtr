/**
 * Renderers for Transcript entities.
 */

import React from 'react';
import type { FullRendererProps, RefRendererProps, InlineRefRendererProps, PreviewRendererProps } from '../types';
import { getTypeColor } from '../helpers';
import type { Transcript } from '../../data/schema';
import { Child } from '../../render-manager/Child';
import { DraggableID } from '../../render-manager/DraggableID';
import { useEntityData } from '../../render-manager/contexts';
import { RefLink } from '../../render-manager/EntityRenderer';

const TYPE_COLOR = getTypeColor('transcript');

/**
 * Full renderer for Transcript entities.
 * Passes showPreviousTurnLink: false to child turns to hide redundant previous turn links.
 */
export function TranscriptFull({ entityId }: FullRendererProps) {
  const entity = useEntityData(entityId) as Transcript | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Transcript not found: {entityId}</div>;
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
        transcript: <DraggableID entityId={entityId} />
      </div>
      <div style={{ color: '#888', marginBottom: 8 }}>
        Topic: <span style={{ color: '#ccc' }}>{entity.topic}</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {entity.turns.map((turnId) => (
          <Child key={turnId} entityId={turnId} customArgs={{ showPreviousTurnLink: false }} />
        ))}
      </div>
    </div>
  );
}

/**
 * Block ref renderer for Transcript entities.
 * Shows full ID and topic, expands to fill available space.
 */
export function TranscriptRef({ entityId, isOwned }: RefRendererProps) {
  const entity = useEntityData(entityId) as Transcript | undefined;

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
        Transcript {entityId}
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
          {entity.topic}
        </span>
      )}
    </div>
  );
}

/**
 * Inline ref renderer for Transcript entities.
 * Minimal display with just ID and type styling.
 */
export function TranscriptInline({ entityId, isOwned }: InlineRefRendererProps) {
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
 * Preview renderer for Transcript entities.
 * Compact preview for hover cards. Children shown as RefLinks.
 */
export function TranscriptPreview({ entityId }: PreviewRendererProps) {
  const entity = useEntityData(entityId) as Transcript | undefined;

  if (!entity) {
    return <div style={{ color: '#f88' }}>Transcript not found: {entityId}</div>;
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
        transcript: <DraggableID entityId={entityId} />
      </div>
      <div style={{ color: '#888', marginBottom: 4, fontSize: '0.85em' }}>
        Topic: <span style={{ color: '#ccc' }}>{entity.topic}</span>
      </div>
      <div style={{ color: '#888', fontSize: '0.85em', marginBottom: 4 }}>
        Turns ({entity.turns.length}):
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {entity.turns.map((turnId) => (
          <RefLink key={turnId} entityId={turnId} isOwned />
        ))}
      </div>
    </div>
  );
}

export const transcript = { full: TranscriptFull, ref: TranscriptRef, inline: TranscriptInline, preview: TranscriptPreview };
