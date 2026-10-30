import { useCallback } from 'react';
import { useMutation } from 'urql';
import { gql } from '@urql/core';
import type { Tick, TriggerResult } from '../types';
import type { ThingKeyInput } from './useThing';

// =============================================================================
// Mutations
// =============================================================================

/**
 * Mutation for triggering a paused thing.
 * Private - use useTriggerThing hook instead.
 */
const TRIGGER_PAUSE = gql`
  mutation TriggerPause($key: String, $keyJSON: JSON, $payload: JSON) {
    triggerPause(key: $key, keyJSON: $keyJSON, payload: $payload) {
      ok
      triggerId
      error
    }
  }
`;

const TRIGGER_TICK_MUTATION = gql`
  mutation TriggerTick($level: String, $desc: String) {
    triggerTick(level: $level, desc: $desc) {
      index
      timestamp
      level
      type
      desc
    }
  }
`;

const HARD_RECAPTURE_MUTATION = gql`
  mutation HardRecapture {
    hardRecapture {
      index
      timestamp
      level
      type
      desc
    }
  }
`;

// =============================================================================
// Hooks
// =============================================================================

/**
 * Hook to trigger a specific thing.
 *
 * Example:
 * ```typescript
 * const [triggering, trigger] = useTriggerThing({ key: 'base64key' });
 *
 * const result = await trigger({ action: 'approve' });
 * if (result.ok) {
 *   console.log('Triggered!', result.triggerId);
 * }
 * ```
 */
export function useTriggerThing(
  keyInput: ThingKeyInput
): [boolean, (payload?: unknown) => Promise<TriggerResult>] {
  const [state, execute] = useMutation(TRIGGER_PAUSE);

  const trigger = useCallback(
    async (payload?: unknown): Promise<TriggerResult> => {
      // Only include one of key/keyJSON, not both (server rejects if both are present, even if one is null)
      const variables = 'key' in keyInput
        ? { key: keyInput.key, payload }
        : { keyJSON: keyInput.keyJSON, payload };

      const result = await execute(variables);

      if (result.error) {
        return { ok: false, error: result.error.message };
      }

      return result.data?.triggerPause as TriggerResult ?? { ok: false, error: 'No data returned' };
    },
    [execute, keyInput]
  );

  return [state.fetching, trigger];
}

/**
 * Hook to trigger a manual tick.
 *
 * Example:
 * ```typescript
 * const [triggering, triggerTick] = useTriggerTick();
 *
 * const tick = await triggerTick('info', 'Manual checkpoint');
 * ```
 */
export function useTriggerTick(): [boolean, (level?: string, desc?: string) => Promise<Tick>] {
  const [state, execute] = useMutation(TRIGGER_TICK_MUTATION);

  const trigger = useCallback(
    async (level?: string, desc?: string): Promise<Tick> => {
      const result = await execute({ level, desc });

      if (result.error) {
        throw new Error(result.error.message);
      }

      return result.data?.triggerTick as Tick;
    },
    [execute]
  );

  return [state.fetching, trigger];
}

/**
 * Hook to trigger a hard recapture - reset all state and take fresh snapshot.
 *
 * Example:
 * ```typescript
 * const [recapturing, hardRecapture] = useHardRecapture();
 *
 * const tick = await hardRecapture();
 * ```
 */
export function useHardRecapture(): [boolean, () => Promise<Tick>] {
  const [state, execute] = useMutation(HARD_RECAPTURE_MUTATION);

  const recapture = useCallback(
    async (): Promise<Tick> => {
      const result = await execute({});

      if (result.error) {
        throw new Error(result.error.message);
      }

      return result.data?.hardRecapture as Tick;
    },
    [execute]
  );

  return [state.fetching, recapture];
}
