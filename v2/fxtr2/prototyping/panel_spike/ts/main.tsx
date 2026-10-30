import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './src/App';

// Old deps test (kept for reference):
// import { DepsTest } from './DepsTest';

const container = document.getElementById('root');
if (container) {
  const root = createRoot(container);
  root.render(<App />);
} else {
  console.error('No root container found');
}
