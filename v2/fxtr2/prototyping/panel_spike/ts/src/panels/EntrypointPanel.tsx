/**
 * EntrypointPanel - Shows links to top-level entities (collections).
 *
 * Clicking an entity opens it in a new panel via viewRegistry.openEntity().
 */

import React from 'react';
import type { IDockviewPanelProps } from 'dockview';
import type { DataBundle } from '../data/ownership';
import type { ViewRegistry } from '../render-manager/ViewRegistry';
import { getTopLevelEntities, getData } from '../data/ownership';

export interface EntrypointPanelParams {
  data: DataBundle;
  viewRegistry: ViewRegistry;
}

/**
 * Panel that displays links to top-level entities.
 */
export function EntrypointPanel({
  params,
}: IDockviewPanelProps<EntrypointPanelParams>) {
  const { data, viewRegistry } = params;
  const topLevel = getTopLevelEntities(data);

  return (
    <div style={{ padding: 16, height: '100%', overflow: 'auto' }}>
      <h3 style={{ marginTop: 0 }}>Entrypoint</h3>
      <p style={{ color: '#888', marginBottom: 12 }}>
        Click an entity to open it:
      </p>
      <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
        {topLevel.map((id) => {
          const entity = getData(data, id);
          return (
            <li
              key={id}
              onClick={() => viewRegistry.openEntity(id)}
              style={{
                padding: '8px 12px',
                marginBottom: 4,
                background: '#3d3d3d',
                borderRadius: 4,
                cursor: 'pointer',
                transition: 'background 0.1s',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = '#4d4d4d';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = '#3d3d3d';
              }}
            >
              <code style={{ color: '#58a6ff' }}>{id}</code>
              <span style={{ color: '#888', marginLeft: 8 }}>
                ({entity?.type})
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
