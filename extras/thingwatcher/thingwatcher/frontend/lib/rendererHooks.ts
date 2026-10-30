/**
 * Renderer Hooks - Hooks for accessing thing data from custom renderers
 *
 * These hooks are thin wrappers that validate the handle type and delegate
 * to the internal hook methods on the handle. The actual React hook calls
 * happen inside the internal methods, which were created by the handle factory.
 *
 * Usage:
 * ```tsx
 * import { usePayload, useLinks, useDraggableIdMarker } from 'thingwatcher-bindings';
 *
 * export function FullRenderer({ handle }: FullRendererProps) {
 *   const payload = usePayload(handle);
 *   const links = useLinks(handle);
 *   const DraggableIDMarker = useDraggableIdMarker(handle);
 *   // ...
 * }
 * ```
 */

import type { FC, ReactNode } from 'react';
import {
  fullRendererBrand,
  refRendererBrand,
  previewRendererBrand,
  type InternalHandle,
  type FullRendererHandle,
  type RefRendererHandle,
  type PreviewRendererHandle,
  type LinkInfo,
  type HandleMetadata,
} from './handleFactory';
import type { TriggerResult, TriggerRecord } from '../../thingwatcher-bindings';

// =============================================================================
// Type Aliases
// =============================================================================

/** Any handle type - for hooks that work with all handles */
type AnyHandle = FullRendererHandle | RefRendererHandle | PreviewRendererHandle;

// =============================================================================
// Data Access Hooks
// =============================================================================

/**
 * Get the thing's payload data.
 *
 * @param handle - Any renderer handle
 * @param options - Optional: { chunk: 'first' | 'latest' }, defaults to 'first'
 * @returns The payload data
 */
export function usePayload<T = unknown>(
  handle: AnyHandle,
  options?: { chunk?: 'first' | 'latest' }
): T {
  const h = handle as unknown as InternalHandle;
  return h._usePayload(options) as T;
}

/**
 * Get all data chunks for the thing.
 *
 * @param handle - Any renderer handle
 * @returns Array of chunk data (oldest first)
 */
export function useDataChunks<T = unknown>(handle: AnyHandle): T[] {
  const h = handle as unknown as InternalHandle;
  return h._useDataChunks() as T[];
}

/**
 * Get links from this thing to others.
 *
 * @param handle - Any renderer handle
 * @returns Array of link info
 */
export function useLinks(handle: AnyHandle): LinkInfo[] {
  const h = handle as unknown as InternalHandle;
  return h._useLinks();
}

/**
 * Get thing metadata (key, shortId, thingType, keyJSON).
 *
 * @param handle - Any renderer handle
 * @returns Metadata object
 */
export function useThingMetadata<K = unknown>(
  handle: AnyHandle
): HandleMetadata & { keyJSON: K } {
  const h = handle as unknown as InternalHandle;
  return h._metadata as HandleMetadata & { keyJSON: K };
}

// =============================================================================
// Triggerable Hooks
// =============================================================================

/**
 * Get triggerable status and trigger record at current tick.
 * Available for FullRenderer and RefRenderer.
 *
 * @param handle - Full or Ref handle
 * @returns [triggerable, triggerRecord] tuple
 * @throws Error if called with wrong handle type
 */
export function useTriggerable(
  handle: FullRendererHandle | RefRendererHandle
): [triggerable: boolean, triggerRecord: TriggerRecord | null] {
  const h = handle as unknown as InternalHandle;
  if (!h[fullRendererBrand] && !h[refRendererBrand]) {
    throw new Error('useTriggerable requires FullRendererHandle or RefRendererHandle');
  }
  return h._useTriggerable!();
}

/**
 * Get the trigger function. FullRenderer only.
 *
 * @param handle - FullRendererHandle
 * @returns Trigger function
 * @throws Error if called with wrong handle type
 */
export function useTrigger<T = unknown>(
  handle: FullRendererHandle
): (payload: T) => Promise<TriggerResult> {
  const h = handle as unknown as InternalHandle;
  if (!h[fullRendererBrand]) {
    throw new Error('useTrigger requires a FullRendererHandle');
  }
  return h._useTrigger!() as (payload: T) => Promise<TriggerResult>;
}

// =============================================================================
// UI Hooks
// =============================================================================

/**
 * Get a component to wrap the draggable ID element.
 * Available for FullRenderer and PreviewRenderer.
 *
 * @param handle - Full or Preview handle
 * @returns DraggableIDMarker component
 * @throws Error if called with wrong handle type
 */
export function useDraggableIdMarker(
  handle: FullRendererHandle | PreviewRendererHandle
): FC<{ children: ReactNode }> {
  const h = handle as unknown as InternalHandle;
  if (!h[fullRendererBrand] && !h[previewRendererBrand]) {
    throw new Error('useDraggableIdMarker requires FullRendererHandle or PreviewRendererHandle');
  }
  return h._useDraggableIdMarker!();
}

/**
 * Hook for storing state that persists across component re-mounts.
 * State is keyed by thing's shortId + user-provided key.
 * FullRenderer only.
 *
 * @param handle - FullRendererHandle
 * @param key - User-provided key to namespace the state
 * @returns [value, setValue] tuple - setValue accepts direct value or updater function
 * @throws Error if called with wrong handle type
 */
export function usePersistentThingState<T>(
  handle: FullRendererHandle,
  key: string
): [T | undefined, (value: T | undefined | ((prev: T | undefined) => T | undefined)) => void] {
  const h = handle as unknown as InternalHandle;
  if (!h[fullRendererBrand]) {
    throw new Error('usePersistentThingState requires a FullRendererHandle');
  }
  return h._usePersistentThingState!<T>(key);
}

/**
 * Hook for storing state that persists across component re-mounts and is
 * shared across all things rendered by the same module.
 * State is keyed by moduleId + user-provided key.
 * FullRenderer only.
 *
 * @param handle - FullRendererHandle
 * @param key - User-provided key to namespace the state
 * @returns [value, setValue] tuple - setValue accepts direct value or updater function
 * @throws Error if called with wrong handle type
 */
export function usePersistentGlobalState<T>(
  handle: FullRendererHandle,
  key: string
): [T | undefined, (value: T | undefined | ((prev: T | undefined) => T | undefined)) => void] {
  const h = handle as unknown as InternalHandle;
  if (!h[fullRendererBrand]) {
    throw new Error('usePersistentGlobalState requires a FullRendererHandle');
  }
  return h._usePersistentGlobalState!<T>(key);
}

// =============================================================================
// Re-exports for convenience
// =============================================================================

export type {
  FullRendererHandle,
  RefRendererHandle,
  PreviewRendererHandle,
  LinkInfo,
  HandleMetadata,
} from './handleFactory';

export type {
  TriggerResult,
  TriggerRecord,
} from '../../thingwatcher-bindings';
