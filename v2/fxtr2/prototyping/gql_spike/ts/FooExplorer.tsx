import React, { useState } from 'react';
import { useQuery, useSubscription, gql, useClient } from 'urql';
import { getIntrospectionQuery } from 'graphql';

// Styles
const styles = {
  container: {
    fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
    fontSize: '14px',
    lineHeight: '1.5',
  },
  treeNode: {
    marginLeft: '20px',
  },
  expandButton: {
    cursor: 'pointer',
    userSelect: 'none' as const,
    marginRight: '4px',
    display: 'inline-block',
    width: '16px',
  },
  label: {
    color: '#032f62',
  },
  value: {
    color: '#d73a49',
  },
  typeTag: {
    color: '#6f42c1',
    fontSize: '12px',
    marginLeft: '8px',
  },
  loading: {
    color: '#666',
    fontStyle: 'italic' as const,
  },
  error: {
    color: '#cb2431',
  },
  nodeId: {
    color: '#999',
    fontSize: '11px',
    marginLeft: '8px',
  },
  refCard: {
    background: '#f6f8fa',
    border: '1px solid #e1e4e8',
    borderRadius: '6px',
    padding: '8px 12px',
    marginTop: '4px',
    marginLeft: '20px',
  },
  refLabel: {
    color: '#0366d6',
    fontWeight: 'bold' as const,
  },
  debugOutput: {
    background: '#1e1e1e',
    color: '#d4d4d4',
    padding: '12px',
    borderRadius: '4px',
    marginTop: '10px',
    maxHeight: '400px',
    overflow: 'auto',
    fontSize: '12px',
    whiteSpace: 'pre-wrap' as const,
    wordBreak: 'break-word' as const,
  },
};

// GraphQL Queries

// Registry query - gets all roots and streams with generation
const REGISTRY_QUERY = gql`
  query GetRegistry {
    registry {
      generation
      roots {
        id
      }
      streams {
        id
      }
    }
  }
`;

// Registry updates subscription
const REGISTRY_UPDATES_SUBSCRIPTION = gql`
  subscription RegistryUpdates($afterGeneration: Int!) {
    registryUpdates(afterGeneration: $afterGeneration) {
      __typename
      ... on RootAdded {
        generation
        root { id }
      }
      ... on RootRemoved {
        generation
        rootId
      }
      ... on StreamAdded {
        generation
        stream { id }
      }
      ... on StreamRemoved {
        generation
        streamId
      }
    }
  }
`;

// Query to get a stream's full content
const STREAM_QUERY = gql`
  query GetStream($streamId: StreamID!) {
    stream(id: $streamId) {
      id
      isFinalized
      itemCount
      key {
        targetId
      }
      items {
        id
        data {
          __typename
          ... on FooSingle {
            value { id data }
          }
          ... on FooList {
            items {
              id
              data { __typename }
            }
          }
          ... on FooDict {
            entries {
              key
              value {
                id
                data { __typename }
              }
            }
          }
          ... on FooRef {
            refTargetId: targetId
          }
          ... on FooStreamRef {
            streamRefTargetId: targetId
          }
        }
      }
      finalResult {
        targetId
      }
    }
  }
`;

// Query to get Root's content (first level entry point)
const ROOT_CONTENT_QUERY = gql`
  query GetRootContent($rootId: RootID!) {
    root(id: $rootId) {
      id
      content {
        id
        data {
          __typename
          ... on FooSingle {
            value { id data }
          }
          ... on FooList {
            items {
              id
              data { __typename }
            }
          }
          ... on FooDict {
            entries {
              key
              value {
                id
                data { __typename }
              }
            }
          }
          ... on FooRef {
            refTargetId: targetId
          }
          ... on FooStreamRef {
            streamRefTargetId: targetId
          }
        }
      }
    }
  }
`;

// Query for FooNode by opaque ID
const NODE_QUERY = gql`
  query GetNode($id: NodeID!) {
    node(id: $id) {
      id
      data {
        __typename
        ... on FooSingle {
          value { id data }
        }
        ... on FooList {
          items {
            id
            data { __typename }
          }
        }
        ... on FooDict {
          entries {
            key
            value {
              id
              data { __typename }
            }
          }
        }
        ... on FooRef {
          refTargetId: targetId
        }
        ... on FooStreamRef {
          streamRefTargetId: targetId
        }
      }
    }
  }
`;

// Types
interface Foo {
  id: string;
  data: string;
}

interface FooNodeStub {
  id: string;
  data: { __typename: string };
}

interface FooEntry {
  key: string;
  value: FooNodeStub;
}

// Helper: Get a friendly summary for a data type
function getTypeSummary(typename: string): string {
  switch (typename) {
    case 'FooSingle': return 'Single';
    case 'FooList': return 'List';
    case 'FooDict': return 'Dict';
    case 'FooRef': return 'Ref';
    case 'FooStreamRef': return 'StreamRef';
    default: return typename;
  }
}

// Type for registry state
interface RegistryState {
  generation: number;
  roots: { id: string }[];
  streams: { id: string }[];
}

// Component: Root List (now using registry with live subscription)
export function RootList() {
  const [registryResult] = useQuery({ query: REGISTRY_QUERY });
  const [debugOutput, setDebugOutput] = useState<string | null>(null);

  const initialRegistry = registryResult.data?.registry as RegistryState | undefined;
  const generation = initialRegistry?.generation ?? 0;

  // Subscribe to registry updates starting from current generation
  const [subscriptionResult] = useSubscription(
    {
      query: REGISTRY_UPDATES_SUBSCRIPTION,
      variables: { afterGeneration: generation },
      pause: registryResult.fetching || !initialRegistry,
    },
    // Reducer: update registry state based on subscription events
    (prevState: RegistryState | undefined, subscriptionData): RegistryState => {
      // Cast is safe because subscription is paused when initialRegistry is undefined
      const prev = prevState ?? (initialRegistry as RegistryState);
      if (!prev) return prev;

      const event = subscriptionData?.registryUpdates;
      if (!event) return prev;

      if (event.__typename === 'RootAdded') {
        return {
          ...prev,
          generation: event.generation,
          roots: [...prev.roots, { id: event.root.id }],
        };
      } else if (event.__typename === 'RootRemoved') {
        return {
          ...prev,
          generation: event.generation,
          roots: prev.roots.filter(r => r.id !== event.rootId),
        };
      } else if (event.__typename === 'StreamAdded') {
        return {
          ...prev,
          generation: event.generation,
          streams: [...prev.streams, { id: event.stream.id }],
        };
      } else if (event.__typename === 'StreamRemoved') {
        return {
          ...prev,
          generation: event.generation,
          streams: prev.streams.filter(s => s.id !== event.streamId),
        };
      }

      return prev;
    }
  );

  // Use subscription data if available, otherwise query data
  const registry = subscriptionResult.data ?? initialRegistry;
  const isSubscribed = !registryResult.fetching && initialRegistry;

  const roots = registry?.roots || [];
  const streams = registry?.streams || [];

  return (
    <div style={styles.container}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <h2 style={{ margin: 0 }}>Registry</h2>
        {isSubscribed && (
          <span style={{ color: '#2da44e', fontSize: '12px' }}>● live</span>
        )}
        <span style={{ color: '#666', fontSize: '12px' }}>
          generation: {registry?.generation ?? '?'}
        </span>
      </div>

      <h3 style={{ marginTop: '16px' }}>Roots ({roots.length})</h3>
      {registryResult.fetching && <div style={styles.loading}>Loading registry...</div>}
      {registryResult.error && <div style={styles.error}>Error: {registryResult.error.message}</div>}
      {roots.map((root: { id: string }) => (
        <RootView key={root.id} rootId={root.id} />
      ))}

      <h3 style={{ marginTop: '20px', color: '#8250df' }}>Streams ({streams.length})</h3>
      {streams.map((stream: { id: string }) => (
        <StreamView key={stream.id} streamId={stream.id} />
      ))}

      <DebugButtons onOutput={setDebugOutput} />
      {debugOutput && (
        <pre style={styles.debugOutput}>{debugOutput}</pre>
      )}
    </div>
  );
}

// Component: Root entry point (not a Node)
// Uses ROOT_CONTENT_QUERY to get the content's ID, then threads it through
function RootView({ rootId }: { rootId: string }) {
  const [expanded, setExpanded] = useState(false);

  const [result] = useQuery({
    query: ROOT_CONTENT_QUERY,
    variables: { rootId },
    pause: !expanded,
  });

  const content = result.data?.root?.content;

  return (
    <div style={{ padding: '2px 0' }}>
      <span style={styles.expandButton} onClick={() => setExpanded(!expanded)}>
        {expanded ? '▼' : '▶'}
      </span>
      <span style={styles.label}>Root "{rootId}"</span>
      {!expanded && <span style={styles.typeTag}>(click to load content)</span>}
      {expanded && (
        <div style={styles.treeNode}>
          {result.fetching && <div style={styles.loading}>Loading...</div>}
          {result.error && <div style={styles.error}>Error: {result.error.message}</div>}
          {content && (
            <>
              <span style={styles.label}>content: </span>
              <span style={styles.nodeId}>id: {content.id}</span>
              <FooDataContent data={content.data} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

// Component: FooNode View using node(id) query
// Takes an opaque nodeId and fetches by ID
function FooNodeView({
  nodeId,
  label,
  index,
  previewTypename,
}: {
  nodeId: string;
  label?: string;
  index?: number;
  previewTypename?: string;
}) {
  const [expanded, setExpanded] = useState(false);

  // Use node(id) with opaque ID
  const [result] = useQuery({
    query: NODE_QUERY,
    variables: { id: nodeId },
    pause: !expanded,
  });

  const fooNode = result.data?.node;
  const displayLabel = label || (index !== undefined ? `[${index}]` : '');

  // Build preview text from typename
  const previewText = previewTypename
    ? `${getTypeSummary(previewTypename)}`
    : 'click to load';

  return (
    <div style={{ padding: '2px 0' }}>
      <span style={styles.expandButton} onClick={() => setExpanded(!expanded)}>
        {expanded ? '▼' : '▶'}
      </span>
      {displayLabel && <span style={styles.label}>{displayLabel}</span>}
      {!expanded && <span style={styles.typeTag}>({previewText})</span>}
      {expanded && (
        <div style={styles.treeNode}>
          {result.fetching && <div style={styles.loading}>Loading...</div>}
          {result.error && <div style={styles.error}>Error: {result.error.message}</div>}
          {fooNode && (
            <>
              <span style={styles.nodeId}>id: {fooNode.id}</span>
              <FooDataContent data={fooNode.data} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

// Component: Render FooData content based on __typename
// No longer needs rootId/path - child nodes have their IDs already
function FooDataContent({
  data,
}: {
  data: any;
}) {
  const typename = data.__typename;

  return (
    <div>
      <span style={styles.typeTag}>{typename}</span>

      {typename === 'FooSingle' && data.value && (
        <div style={styles.treeNode}>
          <FooView foo={data.value} />
        </div>
      )}

      {typename === 'FooList' && data.items && (
        <div style={styles.treeNode}>
          <span style={styles.label}>items:</span>
          <span style={{ color: '#666', marginLeft: '8px' }}>({data.items.length} items)</span>
          {data.items.map((item: FooNodeStub, idx: number) => (
            <FooNodeView
              key={item.id}
              nodeId={item.id}
              index={idx}
              previewTypename={item.data.__typename}
            />
          ))}
        </div>
      )}

      {typename === 'FooDict' && data.entries && (
        <div style={styles.treeNode}>
          <span style={styles.label}>entries:</span>
          <span style={{ color: '#666', marginLeft: '8px' }}>({data.entries.length} entries)</span>
          {data.entries.map((entry: FooEntry) => (
            <FooNodeView
              key={entry.value.id}
              nodeId={entry.value.id}
              label={`"${entry.key}":`}
              previewTypename={entry.value.data.__typename}
            />
          ))}
        </div>
      )}

      {typename === 'FooRef' && (
        <FooRefView targetId={data.refTargetId} />
      )}

      {typename === 'FooStreamRef' && (
        <FooStreamRefView targetId={data.streamRefTargetId} />
      )}
    </div>
  );
}

// Component: FooStreamRef display with expandable referenced stream (with live subscription)
function FooStreamRefView({ targetId }: { targetId: string }) {
  const [expanded, setExpanded] = useState(false);
  const { stream, isSubscribed, fetching, error } = useStreamData(targetId, expanded);

  return (
    <div style={styles.treeNode}>
      <span style={styles.label}>targetId: </span>
      <span style={{ ...styles.refLabel, color: '#8250df' }}>"{targetId}"</span>
      <span style={styles.typeTag}>(stream)</span>
      {isSubscribed && (
        <span style={{ color: '#2da44e', fontSize: '11px', marginLeft: '8px' }}>● live</span>
      )}
      <div style={{ padding: '2px 0' }}>
        <span style={styles.expandButton} onClick={() => setExpanded(!expanded)}>
          {expanded ? '▼' : '▶'}
        </span>
        <span style={styles.label}>load()</span>
        {!expanded && <span style={styles.typeTag}>(click to load referenced stream)</span>}
      </div>
      {expanded && (
        <div style={{ ...styles.refCard, borderColor: '#8250df' }}>
          {fetching && <div style={styles.loading}>Loading referenced stream...</div>}
          {error && <div style={styles.error}>Error: {error.message}</div>}
          {stream && <StreamContentView stream={stream} />}
        </div>
      )}
    </div>
  );
}

// Component: FooRef display with expandable referenced root
function FooRefView({ targetId }: { targetId: string }) {
  const [expanded, setExpanded] = useState(false);

  const [result] = useQuery({
    query: ROOT_CONTENT_QUERY,
    variables: { rootId: targetId },
    pause: !expanded,
  });

  const content = result.data?.root?.content;

  return (
    <div style={styles.treeNode}>
      <span style={styles.label}>targetId: </span>
      <span style={styles.refLabel}>"{targetId}"</span>
      <div style={{ padding: '2px 0' }}>
        <span style={styles.expandButton} onClick={() => setExpanded(!expanded)}>
          {expanded ? '▼' : '▶'}
        </span>
        <span style={styles.label}>load()</span>
        {!expanded && <span style={styles.typeTag}>(click to load referenced root)</span>}
      </div>
      {expanded && (
        <div style={styles.refCard}>
          {result.fetching && <div style={styles.loading}>Loading referenced root...</div>}
          {result.error && <div style={styles.error}>Error: {result.error.message}</div>}
          {content && (
            <>
              <div style={{ marginBottom: '4px' }}>
                <span style={styles.refLabel}>Referenced Root "{targetId}"</span>
              </div>
              <span style={styles.label}>content: </span>
              <span style={styles.nodeId}>id: {content.id}</span>
              <FooDataContent data={content.data} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

// Component: Foo leaf display
function FooView({ foo }: { foo: Foo }) {
  return (
    <div>
      <span style={styles.label}>value: </span>
      <span style={{ color: '#666' }}>{'{'}</span>
      <span style={styles.label}> id: </span>
      <span style={styles.value}>"{foo.id}"</span>
      <span style={{ color: '#666' }}>,</span>
      <span style={styles.label}> data: </span>
      <span style={styles.value}>"{foo.data}"</span>
      <span style={{ color: '#666' }}> {'}'}</span>
    </div>
  );
}

// Component: Stream content display (used by FooStreamRefView and StreamView)
function StreamContentView({ stream }: { stream: any }) {
  return (
    <div>
      <div style={{ marginBottom: '4px' }}>
        <span style={{ ...styles.refLabel, color: '#8250df' }}>Stream "{stream.id}"</span>
        {stream.isFinalized && (
          <span style={{ ...styles.typeTag, color: '#cb2431' }}>(finalized)</span>
        )}
      </div>

      {stream.key && (
        <div>
          <span style={styles.label}>key: </span>
          <FooRefView targetId={stream.key.targetId} />
        </div>
      )}

      {stream.items !== null && (
        <div style={styles.treeNode}>
          <span style={styles.label}>items:</span>
          <span style={{ color: '#666', marginLeft: '8px' }}>
            ({stream.items?.length ?? 0} items)
          </span>
          {stream.items?.map((item: FooNodeStub, idx: number) => (
            <FooNodeView
              key={item.id}
              nodeId={item.id}
              index={idx}
              previewTypename={item.data.__typename}
            />
          ))}
        </div>
      )}

      {stream.isFinalized && stream.items === null && (
        <div style={{ color: '#666', fontStyle: 'italic' }}>
          (items not available - stream is finalized)
        </div>
      )}

      {stream.finalResult && (
        <div>
          <span style={styles.label}>finalResult: </span>
          <FooRefView targetId={stream.finalResult.targetId} />
        </div>
      )}
    </div>
  );
}

// Subscription for stream updates (used by StreamView)
const STREAM_VIEW_SUBSCRIPTION = gql`
  subscription StreamViewUpdates($id: StreamID!, $afterIndex: Int!) {
    streamUpdates(id: $id, afterIndex: $afterIndex) {
      __typename
      ... on StreamItemEmitted {
        streamId
        index
        node {
          id
          data {
            __typename
            ... on FooSingle {
              value { id data }
            }
            ... on FooList {
              items {
                id
                data { __typename }
              }
            }
            ... on FooDict {
              entries {
                key
                value {
                  id
                  data { __typename }
                }
              }
            }
            ... on FooRef {
              refTargetId: targetId
            }
            ... on FooStreamRef {
              streamRefTargetId: targetId
            }
          }
        }
      }
      ... on StreamFinalized {
        streamId
        finalResultTargetId
      }
    }
  }
`;

// Type for combined stream state
interface StreamState {
  id: string;
  isFinalized: boolean;
  itemCount: number | null;
  key: { targetId: string } | null;
  items: FooNodeStub[] | null;
  finalResult: { targetId: string } | null;
}

// Hook: Stream data with live subscription
// Overall pattern based on https://github.com/urql-graphql/urql/issues/347#issuecomment-550427830
function useStreamData(streamId: string, expanded: boolean) {
  // Initial query for stream data
  const [queryResult] = useQuery({
    query: STREAM_QUERY,
    variables: { streamId },
    pause: !expanded,
  });

  const initialStream = queryResult.data?.stream;
  const lastItemIndex = initialStream?.items?.length ?? 0;

  // Subscribe to updates starting after the last known item
  const [subscriptionResult] = useSubscription(
    {
      query: STREAM_VIEW_SUBSCRIPTION,
      variables: { id: streamId, afterIndex: lastItemIndex - 1 },
      // Only subscribe once query has completed and stream is not finalized
      pause: !expanded || queryResult.fetching || !initialStream || initialStream.isFinalized,
    },
    // Reducer: combine subscription events with previous state
    (prevState: StreamState | undefined, subscriptionData): StreamState => {
      // Initialize from query result if no previous state
      const prev = prevState ?? (initialStream as StreamState);
      if (!prev) return prev;

      const event = subscriptionData?.streamUpdates;
      if (!event) return prev;

      if (event.__typename === 'StreamItemEmitted') {
        // Add new item to the list
        const newItems = prev.items ? [...prev.items] : [];
        // Only add if we don't already have this index
        if (event.index >= newItems.length) {
          newItems.push(event.node);
        }
        return {
          ...prev,
          items: newItems,
          itemCount: newItems.length,
        };
      } else if (event.__typename === 'StreamFinalized') {
        // Stream was finalized
        return {
          ...prev,
          isFinalized: true,
          items: null,  // Items become unavailable after finalization
          finalResult: { targetId: event.finalResultTargetId },
        };
      }

      return prev;
    }
  );

  // Use subscription data if available, otherwise query data
  const stream = subscriptionResult.data ?? initialStream;
  const isSubscribed = !queryResult.fetching && initialStream && !initialStream.isFinalized;

  return {
    stream,
    isSubscribed,
    fetching: queryResult.fetching,
    error: queryResult.error,
  };
}

// Component: Stream entry point with live subscription
function StreamView({ streamId }: { streamId: string }) {
  const [expanded, setExpanded] = useState(false);
  const { stream, isSubscribed, fetching, error } = useStreamData(streamId, expanded);

  return (
    <div style={{ padding: '2px 0' }}>
      <span style={styles.expandButton} onClick={() => setExpanded(!expanded)}>
        {expanded ? '▼' : '▶'}
      </span>
      <span style={{ ...styles.label, color: '#8250df' }}>Stream "{streamId}"</span>
      {!expanded && <span style={styles.typeTag}>(click to load stream)</span>}
      {expanded && (
        <div style={styles.treeNode}>
          {fetching && <div style={styles.loading}>Loading...</div>}
          {error && <div style={styles.error}>Error: {error.message}</div>}
          {stream && (
            <>
              {isSubscribed && (
                <span style={{ color: '#2da44e', fontSize: '11px', marginLeft: '8px' }}>
                  ● live
                </span>
              )}
              <StreamContentView stream={stream} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

// Debug query for nodeByPath
const DEBUG_NODE_BY_PATH_QUERY = gql`
  query DebugNodeByPath($rootId: RootID!, $path: JSONPath!) {
    nodeByPath(rootId: $rootId, path: $path) {
      __typename
      id
      data {
        __typename
        ... on FooSingle {
          value { id data }
        }
        ... on FooList {
          items {
            id
            data { __typename }
          }
        }
        ... on FooDict {
          entries {
            key
            value {
              id
              data { __typename }
            }
          }
        }
        ... on FooRef {
          refTargetId: targetId
        }
        ... on FooStreamRef {
          streamRefTargetId: targetId
        }
      }
    }
  }
`;

// Debug query for loading a root with ref and its referenced content in one call
const DEBUG_REF_WITH_LOAD_QUERY = gql`
  query DebugRefWithLoad($rootId: RootID!) {
    root(id: $rootId) {
      id
      content {
        id
        data {
          __typename
          ... on FooDict {
            entries {
              key
              value {
                id
                data {
                  __typename
                  ... on FooSingle {
                    value { id data }
                  }
                  ... on FooRef {
                    targetId
                    load {
                      id
                      content {
                        id
                        data {
                          __typename
                          ... on FooSingle {
                            value { id data }
                          }
                          ... on FooList {
                            items {
                              id
                              data { __typename }
                            }
                          }
                          ... on FooDict {
                            entries {
                              key
                              value {
                                id
                                data { __typename }
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
`;

// Component: Debug buttons
export function DebugButtons({ onOutput }: { onOutput: (output: string) => void }) {
  const client = useClient();
  const [demoRunning, setDemoRunning] = useState(false);

  const handleFetchNodeByPath = async () => {
    console.log('[Debug] Fetching nodeByPath(rootId: "list", path: [1])...');
    const result = await client.query(DEBUG_NODE_BY_PATH_QUERY, {
      rootId: 'list',
      path: JSON.stringify([1]),
    }).toPromise();
    console.log('[Debug] nodeByPath result:', result);
    onOutput(JSON.stringify(result.data, null, 2));
  };

  const handleIntrospection = async () => {
    console.log('[Debug] Running introspection query...');
    const introspectionQuery = getIntrospectionQuery({ descriptions: false });
    const result = await client.query(gql`${introspectionQuery}`, {}).toPromise();
    console.log('[Debug] Introspection result:', result);
    onOutput(JSON.stringify(result.data, null, 2));
  };

  const handleFetchRefWithLoad = async () => {
    console.log('[Debug] Fetching with_ref root with nested load()...');
    const result = await client.query(DEBUG_REF_WITH_LOAD_QUERY, {
      rootId: 'with_ref',
    }).toPromise();
    console.log('[Debug] RefWithLoad result:', result);
    onOutput(JSON.stringify(result.data, null, 2));
  };

  // Demo handler - triggers the multi-stream demo process
  const handleStartDemo = async () => {
    console.log('[Demo] Starting demo process...');
    setDemoRunning(true);
    onOutput('Starting demo...');
    try {
      const response = await fetch('/api/demo/start', {
        method: 'POST',
      });
      const result = await response.json();
      console.log('[Demo] Start result:', result);
      if (result.ok) {
        onOutput('Demo started! Watch the registry update with new roots and streams.\nChild streams will count up at different speeds and then finalize.');
      } else {
        onOutput(`Demo error: ${result.error}`);
      }
    } catch (err) {
      console.error('[Demo] Error:', err);
      onOutput(`Error: ${err}`);
    } finally {
      // Reset button after a short delay
      setTimeout(() => setDemoRunning(false), 2000);
    }
  };

  return (
    <div style={{ marginTop: '20px', padding: '10px', borderTop: '1px solid #ccc' }}>
      <div style={{ marginBottom: '10px' }}>
        <strong style={{ color: '#8250df' }}>Demo:</strong>
        <button
          onClick={handleStartDemo}
          disabled={demoRunning}
          style={{
            marginLeft: '10px',
            padding: '8px 16px',
            backgroundColor: demoRunning ? '#ccc' : '#2da44e',
            color: 'white',
            border: 'none',
            borderRadius: '6px',
            fontWeight: 'bold',
            cursor: demoRunning ? 'not-allowed' : 'pointer',
          }}
        >
          {demoRunning ? 'Running...' : '▶ Start Demo'}
        </button>
        <span style={{ marginLeft: '10px', color: '#666', fontSize: '12px' }}>
          Creates parent/child streams that count up and finalize
        </span>
      </div>
      <div>
        <strong>Debug Queries:</strong>
        <button
          onClick={handleFetchNodeByPath}
          style={{ marginLeft: '10px', padding: '4px 8px' }}
        >
          nodeByPath("list", [1])
        </button>
        <button
          onClick={handleIntrospection}
          style={{ marginLeft: '10px', padding: '4px 8px' }}
        >
          Introspection
        </button>
        <button
          onClick={handleFetchRefWithLoad}
          style={{ marginLeft: '10px', padding: '4px 8px' }}
        >
          with_ref + load()
        </button>
      </div>
    </div>
  );
}
