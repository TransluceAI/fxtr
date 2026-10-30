/**
 * Handle Factory - Creates opaque handles for custom renderers
 *
 * Handles are opaque objects that provide hook methods for accessing thing data.
 * Each handle type (Full, Ref, Preview) supports a different subset of hooks.
 *
 * Handle Creation Pattern:
 * - Handles are created in the component that renders the custom renderer
 * - The handle captures the thing key and metadata in closures
 * - When the renderer calls hooks (usePayload, useLinks, etc.), the hooks
 *   delegate to internal methods on the handle which call React hooks
 *
 * This means:
 * - useThing, useThingWatcher, etc. are called from within the renderer component
 * - State subscriptions happen lazily when the renderer uses hooks
 * - Multiple calls to the same hook with same options share the underlying data
 */

import React, { useCallback, useMemo, useSyncExternalStore } from 'react';
import type { FC, ReactNode } from 'react';
import { useThingWatcher, useThing, useTriggerThing } from '../hooks';
import { useTriggerRecordsAtTick } from '../hooks/useTriggerRecords';
import { persistentStateStore } from './persistentStateStore';
import { createDraggableIDMarker } from './DraggableIDMarker';
import type { TriggerResult, TriggerRecord } from '../../thingwatcher-bindings';
// Note: The internal TriggerRecord has more fields (id, thing, etc.) that are
// implementation details. We only expose triggerTick and payload to renderer authors.

// =============================================================================
// Brand Symbols (runtime)
// =============================================================================

// These symbols brand the handle types at runtime
export const fullRendererBrand = Symbol('fullRendererBrand');
export const refRendererBrand = Symbol('refRendererBrand');
export const previewRendererBrand = Symbol('previewRendererBrand');

// =============================================================================
// Types
// =============================================================================

/** Metadata passed to handle factory */
export interface HandleMetadata {
  key: string;
  keyJSON: unknown;
  shortId: number;
  thingType: string;
  moduleId: string;
}

/** Link info for useLinks return type */
export interface LinkInfo {
  linkType: string;
  linkKey: string;
  linkKeyJSON: unknown;
  toThingKey: string;
  toThingKeyJSON: unknown;
  toThingShortId: number;
  toThingType: string;
}

/**
 * Internal handle structure - contains all possible hook methods.
 * Only certain methods are present depending on handle type.
 */
export interface InternalHandle {
  // Brand - exactly one is true per handle instance
  [fullRendererBrand]?: true;
  [refRendererBrand]?: true;
  [previewRendererBrand]?: true;

  // Metadata (used by useThingMetadata)
  _metadata: HandleMetadata;

  // Hook methods - ALL data access goes through hooks (present on all handles)
  _usePayload: (opts?: { chunk?: 'first' | 'latest' }) => unknown;
  _useDataChunks: () => unknown[];
  _useLinks: () => LinkInfo[];

  // Optional hook methods - only present on certain handle types
  _useTriggerable?: () => [boolean, TriggerRecord | null];  // Full + Ref
  _useTrigger?: () => (payload: unknown) => Promise<TriggerResult>;  // Full only
  _useDraggableIdMarker?: () => FC<{ children: ReactNode }>;  // Full + Preview
  _usePersistentThingState?: <T>(key: string) => [T | undefined, (v: T | undefined | ((prev: T | undefined) => T | undefined)) => void];  // Full only
  _usePersistentGlobalState?: <T>(key: string) => [T | undefined, (v: T | undefined | ((prev: T | undefined) => T | undefined)) => void];  // Full only
}

// =============================================================================
// Opaque Handle Types (for type exports)
// =============================================================================

// These are the types exposed to renderers - they're branded interfaces
// that hide the internal structure
export interface FullRendererHandle {
  readonly [fullRendererBrand]: true;
}

export interface RefRendererHandle {
  readonly [refRendererBrand]: true;
}

export interface PreviewRendererHandle {
  readonly [previewRendererBrand]: true;
}

// =============================================================================
// Shared Hook Implementations
// =============================================================================

/**
 * Creates the _usePayload hook method.
 * This is a React hook that fetches thing data and returns payload.
 */
function createUsePayload(thingKey: string) {
  return (opts?: { chunk?: 'first' | 'latest' }) => {
    const { thing } = useThing({ keyInput: { key: thingKey }, includeData: true });
    if (opts?.chunk === 'latest') {
      const chunks = thing?.dataChunks ?? [];
      return chunks.length > 0 ? chunks[chunks.length - 1].data : undefined;
    }
    return thing?.data;
  };
}

/**
 * Creates the _useDataChunks hook method.
 */
function createUseDataChunks(thingKey: string) {
  return () => {
    const { thing } = useThing({ keyInput: { key: thingKey }, includeData: true });
    return (thing?.dataChunks ?? []).map(c => c.data);
  };
}

/**
 * Creates the _useLinks hook method.
 */
function createUseLinks(thingKey: string) {
  return (): LinkInfo[] => {
    const { thing } = useThing({ keyInput: { key: thingKey }, includeData: true });
    return (thing?.links ?? []).map((link) => ({
      linkType: link.linkType,
      linkKey: link.linkKey,
      linkKeyJSON: link.linkKeyJSON,
      toThingKey: link.toThing.key,
      toThingKeyJSON: link.toThing.keyJSON,
      toThingShortId: link.toThing.shortId,
      toThingType: link.toThing.thingType,
    }));
  };
}

/**
 * Creates the _useTriggerable hook method.
 * Returns [triggerable, triggerRecord] tuple.
 * The internal trigger record has more fields (id, thing, etc.) but we only
 * expose triggerTick and payload to renderer authors.
 */
function createUseTriggerable(thingKey: string) {
  return (): [boolean, TriggerRecord | null] => {
    const { triggerableKeys, isLive, resolvedTick } = useThingWatcher();
    const triggerable = isLive && triggerableKeys.has(thingKey);

    // Get trigger record at current tick (if any)
    const { records: triggerRecords } = useTriggerRecordsAtTick(resolvedTick);
    const internalRecord = triggerRecords.find(r => r.thing?.key === thingKey);

    // Map internal record to external shape (hiding implementation details)
    const triggerRecord: TriggerRecord | null = internalRecord
      ? { triggerTick: internalRecord.triggerTick, payload: internalRecord.payload }
      : null;

    return [triggerable, triggerRecord];
  };
}

/**
 * Creates the _useTrigger hook method.
 * Returns a memoized trigger function.
 */
function createUseTrigger(thingKey: string) {
  return () => {
    const [, triggerMutation] = useTriggerThing({ key: thingKey });
    return useCallback(
      (payload: unknown) => triggerMutation(payload),
      [triggerMutation]
    );
  };
}

/**
 * Creates the _useDraggableIdMarker hook method.
 * Returns a memoized DraggableIDMarker component.
 */
function createUseDraggableIdMarker(metadata: HandleMetadata) {
  return () => {
    return useMemo(() => createDraggableIDMarker(metadata), []);
  };
}

/**
 * Creates the _usePersistentThingState hook method.
 */
function createUsePersistentThingState(shortId: number) {
  return <T,>(key: string): [T | undefined, (v: T | undefined | ((prev: T | undefined) => T | undefined)) => void] => {
    const storageKey = `${shortId}:${key}`;

    const value = useSyncExternalStore(
      (onStoreChange) => persistentStateStore.subscribe(storageKey, onStoreChange),
      () => persistentStateStore.get<T>(storageKey),
    );

    const setValue = useCallback(
      (valueOrUpdater: T | undefined | ((prev: T | undefined) => T | undefined)) => {
        persistentStateStore.set(storageKey, valueOrUpdater);
      },
      [storageKey]
    );

    return [value, setValue];
  };
}

/**
 * Creates the _usePersistentGlobalState hook method.
 * Like _usePersistentThingState but keyed by moduleId instead of shortId,
 * so state is shared across all things rendered by the same module.
 */
function createUsePersistentGlobalState(moduleId: string) {
  return <T,>(key: string): [T | undefined, (v: T | undefined | ((prev: T | undefined) => T | undefined)) => void] => {
    const storageKey = `module:${moduleId}:${key}`;

    const value = useSyncExternalStore(
      (onStoreChange) => persistentStateStore.subscribe(storageKey, onStoreChange),
      () => persistentStateStore.get<T>(storageKey),
    );

    const setValue = useCallback(
      (valueOrUpdater: T | undefined | ((prev: T | undefined) => T | undefined)) => {
        persistentStateStore.set(storageKey, valueOrUpdater);
      },
      [storageKey]
    );

    return [value, setValue];
  };
}

// =============================================================================
// Handle Factory Functions
// =============================================================================

/**
 * Creates a FullRendererHandle with all hook methods.
 *
 * @param thingKey - The thing's key (base64)
 * @param metadata - Thing metadata (key, keyJSON, shortId, thingType)
 */
export function createFullRendererHandle(
  thingKey: string,
  metadata: HandleMetadata
): FullRendererHandle {
  const handle: InternalHandle = {
    [fullRendererBrand]: true,
    _metadata: metadata,
    _usePayload: createUsePayload(thingKey),
    _useDataChunks: createUseDataChunks(thingKey),
    _useLinks: createUseLinks(thingKey),
    _useTriggerable: createUseTriggerable(thingKey),
    _useTrigger: createUseTrigger(thingKey),
    _useDraggableIdMarker: createUseDraggableIdMarker(metadata),
    _usePersistentThingState: createUsePersistentThingState(metadata.shortId),
    _usePersistentGlobalState: createUsePersistentGlobalState(metadata.moduleId),
  };
  return handle as unknown as FullRendererHandle;
}

/**
 * Creates a RefRendererHandle for RefRenderer and InlineRefRenderer.
 * Supports: usePayload, useDataChunks, useLinks, useThingMetadata, useTriggerable
 *
 * @param thingKey - The thing's key (base64)
 * @param metadata - Thing metadata
 */
export function createRefRendererHandle(
  thingKey: string,
  metadata: HandleMetadata
): RefRendererHandle {
  const handle: InternalHandle = {
    [refRendererBrand]: true,
    _metadata: metadata,
    _usePayload: createUsePayload(thingKey),
    _useDataChunks: createUseDataChunks(thingKey),
    _useLinks: createUseLinks(thingKey),
    _useTriggerable: createUseTriggerable(thingKey),
    // No _useTrigger, _useDraggableIdMarker, _usePersistentThingState
  };
  return handle as unknown as RefRendererHandle;
}

/**
 * Creates a PreviewRendererHandle for PreviewRenderer.
 * Supports: usePayload, useDataChunks, useLinks, useThingMetadata, useDraggableIdMarker
 *
 * @param thingKey - The thing's key (base64)
 * @param metadata - Thing metadata
 */
export function createPreviewRendererHandle(
  thingKey: string,
  metadata: HandleMetadata
): PreviewRendererHandle {
  const handle: InternalHandle = {
    [previewRendererBrand]: true,
    _metadata: metadata,
    _usePayload: createUsePayload(thingKey),
    _useDataChunks: createUseDataChunks(thingKey),
    _useLinks: createUseLinks(thingKey),
    _useDraggableIdMarker: createUseDraggableIdMarker(metadata),
    // No _useTriggerable, _useTrigger, _usePersistentThingState
  };
  return handle as unknown as PreviewRendererHandle;
}
