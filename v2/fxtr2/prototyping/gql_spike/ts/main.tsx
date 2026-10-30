import React from 'react';
import { createRoot } from 'react-dom/client';
import { Provider } from 'urql';
import { buildClient, fetchIntrospectionSchema } from './graphqlClient';
import { RootList } from './FooExplorer';
import { Client } from '@urql/core';

function App({ client }: { client: Client }) {
  return (
    <Provider value={client}>
      <div style={{ maxWidth: '900px', margin: '0 auto' }}>
        <h1>GraphQL Explorer</h1>
        <p style={{ color: '#666', marginBottom: '20px' }}>
          Click triangles to expand nodes. Check browser console to see GraphQL queries.
        </p>
        <RootList />
      </div>
    </Provider>
  );
}

// Initialize the React app after fetching introspection schema
async function init() {
  const container = document.getElementById('root');
  if (!container) {
    console.error('No root container found');
    return;
  }

  // Show loading state
  container.textContent = 'Loading schema...';

  // Fetch introspection schema and build client with it
  const schema = await fetchIntrospectionSchema();
  const client = buildClient(schema);

  // Render the app
  const root = createRoot(container);
  root.render(<App client={client} />);
}

init().catch(console.error);
