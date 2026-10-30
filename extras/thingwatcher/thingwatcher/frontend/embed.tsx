/**
 * Embed entry point - renders EmbedApp for single-thing viewing
 */

// =============================================================================
// Shared Module Injection - MUST BE AT TOP
// =============================================================================
// Populate window.__THINGWATCHER_INJECTED__ with bundled modules.
// This is the same global used by esbuildSetup.ts for dynamic renderers.
// Import map shims (served at /shims/*) read from this global, ensuring
// esm.sh dependencies use the same React instance as the main app.

import * as React from 'react';
import * as ReactJsxRuntime from 'react/jsx-runtime';
import * as ReactDom from 'react-dom';
import * as ReactDomClient from 'react-dom/client';
import * as ThingwatcherBindings from './lib/ThingWatcherBindingsExternal';

declare global {
  interface Window {
    __THINGWATCHER_INJECTED__: Record<string, unknown>;
  }
}

window.__THINGWATCHER_INJECTED__ = {
  ...window.__THINGWATCHER_INJECTED__,
  'react': React,
  'react/jsx-runtime': ReactJsxRuntime,
  'react-dom': ReactDom,
  'react-dom/client': ReactDomClient,
  'thingwatcher-bindings': ThingwatcherBindings,
};

// =============================================================================
// Application
// =============================================================================

import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Provider } from 'urql';
import { createGraphQLClient, ThingWatcherProvider, RendererRefreshProvider } from './hooks';
import { EmbedApp } from './EmbedApp';

/**
 * Root component that manages GraphQL client and websocket connection state.
 */
function EmbedMain() {
  const [isWsConnected, setIsWsConnected] = useState(false);

  // Create client once with connection state callbacks
  const [client] = useState(() =>
    createGraphQLClient({
      onWsConnected: () => setIsWsConnected(true),
      onWsDisconnected: () => setIsWsConnected(false),
    })
  );

  return (
    <Provider value={client}>
      <RendererRefreshProvider>
        <ThingWatcherProvider isWsConnected={isWsConnected}>
          <EmbedApp />
        </ThingWatcherProvider>
      </RendererRefreshProvider>
    </Provider>
  );
}

async function init() {
  const container = document.getElementById('root');
  if (!container) {
    console.error('No root container found');
    return;
  }

  // Show loading state
  container.textContent = 'Connecting to thingwatcher...';

  try {
    // Render the embed app
    const root = createRoot(container);
    root.render(<EmbedMain />);
  } catch (err) {
    container.textContent = `Failed to initialize: ${err}`;
    console.error('Initialization error:', err);
  }
}

init();
