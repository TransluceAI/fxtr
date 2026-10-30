import { useCallback } from 'react';
import { useQuery } from 'urql';
import { gql } from '@urql/core';
import type { ThingTypeConfig } from '../types';
import { useOnRendererRefresh } from './useRendererRefresh';

// =============================================================================
// Single type config query
// =============================================================================

const GET_THING_TYPE_CONFIG = gql`
  query GetThingTypeConfig($thingType: String!) {
    thingTypeConfig(thingType: $thingType) {
      thingType
      color
      linkColors {
        linkType
        color
      }
      renderer {
        moduleId
        rendererKey
      }
    }
  }
`;

export interface UseThingTypeConfigResult {
  config: ThingTypeConfig | null;
  loading: boolean;
  error: Error | null;
}

/**
 * Hook to fetch rendering config for a specific thing type.
 *
 * Uses cache-and-network to always refetch in the background, ensuring
 * fresh data when files change on disk. Listens for refresh signals to
 * force an immediate network refetch.
 *
 * Example:
 * ```typescript
 * const { config } = useThingTypeConfig('Task');
 * if (config) {
 *   console.log('Task color:', config.color);
 *   console.log('Has custom renderer:', !!config.renderer);
 * }
 * ```
 */
export function useThingTypeConfig(thingType: string): UseThingTypeConfigResult {
  const [result, reexecute] = useQuery({
    query: GET_THING_TYPE_CONFIG,
    variables: { thingType },
    pause: !thingType,
    requestPolicy: 'cache-and-network',
  });

  // Re-execute query when refresh is triggered
  const handleRefresh = useCallback(() => {
    reexecute({ requestPolicy: 'network-only' });
  }, [reexecute]);
  useOnRendererRefresh(handleRefresh);

  return {
    config: (result.data?.thingTypeConfig as ThingTypeConfig | undefined) ?? null,
    loading: result.fetching,
    error: result.error ?? null,
  };
}
