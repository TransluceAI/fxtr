import { useQuery } from 'urql';
import { GET_TRIGGER_RECORDS } from '../graphql/queries';
import type { TriggerRecord } from '../types';

// =============================================================================
// Types
// =============================================================================

export interface UseTriggerRecordsOptions {
  /** Filter by tick range (from). */
  from?: number;
  /** Filter by tick range (to). */
  to?: number;
  /** Pause the query. */
  pause?: boolean;
}

export interface UseTriggerRecordsResult {
  /** All trigger records (possibly filtered). */
  records: TriggerRecord[];
  /** Whether the query is currently loading. */
  loading: boolean;
  /** Error from the query, if any. */
  error: Error | null;
}

// =============================================================================
// Hook
// =============================================================================

/**
 * Hook to fetch trigger records (trigger history).
 *
 * Example:
 * ```typescript
 * const { records } = useTriggerRecords();
 *
 * // Filter by tick range (server-side)
 * const { records } = useTriggerRecords({ from: 1, to: 10 });
 * ```
 */
export function useTriggerRecords(options: UseTriggerRecordsOptions = {}): UseTriggerRecordsResult {
  const { from, to, pause = false } = options;

  const [result] = useQuery({
    query: GET_TRIGGER_RECORDS,
    variables: { from, to },
    pause,
  });

  return {
    records: (result.data?.triggerRecords ?? []) as TriggerRecord[],
    loading: result.fetching,
    error: result.error ?? null,
  };
}

/**
 * Hook to get trigger records for a specific tick (server-side filtered).
 */
export function useTriggerRecordsAtTick(tickIndex: number): UseTriggerRecordsResult {
  return useTriggerRecords({ from: tickIndex, to: tickIndex });
}
