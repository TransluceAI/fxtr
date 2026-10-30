/**
 * useAdditionalActiveThings - Fetches active things not in the snapshot
 *
 * This hook queries things that are "active" (have panels open, are pinned, etc.)
 * but are not already included in the snapshot things from useTickData.
 * These are typically things from immutable stores that aren't captured in the snapshot.
 *
 * Used to show store things in the Graph and ThingsList when they have open panels.
 */

import { useMemo } from 'react';
import { gql, useQuery } from 'urql';
import type { Thing } from '../types';
import { useActiveKeys } from './useActiveMarks';
import { useThingWatcher } from './ThingWatcherProvider';

// =============================================================================
// GraphQL Query
// =============================================================================

/**
 * Batch fetch things by keys.
 * Returns array with null for keys that don't resolve.
 */
const GET_THINGS_BY_KEYS = gql`
  query GetThingsByKeys($keys: [String!]!, $tick: Int!) {
    things(keys: $keys, atTick: $tick) {
      shortId
      key
      keyJSON
      thingType
      thingstateId
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
    }
  }
`;

// =============================================================================
// Types
// =============================================================================

export interface UseAdditionalActiveThingsOptions {
  /** Keys already in snapshot (will be excluded from query) */
  snapshotKeys: Set<string>;
}

export interface UseAdditionalActiveThingsResult {
  /** Additional things that resolved successfully */
  things: Thing[];
  /** Whether query is in progress */
  loading: boolean;
}

// =============================================================================
// Hook
// =============================================================================

/**
 * Fetches active things that are not in the snapshot.
 *
 * @param options.snapshotKeys - Set of keys already in snapshot (excluded from query)
 * @returns things - Additional things that resolved, loading - whether query is in progress
 */
export function useAdditionalActiveThings(
  options: UseAdditionalActiveThingsOptions
): UseAdditionalActiveThingsResult {
  const activeKeys = useActiveKeys();
  const { resolvedTick } = useThingWatcher();

  // Compute keys to query: active but not in snapshot
  // activeKeys is a Map<string, stability>, so we iterate over its keys
  const additionalKeys = useMemo(() => {
    return Array.from(activeKeys.keys()).filter(key => !options.snapshotKeys.has(key));
  }, [activeKeys, options.snapshotKeys]);

  // Query additional keys (skip if none)
  const [result] = useQuery({
    query: GET_THINGS_BY_KEYS,
    variables: { keys: additionalKeys, tick: resolvedTick },
    pause: additionalKeys.length === 0,
  });

  // Filter out nulls (keys that didn't resolve)
  // Important: also check additionalKeys.length because when paused, result.data keeps stale data
  const things = useMemo(() => {
    if (additionalKeys.length === 0) return [];
    if (!result.data?.things) return [];
    return result.data.things.filter((t: Thing | null): t is Thing => t !== null);
  }, [result.data, additionalKeys.length]);

  return {
    things,
    loading: result.fetching && additionalKeys.length > 0,
  };
}
