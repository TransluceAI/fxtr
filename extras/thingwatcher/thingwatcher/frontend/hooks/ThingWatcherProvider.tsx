import React, { createContext, useContext, useState, useCallback, useMemo } from 'react';
import { useQuery, useSubscription } from 'urql';
import { GET_LATEST_TICK, GET_TRIGGERABLE_KEYS, NEW_TICK_SUBSCRIPTION } from '../graphql/queries';
import type { Tick } from '../types';

// =============================================================================
// Types
// =============================================================================

/**
 * Context value provided by ThingWatcherProvider.
 */
export interface ThingWatcherContextValue {
  // Tick state (read by hooks)
  currentTick: number | 'live';  // 'live' means follow latest tick
  latestTick: number | null;

  // Computed from currentTick
  isLive: boolean;               // true when currentTick === 'live'
  resolvedTick: number;          // actual tick number (latestTick when live), 0 if no ticks

  // Subscription status
  isSubscriptionActive: boolean; // true when websocket is connected and listening

  // Triggerability (separate from Thing cache)
  triggerableKeys: Set<string>;  // b64 keys
  isTriggerableKeysLoading: boolean;

  // Actions
  setCurrentTick: (tick: number | 'live') => void;
}

const ThingWatcherContext = createContext<ThingWatcherContextValue | null>(null);

// =============================================================================
// ThingWatcherProvider
// =============================================================================

interface ThingWatcherProviderProps {
  children: React.ReactNode;
  /** Whether to include streaming updates in the subscription (default: true) */
  includeStreamingUpdates?: boolean;
  /** Whether the websocket is connected (from graphql-ws lifecycle callbacks) */
  isWsConnected?: boolean;
}

/**
 * Central provider for ThingWatcher state management.
 *
 * Handles:
 * - Subscription to newTick (graphcache handler updates the cache)
 * - Tick state management (currentTick, latestTick, isLive, resolvedTick)
 * - Triggerable keys from subscription payload
 *
 * Components use hooks (useThing, useTickData, etc.) which read from this context
 * and the graphcache-managed queries.
 */
export function ThingWatcherProvider({
  children,
  includeStreamingUpdates = true,
  isWsConnected = false,
}: ThingWatcherProviderProps) {
  // Current tick state - 'live' means follow latestTick
  const [currentTick, setCurrentTickState] = useState<number | 'live'>('live');

  // Query for latest tick (will be updated by subscription handler via graphcache)
  const [latestTickResult] = useQuery({
    query: GET_LATEST_TICK,
  });

  // Query for triggerable keys (will be updated by subscription handler via graphcache)
  const [triggerableKeysResult] = useQuery({
    query: GET_TRIGGERABLE_KEYS,
  });

  // Subscribe to new ticks - graphcache handler does the cache updates
  // We just need the subscription active, we don't need to do anything with the result
  useSubscription({
    query: NEW_TICK_SUBSCRIPTION,
    variables: { includeStreaming: includeStreamingUpdates },
  });

  // Connection status comes from graphql-ws lifecycle callbacks via prop
  const isSubscriptionActive = isWsConnected;

  // Extract data
  const latestTick = (latestTickResult.data?.latestTick as Tick | undefined)?.index ?? null;

  const triggerableKeysArray = (triggerableKeysResult.data?.triggerableKeys ?? []) as string[];
  const triggerableKeys = useMemo(
    () => new Set(triggerableKeysArray),
    [triggerableKeysArray]
  );

  // Computed values
  const isLive = currentTick === 'live';
  const resolvedTick = isLive ? (latestTick ?? 0) : currentTick;

  // Action to set current tick
  const setCurrentTick = useCallback((tick: number | 'live') => {
    setCurrentTickState(tick);
  }, []);

  const value: ThingWatcherContextValue = {
    currentTick,
    latestTick,
    isLive,
    resolvedTick,
    triggerableKeys,
    isTriggerableKeysLoading: triggerableKeysResult.fetching,
    isSubscriptionActive,
    setCurrentTick,
  };

  return (
    <ThingWatcherContext.Provider value={value}>
      {children}
    </ThingWatcherContext.Provider>
  );
}

// =============================================================================
// Hooks
// =============================================================================

/**
 * Access the ThingWatcher context.
 * Must be used within a ThingWatcherProvider.
 *
 * Components can check triggerable status via:
 * ```typescript
 * const { triggerableKeys, isLive } = useThingWatcher();
 * const isTriggerable = isLive && triggerableKeys.has(thing.key);
 * ```
 */
export function useThingWatcher(): ThingWatcherContextValue {
  const ctx = useContext(ThingWatcherContext);
  if (!ctx) {
    throw new Error('useThingWatcher must be used within a ThingWatcherProvider');
  }
  return ctx;
}
