/**
 * Debug Embed entry point - renders DebugEmbedApp for testing the embed view
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

import { createRoot } from 'react-dom/client';
import { DebugEmbedApp } from './DebugEmbedApp';

function init() {
  const container = document.getElementById('root');
  if (!container) {
    console.error('No root container found');
    return;
  }

  const root = createRoot(container);
  root.render(<DebugEmbedApp />);
}

init();
