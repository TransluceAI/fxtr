import { Client, fetchExchange, cacheExchange, subscriptionExchange, gql } from '@urql/core';
import { cacheExchange as graphcacheExchange } from '@urql/exchange-graphcache';
import { Exchange, Operation, OperationResult } from '@urql/core';
import { pipe, tap } from 'wonka';
import { IntrospectionQuery } from 'graphql';
import { createClient as createWSClient } from 'graphql-ws';

// Minimal query for updating cached stream state (just the mutable fields).
// We never actually run this query, but instead we use it with urql as a virtual query to populate
// the new state of the cache based on a subscription event from the server.
const STREAM_STATE_QUERY = gql`
  query GetStreamState($streamId: StreamID!) {
    stream(id: $streamId) {
      id
      isFinalized
      itemCount
      items {
        id
      }
      finalResult {
        targetId
      }
    }
  }
`;

// Virtual query for updating cached registry state.
// Used to update the cache when registry subscription events arrive.
const REGISTRY_STATE_QUERY = gql`
  query GetRegistryState {
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

// Custom exchange that logs all GraphQL queries to console
const loggingExchange = (disamb: string): Exchange => ({ forward }) => {
  return (ops$) => {
    return pipe(
      ops$,
      tap((operation: Operation) => {
        if (operation.kind === 'query' || operation.kind === 'mutation') {
          const queryStr = operation.query.loc?.source.body || 'unknown query';
          const variables = operation.variables;
          console.log(`[GQL:${disamb}] ${operation.kind}:`, queryStr.trim());
          if (variables && Object.keys(variables).length > 0) {
            console.log(`[GQL:${disamb}] variables:`, variables);
          }
        }
      }),
      forward,
      tap((result: OperationResult) => {
        if (result.error) {
          console.error(`[GQL:${disamb}] error:`, result.error);
        } else {
          console.log(`[GQL:${disamb}] result:`, result);
        }
      })
    );
  };
};

// Container type for node IDs
type ContainerRef = ['root', string] | ['stream', string];

// Helper: Build node ID from container and path
// Format: [["root"|"stream", id], ...path]
function buildNodeId(container: ContainerRef, path: (string | number)[]): string {
  return JSON.stringify([container, ...path]);
}

// Helper: Build node ID from root ID and path
function buildNodeIdFromRoot(rootId: string, path: (string | number)[]): string {
  return buildNodeId(['root', rootId], path);
}

// Helper: Build node ID from stream ID and path (path[0] is always item index)
function buildNodeIdFromStream(streamId: string, path: (string | number)[]): string {
  return buildNodeId(['stream', streamId], path);
}

// Build normalized cache with optional schema for better type awareness
function buildNormalizedCache(schema?: IntrospectionQuery) {
  return graphcacheExchange({
    schema: schema || undefined,
    keys: {
      // Root has simple string ID
      Root: (data) => data.id as string,
      // Stream has simple string ID
      Stream: (data) => data.id as string,
      // Registry is a singleton (no ID needed)
      Registry: () => null,
      // FooNode is the main cacheable entity
      FooNode: (data) => data.id as string,
      // Data types don't have IDs - they're embedded in FooNode
      FooSingle: () => null,
      FooList: () => null,
      FooDict: () => null,
      FooEntry: () => null,
      FooRef: () => null,
      FooStreamRef: () => null,
      // Foo (the leaf value) uses its own id
      Foo: (data) => data.id as string,
      // Registry update event types don't need IDs
      RootAdded: () => null,
      RootRemoved: () => null,
      StreamAdded: () => null,
      StreamRemoved: () => null,
    },

    resolvers: {
      Query: {
        // Resolve node(id) from cache
        node: (_, args, cache) => {
          return { __typename: 'FooNode', id: args.id as string };
        },

        // Resolve nodeByPath(rootId, path) by computing what the ID would be
        // Note: it is very important that this return the same ID as will be returned by the
        // server, or else the response may be dropped and replaced with null!
        nodeByPath: (_, args, cache) => {
          const id = buildNodeIdFromRoot(args.rootId as string, JSON.parse(args.path as string));
          console.log(`nodeByPath->`, { __typename: 'FooNode', id });
          return { __typename: 'FooNode', id };
        },

        // Resolve root(id)
        root: (_, args, cache) => {
          return { __typename: 'Root', id: args.id as string };
        },

        // Resolve stream(id)
        stream: (_, args, cache) => {
          return { __typename: 'Stream', id: args.id as string };
        },
      },
    },

    updates: {
      Subscription: {
        // Handle stream update events from subscription. This is called when a subscription event
        // is received from the server, and it must compute the new state that the corresponding
        // stream data should have if fetched anew. Note that even if we don't update the cache,
        // a proper live stream component will subscribe to updates based on the data it has and
        // thus receive all necessary updates. But updating the cache here allows us to avoid
        // repeatedly re-fetching updates from a stale cache. Based on
        // https://nearform.com/open-source/urql/docs/graphcache/cache-updates/#updating-lists-or-links
        streamUpdates: (result: any, _args, cache, _info) => {
          const event = result?.streamUpdates;
          if (!event) return;

          console.log('[Cache Update] streamUpdates event:', event);

          if (event.__typename === 'StreamItemEmitted') {
            const streamId = event.streamId;
            const newNode = event.node;

            cache.updateQuery(
              { query: STREAM_STATE_QUERY, variables: { streamId } },
              (data: any) => {
                if (!data?.stream) {
                  console.log('[Cache Update] No cached stream found for', streamId);
                  return data;
                }

                // Append new item to items list
                const currentItems = data.stream.items || [];
                const newItems = [...currentItems, { __typename: 'FooNode', id: newNode.id }];

                console.log('[Cache Update] Adding item to stream', streamId, ':', newNode.id);

                return {
                  ...data,
                  stream: {
                    ...data.stream,
                    items: newItems,
                    itemCount: newItems.length,
                  },
                };
              }
            );
          } else if (event.__typename === 'StreamFinalized') {
            const streamId = event.streamId;
            const finalResultTargetId = event.finalResultTargetId;

            cache.updateQuery(
              { query: STREAM_STATE_QUERY, variables: { streamId } },
              (data: any) => {
                if (!data?.stream) {
                  console.log('[Cache Update] No cached stream found for', streamId);
                  return data;
                }

                console.log('[Cache Update] Finalizing stream', streamId, 'with result', finalResultTargetId);

                return {
                  ...data,
                  stream: {
                    ...data.stream,
                    isFinalized: true,
                    items: null,
                    itemCount: null,
                    finalResult: { __typename: 'FooRef', targetId: finalResultTargetId },
                  },
                };
              }
            );
          }
        },

        // Handle registry update events from subscription
        registryUpdates: (result: any, _args, cache, _info) => {
          const event = result?.registryUpdates;
          if (!event) return;

          console.log('[Cache Update] registryUpdates event:', event);

          cache.updateQuery(
            { query: REGISTRY_STATE_QUERY },
            (data: any) => {
              if (!data?.registry) {
                console.log('[Cache Update] No cached registry found');
                return data;
              }

              const currentRoots = data.registry.roots || [];
              const currentStreams = data.registry.streams || [];

              if (event.__typename === 'RootAdded') {
                const newRoot = event.root;
                console.log('[Cache Update] Adding root to registry:', newRoot.id);
                return {
                  ...data,
                  registry: {
                    ...data.registry,
                    generation: event.generation,
                    roots: [...currentRoots, { __typename: 'Root', id: newRoot.id }],
                  },
                };
              } else if (event.__typename === 'RootRemoved') {
                const rootId = event.rootId;
                console.log('[Cache Update] Removing root from registry:', rootId);
                return {
                  ...data,
                  registry: {
                    ...data.registry,
                    generation: event.generation,
                    roots: currentRoots.filter((r: any) => r.id !== rootId),
                  },
                };
              } else if (event.__typename === 'StreamAdded') {
                const newStream = event.stream;
                console.log('[Cache Update] Adding stream to registry:', newStream.id);
                return {
                  ...data,
                  registry: {
                    ...data.registry,
                    generation: event.generation,
                    streams: [...currentStreams, { __typename: 'Stream', id: newStream.id }],
                  },
                };
              } else if (event.__typename === 'StreamRemoved') {
                const streamId = event.streamId;
                console.log('[Cache Update] Removing stream from registry:', streamId);
                return {
                  ...data,
                  registry: {
                    ...data.registry,
                    generation: event.generation,
                    streams: currentStreams.filter((s: any) => s.id !== streamId),
                  },
                };
              }

              return data;
            }
          );
        },
      },
    },
  });
}

// Build WebSocket client for subscriptions
function buildWSClient() {
  // Construct WebSocket URL from current location
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}/graphql/`;
  console.log('[Init] Creating WebSocket client for:', wsUrl);

  return createWSClient({
    url: wsUrl,
    // Reconnect on close
    retryAttempts: 5,
    shouldRetry: () => true,
    on: {
      connected: () => console.log('[WS] Connected'),
      closed: () => console.log('[WS] Closed'),
      error: (err) => console.error('[WS] Error:', err),
    },
  });
}

// Build client - using simple document cache for now
// TODO: Add back graphcache with subscription cache updates
export function buildClient(schema?: IntrospectionQuery): Client {
  const wsClient = buildWSClient();

  const client = new Client({
    url: '/graphql/',
    exchanges: [
      // loggingExchange('pre-cache'),
      buildNormalizedCache(schema),
      // loggingExchange('post-cache'),
      fetchExchange,
      subscriptionExchange({
        forwardSubscription: (request) => ({
          subscribe: (sink) => {
            // Wrap sink to log subscription events for debugging
            const loggingSink = {
              next: (value: unknown) => {
                console.log('[Subscription', request.operationName, request.variables, '] next:', value);
                sink.next(value as any);
              },
              error: (err: unknown) => {
                console.error('[Subscription', request.operationName, request.variables, '] error:', err);
                sink.error(err);
              },
              complete: () => {
                console.log('[Subscription', request.operationName, request.variables, '] complete');
                sink.complete();
              },
            };
            return {
              unsubscribe: wsClient.subscribe(
                { ...request, query: request.query! },
                loggingSink  // could also just use `sink` if we don't want the logging
              ),
            };
          },
        }),
      }),
    ],
  });

  // Subscribe to debug events if available (development only)
  if (client.subscribeToDebugTarget) {
    client.subscribeToDebugTarget((event) => {
      console.log(`[Debug:${event.source}] ${event.type}:`, event.message, event.data ?? '');
    });
    console.log('[Init] Subscribed to debug events');
  }

  return client;
}

// Fetch introspection schema from server
export async function fetchIntrospectionSchema(): Promise<IntrospectionQuery> {
  const { getIntrospectionQuery } = await import('graphql');
  const query = getIntrospectionQuery({ descriptions: false });

  const response = await fetch('/graphql/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query }),
  });

  const result = await response.json();
  console.log('[Init] Fetched introspection schema');
  return result.data as IntrospectionQuery;
}
