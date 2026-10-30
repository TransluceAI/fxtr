/**
 * Hooks for accessing ThingWatcher data.
 *
 * Hook Architecture:
 * - ThingWatcherProvider: Central context that manages tick state and subscription
 * - useThingWatcher: Access tick state (currentTick, resolvedTick, isLive, triggerableKeys)
 * - useThing: Fetch a single thing by key or keyJSON (with optional chunk data resolution)
 * - useTickData: Fetch things/changes/roots at a tick
 * - useTicks: Fetch tick list for timeline
 * - useLatestTick: Fetch latest tick info
 * - useTriggerThing: Trigger a specific thing
 * - useTriggerTick: Trigger a manual tick
 * - useTriggerRecords: Fetch trigger history
 * - useThingTypeConfig: Fetch rendering config for a specific thing type
 * - createGraphQLClient: Factory for urql client with graphcache
 *
 * Data Flow:
 * 1. ThingWatcherProvider subscribes to newTick
 * 2. graphcache subscription handler updates latestTick, triggerableKeys, ticks list
 * 3. Components use hooks which read from context + graphcache
 * 4. No manual refresh needed - data is automatically updated
 *
 * Triggerable status:
 * ```typescript
 * const { triggerableKeys, isLive } = useThingWatcher();
 * const isTriggerable = isLive && triggerableKeys.has(thing.key);
 * ```
 *
 * Fetching thing with data:
 * ```typescript
 * // For rendering (includes resolved chunk data)
 * const { thing } = useThing({ keyInput: { key: '...' }, includeData: true });
 * // thing.data = first chunk data
 * // thing.dataChunks = [{ id, data }, ...]
 *
 * // For simple lookups (no data, just IDs)
 * const { thing } = useThing({ keyInput: { key: '...' } });
 * ```
 */

// Provider and context
export {
  ThingWatcherProvider,
  useThingWatcher,
  type ThingWatcherContextValue,
} from './ThingWatcherProvider';

// Active marks (registry-based system for tracking active things)
export {
  ActiveMarksProvider,
  useActiveMarksRegistry,
  useRegisterActiveMark,
  useActiveKeys,
  useActiveMarks,
  useIsActive,
  useGetStability,
  type MarkSource,
  type ActiveMark,
  type ThingStability,
} from './useActiveMarks';

// Pinned things context (persistent marks + breadcrumbs)
export {
  PinnedThingsProvider,
  usePinnedThings,
  useBreadcrumbsFor,
  type PinnedThingsContextValue,
  type Breadcrumb,
} from './usePinnedThings';

// Breadcrumb source context (navigation tracking)
export {
  BreadcrumbSourceProvider,
  useBreadcrumbSource,
  type BreadcrumbSource,
  type BreadcrumbType,
  type PendingBreadcrumb,
} from './useBreadcrumbSource';

// Additional active things (for Graph/ThingsList)
export {
  useAdditionalActiveThings,
  type UseAdditionalActiveThingsOptions,
  type UseAdditionalActiveThingsResult,
} from './useAdditionalActiveThings';

// Data hooks
export { useThing, type ThingKeyInput, type UseThingOptions, type UseThingResult } from './useThing';
export {
  useLatestTick, type UseLatestTickResult,
  useTicks, type UseTicksOptions, type UseTicksResult,
  useTickData, type UseTickDataOptions, type UseTickDataResult,
} from './ticks';
export { useTriggerThing, useTriggerTick, useHardRecapture } from './useTrigger';
export {
  useTriggerRecords,
  useTriggerRecordsAtTick,
  type UseTriggerRecordsOptions,
  type UseTriggerRecordsResult,
} from './useTriggerRecords';
export {
  useThingTypeConfig,
  type UseThingTypeConfigResult,
} from './useRenderingConfig';

// Renderer refresh hooks
export {
  RendererRefreshProvider,
  useRendererRefreshTrigger,
  useOnRendererRefresh,
} from './useRendererRefresh';

// GraphQL client factory
export { createGraphQLClient, type CreateGraphQLClientOptions } from './useGraphQL';
