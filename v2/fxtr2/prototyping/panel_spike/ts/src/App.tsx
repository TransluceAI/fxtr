/**
 * Root application component for the panel spike.
 */

import React, { useMemo } from 'react';
import { RenderRoot } from './render-manager/RenderManager';
import { createViewRegistry } from './render-manager/ViewRegistry';
import { ViewRegistryContext } from './render-manager/contexts';
import { exampleEntities } from './data/example-data';
import { createDataBundle } from './data/ownership';

export function App() {
  // Create the view registry once
  const viewRegistry = useMemo(() => createViewRegistry(), []);

  // Create the data bundle once
  const data = useMemo(() => createDataBundle(exampleEntities), []);

  return (
    <ViewRegistryContext.Provider value={viewRegistry}>
      <div style={{ width: '100vw', height: '100vh' }}>
        <RenderRoot data={data} viewRegistry={viewRegistry} />
      </div>
    </ViewRegistryContext.Provider>
  );
}
