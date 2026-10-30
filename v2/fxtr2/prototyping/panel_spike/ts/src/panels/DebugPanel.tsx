/**
 * DebugPanel - Shows entity relationships and ViewRegistry state.
 */

import React, { useSyncExternalStore } from 'react';
import type { IDockviewPanelProps } from 'dockview';
import type { DataBundle } from '../data/ownership';
import type { ViewRegistry } from '../render-manager/ViewRegistry';
import { getOwns, getRefs } from '../data/ownership';

export interface DebugPanelParams {
  data: DataBundle;
  viewRegistry: ViewRegistry;
}

/**
 * Debug panel - shows all objects, relationships, and render manager state.
 */
export function DebugPanel({ params }: IDockviewPanelProps<DebugPanelParams>) {
  const { data, viewRegistry } = params;

  // Subscribe to viewRegistry state
  const registryState = useSyncExternalStore(
    viewRegistry.subscribe,
    () => viewRegistry.getSnapshot()
  );

  // Sort entities: collections first, then by type, then by id
  const sortedEntities = Array.from(data.entities.entries()).sort(
    ([idA, a], [idB, b]) => {
      // Collections first
      if (a.type === 'collection' && b.type !== 'collection') return -1;
      if (a.type !== 'collection' && b.type === 'collection') return 1;
      // Then by type
      if (a.type !== b.type) return a.type.localeCompare(b.type);
      // Then by id
      return idA.localeCompare(idB);
    }
  );

  const views = Array.from(registryState.views.values());
  const slots = Array.from(registryState.slots.values());
  const panels = Array.from(registryState.panels.values());

  return (
    <div style={{ padding: 16, height: '100%', overflow: 'auto' }}>
      <h3 style={{ marginTop: 0 }}>Debug Panel</h3>

      {/* ViewRegistry State */}
      <div
        style={{
          marginBottom: 24,
          padding: 12,
          background: '#1a1a2e',
          borderRadius: 4,
          border: '1px solid #333',
        }}
      >
        <h4 style={{ marginTop: 0, color: '#8af' }}>ViewRegistry State</h4>

        <div style={{ marginBottom: 12 }}>
          <strong style={{ color: '#aaa' }}>
            Views ({views.length}):
          </strong>
          {views.length === 0 ? (
            <span style={{ color: '#666', marginLeft: 8 }}>(none)</span>
          ) : (
            <div style={{ marginLeft: 12, marginTop: 4 }}>
              {views.map((v) => (
                <div
                  key={v.viewId}
                  style={{
                    fontSize: 11,
                    color: '#888',
                    fontFamily: 'monospace',
                    marginBottom: 2,
                  }}
                >
                  <span style={{ color: '#58a6ff' }}>{v.viewId.slice(0, 16)}</span>
                  {' → '}
                  <span style={{ color: '#7ee787' }}>{v.entityId}</span>
                  {v.ownerSlotId && (
                    <span style={{ color: '#fa0' }}> [slot]</span>
                  )}
                  {v.panelId && (
                    <span style={{ color: '#f0a' }}> [panel]</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div style={{ marginBottom: 12 }}>
          <strong style={{ color: '#aaa' }}>
            Slots ({slots.length}):
          </strong>
          {slots.length === 0 ? (
            <span style={{ color: '#666', marginLeft: 8 }}>(none)</span>
          ) : (
            <div style={{ marginLeft: 12, marginTop: 4 }}>
              {slots.map((s) => (
                <div
                  key={s.slotId}
                  style={{
                    fontSize: 11,
                    color: '#888',
                    fontFamily: 'monospace',
                    marginBottom: 2,
                  }}
                >
                  <span style={{ color: '#fa0' }}>{s.slotId}</span>
                  {' → '}
                  <span style={{ color: '#7ee787' }}>{s.entityId}</span>
                  {' in '}
                  <span style={{ color: '#58a6ff' }}>{s.parentViewId.slice(0, 16)}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div>
          <strong style={{ color: '#aaa' }}>
            Panels ({panels.length}):
          </strong>
          {panels.length === 0 ? (
            <span style={{ color: '#666', marginLeft: 8 }}>(none)</span>
          ) : (
            <div style={{ marginLeft: 12, marginTop: 4 }}>
              {panels.map((p) => (
                <div
                  key={p.panelId}
                  style={{
                    fontSize: 11,
                    color: '#888',
                    fontFamily: 'monospace',
                    marginBottom: 2,
                  }}
                >
                  <span style={{ color: '#f0a' }}>{p.panelId}</span>
                  {' → '}
                  <span style={{ color: '#58a6ff' }}>{p.viewId.slice(0, 16)}</span>
                  {p.isPreview && (
                    <span style={{ color: '#ff8' }}> [preview]</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Entity Relationships */}
      <h4 style={{ color: '#aaa' }}>Entity Relationships</h4>
      <div style={{ fontFamily: 'monospace', fontSize: 12 }}>
        {sortedEntities.map(([id, entity]) => {
          const owns = getOwns(data, id);
          const refs = getRefs(data, id);

          return (
            <div
              key={id}
              style={{
                marginBottom: 12,
                padding: 8,
                background: '#2a2a2a',
                borderRadius: 4,
                borderLeft: `3px solid ${getTypeColor(entity.type)}`,
              }}
            >
              <div style={{ marginBottom: 4 }}>
                <span style={{ color: getTypeColor(entity.type), fontWeight: 'bold' }}>
                  {entity.type}
                </span>
                <span style={{ color: '#58a6ff', marginLeft: 8 }}>{id}</span>
                <span
                  onClick={() => viewRegistry.openEntity(id)}
                  style={{
                    marginLeft: 8,
                    color: '#8af',
                    cursor: 'pointer',
                    fontSize: '0.9em',
                    textDecoration: 'underline',
                  }}
                >
                  open
                </span>
              </div>

              {owns.length > 0 && (
                <div style={{ color: '#8b8', marginLeft: 12 }}>
                  owns: {owns.join(', ')}
                </div>
              )}

              {refs.length > 0 && (
                <div style={{ color: '#b88', marginLeft: 12 }}>
                  refs: {refs.join(', ')}
                </div>
              )}

              {owns.length === 0 && refs.length === 0 && (
                <div style={{ color: '#666', marginLeft: 12, fontStyle: 'italic' }}>
                  (no relationships)
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function getTypeColor(type: string): string {
  const colors: Record<string, string> = {
    collection: '#f0a',
    transcript: '#0af',
    turn: '#0fa',
    judgement_set: '#fa0',
    judgement: '#ff0',
    rubric: '#a0f',
    misconfigured: '#f00',
  };
  return colors[type] ?? '#888';
}
