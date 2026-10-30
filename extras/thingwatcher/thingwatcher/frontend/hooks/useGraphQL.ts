import { Client, fetchExchange, subscriptionExchange, errorExchange } from '@urql/core';
import { cacheExchange, Data } from '@urql/exchange-graphcache';
import { createClient as createWSClient } from 'graphql-ws';
import { GET_LATEST_TICK, GET_TICKS, GET_TRIGGERABLE_KEYS } from '../graphql/queries';
import type { Tick, NewTickPayload } from '../types';

/**
 * Error exchange that logs all GraphQL errors to the console.
 * This ensures errors don't get silently swallowed by hooks.
 */
const logErrorExchange = errorExchange({
  onError: (error, operation) => {
    const operationType = operation.kind;
    const operationName = operation.query.definitions
      .filter((def): def is import('graphql').OperationDefinitionNode => def.kind === 'OperationDefinition')
      .map(def => def.name?.value || 'anonymous')
      .join(', ');

    console.error(
      `[GraphQL ${operationType}] ${operationName}:`,
      error.message,
      '\nOperation:', operation,
      '\nError:', error
    );
  },
});

/**
 * Options for creating the GraphQL client.
 */
export interface CreateGraphQLClientOptions {
  /** Called when websocket connects */
  onWsConnected?: () => void;
  /** Called when websocket disconnects */
  onWsDisconnected?: () => void;
}

/**
 * Create urql client configured for thingwatcher GraphQL endpoint.
 * Supports both HTTP queries/mutations and WebSocket subscriptions.
 *
 * Uses graphcache for normalized caching:
 * - DataChunk entities cached by ID (immutable)
 * - Thing entities NOT cached by ID (links/backlinks can change)
 * - Subscription handler updates cache on newTick events
 */
export function createGraphQLClient(options: CreateGraphQLClientOptions = {}): Client {
  // Determine URLs based on current location
  const protocol = window.location.protocol;
  const host = window.location.host;
  const httpUrl = `${protocol}//${host}/graphql/`;
  const wsProtocol = protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${host}/graphql/`;

  // Create WebSocket client for subscriptions
  const wsClient = createWSClient({
    url: wsUrl,
    // Retry on connection loss
    retryAttempts: Infinity,
    shouldRetry: () => true,
    // Lifecycle callbacks for connection state tracking
    on: {
      connected: () => options.onWsConnected?.(),
      closed: () => options.onWsDisconnected?.(),
    },
  });

  // Configure graphcache with entity keys and subscription handlers
  const cache = cacheExchange({
    keys: {
      // DataChunk entities are keyed by their 'id' field for caching (immutable)
      DataChunk: (data) => (data.id != null ? String(data.id) : null),
      // Thing is NOT cached by ID - links/backlinks can change independently
      // Thing data is invalidated and refetched when tick changes
      Thing: () => null,
      // Types without natural unique keys
      Tick: () => null,
      Link: () => null,
      Root: () => null,
      ThingChange: () => null,
      ThingTypeConfig: () => null,
      LinkColorConfig: () => null,
      NewTickPayload: () => null,
      RendererReference: () => null,
    },
    updates: {
      Subscription: {
        /**
         * Handle newTick subscription events.
         *
         * Updates the cache with:
         * - latestTick: updated directly with tick data
         * - triggerableKeys: updated directly from payload
         * - ticks list: appended or updated (streaming updates may update existing tick)
         *
         * Also invalidates tick-specific queries to force refetch.
         */
        newTick: (result, _args, cache, _info) => {
          const payload = (result as { newTick?: NewTickPayload })?.newTick;
          if (!payload) return;

          const { tick, triggerableKeys } = payload;

          // FIRST: Invalidate to ensure stale data is cleared
          // Invalidate tick query for this specific tick
          cache.invalidate('Query', 'tick', { index: tick.index });

          // Invalidate thing queries at this specific tick only
          // (necessary for streaming updates where tick index stays the same)
          const fields = cache.inspectFields('Query');
          for (const field of fields) {
            if (field.fieldName === 'thing') {
              const args = field.arguments as { atTick?: number } | null;
              if (args?.atTick === tick.index) {
                cache.invalidate('Query', 'thing', field.arguments);
              }
            }
            // Invalidate triggerRecords queries that include this tick
            // (trigger records are written after post_trigger tick is created)
            if (field.fieldName === 'triggerRecords') {
              const args = field.arguments as { from?: number; to?: number } | null;
              // Invalidate if this tick falls within the query's range
              const from = args?.from ?? 0;
              const to = args?.to ?? Infinity;
              if (tick.index >= from && tick.index <= to) {
                cache.invalidate('Query', 'triggerRecords', field.arguments);
              }
            }
          }

          // Note: DataChunk queries are NOT invalidated - chunks are immutable.
          // New chunks created by ThingDelta append will be fetched fresh.

          // THEN: Update simple queries with data we have
          cache.updateQuery({ query: GET_LATEST_TICK }, () => ({
            latestTick: tick,
          }));

          cache.updateQuery({ query: GET_TRIGGERABLE_KEYS }, () => ({
            triggerableKeys,
          }));

          // Append or replace tick in ticks list
          // (streaming updates may update an existing tick rather than adding new)
          cache.updateQuery({ query: GET_TICKS }, (data) => {
            const existingData = data as { ticks?: Tick[] } | null;
            const ticks = existingData?.ticks || [];
            const existingIndex = ticks.findIndex(t => t.index === tick.index);
            if (existingIndex >= 0) {
              // Replace existing tick (streaming update)
              const updated = [...ticks];
              updated[existingIndex] = tick;
              return { ticks: updated } as unknown as Data;
            } else {
              // Append new tick
              return { ticks: [...ticks, tick] } as unknown as Data;
            }
          });
        },
      },
    },
  });

  // Create urql client
  const client = new Client({
    url: httpUrl,
    exchanges: [
      logErrorExchange,
      cache,
      fetchExchange,
      subscriptionExchange({
        forwardSubscription(request) {
          const input = { ...request, query: request.query || '' };
          return {
            subscribe(sink) {
              const unsubscribe = wsClient.subscribe(input, sink);
              return { unsubscribe };
            },
          };
        },
      }),
    ],
  });

  return client;
}
