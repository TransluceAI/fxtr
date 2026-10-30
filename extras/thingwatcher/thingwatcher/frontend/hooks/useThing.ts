import { useState, useEffect, useMemo } from 'react';
import { useQuery, useClient } from 'urql';
import { gql } from '@urql/core';
import { useThingWatcher } from './ThingWatcherProvider';
import type { Thing } from '../types';

// =============================================================================
// Types
// =============================================================================

/**
 * Input for identifying a thing - either by b64 key or keyJSON.
 * shortId is NOT supported here - use the canonical key.
 */
export type ThingKeyInput =
  | { key: string; keyJSON?: never }
  | { keyJSON: unknown; key?: never };

export interface UseThingOptions {
  /** Thing identifier - either { key } or { keyJSON } */
  keyInput: ThingKeyInput;
  /** Tick to query at. Defaults to resolvedTick from context. */
  tick?: number;
  /** Pause the query. */
  pause?: boolean;
  /**
   * Include full chunk data in the response.
   * When true, resolves dataChunks to include `data` field (uses graphcache for efficiency).
   * When false (default), dataChunks only contain `id`.
   * Use true when rendering thing details, false for simple lookups.
   */
  includeData?: boolean;
}

export interface UseThingResult {
  /** The thing data, or null if not found/loading. */
  thing: Thing | null;
  /** Whether the query is currently loading. */
  loading: boolean;
  /** Error from the query, if any. */
  error: Error | null;
}

// =============================================================================
// Queries
// =============================================================================

/**
 * Query for a single thing by key or keyJSON.
 * Includes thingstateId for change detection but NOT triggerable.
 * Check triggerable status via useThingWatcher().triggerableKeys.
 */
const GET_THING = gql`
  query GetThing($key: String, $keyJSON: JSON, $tick: Int!) {
    thing(key: $key, keyJSON: $keyJSON, atTick: $tick) {
      shortId
      key
      keyJSON
      thingType
      thingstateId
      dataChunks {
        id
      }
      links {
        linkType
        linkKey
        linkKeyJSON
        toThing {
          shortId
          key
          keyJSON
          thingType
        }
      }
      backlinks {
        linkType
        linkKey
        linkKeyJSON
        fromThing {
          shortId
          key
          keyJSON
          thingType
        }
      }
      sources {
        kind
        storeName
        overridden
      }
    }
  }
`;

/**
 * Query for a single data chunk by ID.
 * Used to resolve chunk data - graphcache caches these by ID.
 */
const GET_DATA_CHUNK = gql`
  query GetDataChunk($id: String!) {
    dataChunk(id: $id) {
      id
      data
    }
  }
`;

// =============================================================================
// Hook
// =============================================================================

/**
 * Hook to fetch a single thing by key or keyJSON.
 *
 * Uses the current tick from ThingWatcherProvider by default,
 * or a specific tick if provided.
 *
 * When `includeData` is true, chunk data is resolved in a second step.
 * This two-step process allows graphcache to serve cached chunks,
 * avoiding refetches when only new chunks are appended.
 *
 * Example:
 * ```typescript
 * // By b64 key (canonical identifier)
 * const { thing } = useThing({ keyInput: { key: 'base64encodedkey' } });
 *
 * // By JSON key
 * const { thing } = useThing({ keyInput: { keyJSON: { id: 123 } } });
 *
 * // At a specific tick
 * const { thing } = useThing({ keyInput: { key: '...' }, tick: 5 });
 *
 * // With full data for rendering
 * const { thing } = useThing({ keyInput: { key: '...' }, includeData: true });
 * ```
 */
export function useThing(options: UseThingOptions): UseThingResult {
  const { keyInput, tick, pause = false, includeData = false } = options;
  const { resolvedTick } = useThingWatcher();
  const client = useClient();

  const effectiveTick = tick ?? resolvedTick;

  // Build variables based on which key type was provided
  // Only include one of key/keyJSON, not both (server rejects if both are present, even if one is null)
  const variables = 'key' in keyInput
    ? { key: keyInput.key, tick: effectiveTick }
    : { keyJSON: keyInput.keyJSON, tick: effectiveTick };

  // Step 1: Query thing with chunk IDs
  const [thingResult] = useQuery({
    query: GET_THING,
    variables,
    pause: pause || effectiveTick === 0,
    requestPolicy: 'cache-and-network',
  });

  const baseThing = thingResult.data?.thing as Thing | undefined;

  // Step 2: Resolve chunk data when includeData is true
  const [resolvedChunks, setResolvedChunks] = useState<Array<{ id: string; data: unknown }>>([]);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [chunksError, setChunksError] = useState<Error | null>(null);

  // Track which chunk IDs we've resolved to avoid unnecessary refetches
  const chunkIds = useMemo(() => {
    if (!baseThing?.dataChunks) return [];
    return baseThing.dataChunks.map(c => c.id);
  }, [baseThing?.dataChunks]);

  useEffect(() => {
    if (!includeData || chunkIds.length === 0) {
      setResolvedChunks([]);
      setChunksLoading(false);
      setChunksError(null);
      return;
    }

    let cancelled = false;
    setChunksLoading(true);
    setChunksError(null);

    // Fetch all chunks in parallel - graphcache serves cached ones instantly
    Promise.all(
      chunkIds.map(id =>
        client.query(GET_DATA_CHUNK, { id }).toPromise()
      )
    )
      .then(results => {
        if (cancelled) return;

        const chunks = results.map((result, i) => ({
          id: chunkIds[i],
          data: result.data?.dataChunk?.data ?? null,
        }));

        setResolvedChunks(chunks);
        setChunksLoading(false);
      })
      .catch(err => {
        if (cancelled) return;
        setChunksError(err);
        setChunksLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [client, includeData, chunkIds]);

  // Build final thing with resolved data
  const thing = useMemo((): Thing | null => {
    if (!baseThing) return null;

    if (!includeData) {
      // Return as-is without data field
      return baseThing;
    }

    // Add resolved chunk data
    return {
      ...baseThing,
      data: resolvedChunks[0]?.data ?? null,
      dataChunks: resolvedChunks,
    };
  }, [baseThing, includeData, resolvedChunks]);

  return {
    thing,
    loading: thingResult.fetching || chunksLoading,
    error: thingResult.error ?? chunksError ?? null,
  };
}
