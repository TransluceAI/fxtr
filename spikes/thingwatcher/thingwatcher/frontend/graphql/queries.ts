/**
 * Shared GraphQL queries used by multiple hooks.
 *
 * Note: Most queries have been moved into their respective hook files.
 * Only shared queries that are used by multiple hooks remain here.
 * Components should NOT import from this file - use hooks instead.
 */

import { gql } from '@urql/core';

// =============================================================================
// Shared Queries (used by multiple hooks)
// =============================================================================

export const GET_LATEST_TICK = gql`
  query GetLatestTick {
    latestTick {
      index
      timestamp
      level
      type
      desc
    }
  }
`;

export const GET_TRIGGERABLE_KEYS = gql`
  query GetTriggerableKeys {
    triggerableKeys
  }
`;

export const GET_TICK = gql`
  query GetTick($index: Int!) {
    tick(index: $index) {
      index
      timestamp
      level
      type
      desc
    }
  }
`;

export const GET_TICKS = gql`
  query GetTicks($from: Int, $to: Int, $levels: [String!]) {
    ticks(from: $from, to: $to, levels: $levels) {
      index
      timestamp
      level
      desc
    }
  }
`;

export const GET_TRIGGER_RECORDS = gql`
  query GetTriggerRecords($from: Int, $to: Int) {
    triggerRecords(from: $from, to: $to) {
      id
      triggerTick
      thing {
        shortId
        key
      }
      payload
    }
  }
`;

export const GET_RENDERER_MODULE = gql`
  query GetRendererModule($id: String!) {
    rendererModule(id: $id) {
      id
      main
      imports
    }
  }
`;

// =============================================================================
// Subscriptions
// =============================================================================

export const NEW_TICK_SUBSCRIPTION = gql`
  subscription OnNewTick($includeStreaming: Boolean!) {
    newTick(includeStreamingUpdates: $includeStreaming) {
      tick {
        index
        timestamp
        level
        type
        desc
      }
      triggerableKeys
    }
  }
`;
