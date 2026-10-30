import { useMemo } from 'react';
import { useQuery } from 'urql';
import { gql } from '@urql/core';
import { GET_LATEST_TICK, GET_TICKS } from '../graphql/queries';
import type { Tick, Thing, Root, ChangeInfo } from '../types';

// =============================================================================
// useLatestTick
// =============================================================================

export interface UseLatestTickResult {
  latestTick: Tick | null;
  loading: boolean;
  error: Error | null;
}

/**
 * Hook to fetch the latest tick info.
 * Auto-updated by graphcache subscription handler.
 */
export function useLatestTick(): UseLatestTickResult {
  const [result] = useQuery({ query: GET_LATEST_TICK });

  return {
    latestTick: (result.data?.latestTick as Tick | undefined) ?? null,
    loading: result.fetching,
    error: result.error ?? null,
  };
}

// =============================================================================
// useTicks
// =============================================================================

export interface UseTicksOptions {
  from?: number;
  to?: number;
  levels?: string[];
  pause?: boolean;
}

export interface UseTicksResult {
  ticks: Tick[];
  loading: boolean;
  error: Error | null;
}

/**
 * Hook to fetch the list of ticks for the timeline.
 * Auto-updated by graphcache subscription handler.
 */
export function useTicks(options: UseTicksOptions = {}): UseTicksResult {
  const { from, to, levels, pause = false } = options;

  const [result] = useQuery({
    query: GET_TICKS,
    variables: { from, to, levels },
    pause,
  });

  return {
    ticks: (result.data?.ticks ?? []) as Tick[],
    loading: result.fetching,
    error: result.error ?? null,
  };
}

// =============================================================================
// useTickData
// =============================================================================

/**
 * Query for tick data (things and roots).
 * Does NOT include renderingConfig - use useThingTypeConfig for that.
 */
const GET_TICK_DATA = gql`
  query GetTickData($tick: Int!) {
    tick(index: $tick) {
      index
      things {
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
          }
        }
      }
      roots {
        name
        thing {
          shortId
          key
          keyJSON
          thingType
        }
      }
    }
  }
`;

/**
 * Query for changes at current tick vs previous tick.
 * Returns added/modified things.
 *
 * NOTE: This MUST be a separate query document from GET_NEXT_TICK_CHANGES.
 * urql deduplicates/confuses multiple useQuery hooks that share the same
 * query document, causing operation.variables to be wrong. Different
 * operation names ensure urql treats them as distinct queries.
 */
const GET_PREV_TICK_CHANGES = gql`
  query GetPrevTickChanges($tick: Int!, $since: Int!) {
    tick(index: $tick) {
      index
      changes(since: $since) {
        kind
        thing {
          key
        }
      }
    }
  }
`;

/**
 * Query for changes at next tick vs current tick.
 * Returns things that will disappear.
 *
 * NOTE: See GET_PREV_TICK_CHANGES for why this must be separate.
 */
const GET_NEXT_TICK_CHANGES = gql`
  query GetNextTickChanges($tick: Int!, $since: Int!) {
    tick(index: $tick) {
      index
      changes(since: $since) {
        kind
        thing {
          key
        }
      }
    }
  }
`;

interface RawChange {
  kind: 'ADDED' | 'MODIFIED' | 'REMOVED';
  thing: { key: string };
}

export interface UseTickDataOptions {
  tick: number;
  prevTick?: number;
  nextTick?: number;
  pause?: boolean;
}

export interface UseTickDataResult {
  things: Thing[];
  changeInfo: ChangeInfo;
  roots: Root[];
  loading: boolean;
  error: Error | null;
}

// Empty ChangeInfo to return when paused or loading
const EMPTY_CHANGE_INFO: ChangeInfo = {
  addedKeys: new Set(),
  modifiedKeys: new Set(),
  disappearingKeys: new Set(),
};

/**
 * Hook to fetch all things, change info, and roots at a tick.
 *
 * @param tick - Required: the tick to show
 * @param prevTick - Optional: for ADDED/MODIFIED detection (compares tick vs prevTick)
 * @param nextTick - Optional: for disappearing detection (finds REMOVED in nextTick)
 *
 * Note: Does NOT include renderingConfig. Use useThingTypeConfig for
 * rendering configuration per thing type.
 */
export function useTickData(options: UseTickDataOptions): UseTickDataResult {
  const { tick, prevTick, nextTick, pause = false } = options;

  // Query 1: Get things and roots at the current tick
  const [mainResult] = useQuery({
    query: GET_TICK_DATA,
    variables: { tick },
    pause: pause || tick === 0,
  });

  // Query 2: Get changes from prevTick (for ADDED/MODIFIED)
  const prevQueryVars = { tick, since: prevTick };
  const prevQueryPaused = pause || tick === 0 || prevTick === undefined;
  const [prevChangesResult] = useQuery({
    query: GET_PREV_TICK_CHANGES,
    variables: prevQueryVars,
    pause: prevQueryPaused,
  });

  // Query 3: Get changes at nextTick (to find things that will be REMOVED)
  const nextQueryVars = { tick: nextTick, since: tick };
  const nextQueryPaused = pause || tick === 0 || nextTick === undefined;
  const [nextChangesResult] = useQuery({
    query: GET_NEXT_TICK_CHANGES,
    variables: nextQueryVars,
    pause: nextQueryPaused,
  });

  // Debug: Log what we're passing vs what urql has
  // console.log('[useTickData] Query setup:', {
  //   prev: { varsWePassed: prevQueryVars, paused: prevQueryPaused, operationVars: prevChangesResult.operation?.variables },
  //   next: { varsWePassed: nextQueryVars, paused: nextQueryPaused, operationVars: nextChangesResult.operation?.variables },
  // });

  const things = (mainResult.data?.tick?.things ?? []) as Thing[];
  const roots = (mainResult.data?.tick?.roots ?? []) as Root[];

  // Build the three Sets from the change queries
  // IMPORTANT: We must validate that the returned data actually matches the requested tick.
  // When navigating to a new tick, the query variables change immediately, but
  // result.data may still contain stale data from the previous query until the
  // new query completes. Using stale data would cause incorrect badges to appear.
  const changeInfo = useMemo((): ChangeInfo => {
    // Debug logging to understand data flow
    const mainTickIndex = mainResult.data?.tick?.index;
    const prevDataTickIndex = prevChangesResult.data?.tick?.index;
    const nextDataTickIndex = nextChangesResult.data?.tick?.index;

    // console.log('[useTickData] Computing changeInfo:', {
    //   requested: { tick, prevTick, nextTick },
    //   dataTickIndices: { main: mainTickIndex, prev: prevDataTickIndex, next: nextDataTickIndex },
    //   fetching: { main: mainResult.fetching, prev: prevChangesResult.fetching, next: nextChangesResult.fetching },
    //   // What variables did urql actually use for each query?
    //   actualQueryVars: {
    //     prev: prevChangesResult.operation?.variables,
    //     next: nextChangesResult.operation?.variables,
    //   },
    //   // Is urql telling us this data is stale?
    //   stale: { prev: prevChangesResult.stale, next: nextChangesResult.stale },
    //   // Validation results
    //   prevValidation: prevTick !== undefined ? `data.tick.index(${prevDataTickIndex}) === tick(${tick}) ? ${prevDataTickIndex === tick}` : 'skipped (no prevTick)',
    //   nextValidation: nextTick !== undefined ? `data.tick.index(${nextDataTickIndex}) === nextTick(${nextTick}) ? ${nextDataTickIndex === nextTick}` : 'skipped (no nextTick)',
    // });

    if (pause || tick === 0) {
      // console.log('[useTickData] Returning EMPTY (pause or tick=0)');
      return EMPTY_CHANGE_INFO;
    }

    const addedKeys = new Set<string>();
    const modifiedKeys = new Set<string>();
    const disappearingKeys = new Set<string>();

    // Process changes from prevTick → tick (ADDED/MODIFIED)
    // Only use data if it matches the expected tick (not stale from previous query)
    if (prevTick !== undefined && prevChangesResult.data?.tick?.index === tick) {
      const prevChanges = (prevChangesResult.data?.tick?.changes ?? []) as RawChange[];
      // console.log('[useTickData] Processing prevChanges:', prevChanges.length, 'changes');
      for (const change of prevChanges) {
        if (change.kind === 'ADDED') {
          addedKeys.add(change.thing.key);
        } else if (change.kind === 'MODIFIED') {
          modifiedKeys.add(change.thing.key);
        }
        // Ignore REMOVED - we show disappearing from nextTick instead
      }
    } else if (prevTick !== undefined) {
      // console.log('[useTickData] SKIPPING prevChanges - data tick mismatch');
    }

    // Process changes from tick → nextTick (things that will be REMOVED become DISAPPEARING)
    // Only use data if it matches the expected nextTick (not stale from previous query)
    if (nextTick !== undefined && nextChangesResult.data?.tick?.index === nextTick) {
      const nextChanges = (nextChangesResult.data?.tick?.changes ?? []) as RawChange[];
      // console.log('[useTickData] Processing nextChanges:', nextChanges.length, 'changes');
      for (const change of nextChanges) {
        if (change.kind === 'REMOVED') {
          disappearingKeys.add(change.thing.key);
        }
      }
    } else if (nextTick !== undefined) {
      // console.log('[useTickData] SKIPPING nextChanges - data tick mismatch');
    }

    // console.log('[useTickData] Result:', {
    //   added: addedKeys.size,
    //   modified: modifiedKeys.size,
    //   disappearing: disappearingKeys.size,
    // });

    return { addedKeys, modifiedKeys, disappearingKeys };
  }, [pause, tick, prevTick, nextTick, mainResult.data, prevChangesResult.data, nextChangesResult.data]);

  const loading = mainResult.fetching || prevChangesResult.fetching || nextChangesResult.fetching;
  const error = mainResult.error ?? prevChangesResult.error ?? nextChangesResult.error ?? null;

  return {
    things,
    changeInfo,
    roots,
    loading,
    error,
  };
}
