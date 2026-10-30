import React, { useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { ModulePage } from './ModulePage';
import { EntityDefinitionPage } from './EntityDefinitionPage';
import { EntityPage } from './EntityPage';
import { LinkRendererProvider } from './LinkRendererContext';

function App() {
  const [route, setRoute] = useState<{ type: 'module' | 'entityDefinition' | 'entity'; moduleCid?: string; entityIndex?: number; entityCid?: string } | null>(null);

  useEffect(() => {
    // Extract route information from URL
    const path = window.location.pathname;

    // Check for entity route: /view/entity/{cid}
    const entityMatch = path.match(/\/view\/entity\/([^\/]+)$/);
    if (entityMatch) {
      setRoute({
        type: 'entity',
        entityCid: entityMatch[1]
      });
      return;
    }

    // Check for entity definition route: /view/collection/{cid}/entdefn/{index}
    const entityDefnMatch = path.match(/\/view\/collection\/([^\/]+)\/entdefn\/(\d+)/);
    if (entityDefnMatch) {
      setRoute({
        type: 'entityDefinition',
        moduleCid: entityDefnMatch[1],
        entityIndex: parseInt(entityDefnMatch[2], 10)
      });
      return;
    }

    // Check for collection route: /view/collection/{cid}
    const moduleMatch = path.match(/\/view\/collection\/([^\/]+)$/);
    if (moduleMatch) {
      setRoute({
        type: 'module',
        moduleCid: moduleMatch[1]
      });
      return;
    }

    // Invalid route
    setRoute(null);
  }, []);

  if (!route) {
    return <div>Invalid URL - expected /view/collection/&lt;cid&gt;, /view/collection/&lt;cid&gt;/entdefn/&lt;index&gt;, or /view/entity/&lt;cid&gt;</div>;
  }

  const content = (() => {
    switch (route.type) {
      case 'entity':
        return <EntityPage entityCid={route.entityCid!} />;
      case 'entityDefinition':
        return <EntityDefinitionPage moduleCid={route.moduleCid!} entityIndex={route.entityIndex!} />;
      case 'module':
        return <ModulePage moduleCid={route.moduleCid!} />;
      default:
        return <div>Unknown route type</div>;
    }
  })();

  return (
    <LinkRendererProvider>
      {content}
    </LinkRendererProvider>
  );
}

// Initialize the React app
const container = document.getElementById('root');
if (container) {
  const root = createRoot(container);
  root.render(<App />);
}
