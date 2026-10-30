/**
 * Drag-and-drop constants and helpers for thingwatcher.
 *
 * Used by DraggableIDMarker, NodeRef, ThingNode, and DockviewLayout to coordinate
 * drag operations that create new panels or drop into custom drop regions.
 *
 * Drag data includes multiple MIME types for filtering:
 * - DRAG_MIME_THING: Always set, data is JSON metadata
 * - DRAG_MIME_THING_TYPE_PREFIX + thingType: For type-based filtering (no data)
 * - DRAG_MIME_THING_KEY_PREFIX + key: For specific-key filtering (no data)
 *
 * Breadcrumb integration:
 * - ThingDropInfo optionally includes pendingBreadcrumbs
 * - When dropping creates a panel, breadcrumbs are created for the navigation chain
 */

import type { PendingBreadcrumb } from '../hooks/useBreadcrumbSource';

/** MIME type for dragging things. The data is JSON-encoded ThingDropInfo. */
export const DRAG_MIME_THING = 'application/x-thingwatcher-thing';

/** MIME type prefix for filtering by thing type. Append hex-encoded thingType to get full MIME. */
export const DRAG_MIME_THING_TYPE_PREFIX = 'application/x-thingwatcher-thing-type/';

/** MIME type prefix for filtering by specific key. Append hex-encoded key to get full MIME. */
export const DRAG_MIME_THING_KEY_PREFIX = 'application/x-thingwatcher-thing-key/';

/**
 * Hex-encode a string for use in MIME types.
 * Browsers lowercase MIME types, so we encode to preserve case sensitivity.
 */
export function hexEncode(str: string): string {
  return Array.from(str)
    .map((c) => c.charCodeAt(0).toString(16).padStart(2, '0'))
    .join('');
}

/**
 * Decode a hex-encoded string.
 */
export function hexDecode(hex: string): string {
  const chars: string[] = [];
  for (let i = 0; i < hex.length; i += 2) {
    chars.push(String.fromCharCode(parseInt(hex.slice(i, i + 2), 16)));
  }
  return chars.join('');
}

/**
 * Metadata about a dragged/dropped thing.
 */
export interface ThingDropInfo {
  /** Base64-encoded CBOR key */
  key: string;
  /** JSON-deserialized key, or null if not JSON-serializable */
  keyJSON: unknown;
  /** Short numeric ID for display */
  shortId: number;
  /** Thing type string */
  thingType: string;
  /** Breadcrumbs to create when this thing is dropped into a panel (optional) */
  pendingBreadcrumbs?: PendingBreadcrumb[];
}

/**
 * Build the list of MIME types to set during drag.
 * Returns an array of [mimeType, data] pairs.
 *
 * Note: Browsers lowercase MIME types automatically, so we hex-encode
 * thingType and key to preserve case sensitivity.
 */
export function buildThingDragData(info: ThingDropInfo): Array<[string, string]> {
  return [
    // Main MIME type with full metadata
    [DRAG_MIME_THING, JSON.stringify(info)],
    // Type-based filtering MIME (no data needed, just presence)
    // Hex-encoded because browsers lowercase MIME types
    [DRAG_MIME_THING_TYPE_PREFIX + hexEncode(info.thingType), ''],
    // Key-based filtering MIME (no data needed, just presence)
    // Hex-encoded because browsers lowercase MIME types
    [DRAG_MIME_THING_KEY_PREFIX + hexEncode(info.key), ''],
    // Plain text fallback (keyJSON, marked in a sentinel)
    ['text/plain', info.keyJSON ? ("<tw-k>" +JSON.stringify(info.keyJSON) + "</tw-k>") : ""],
  ];
}

/**
 * Set all thing drag data on a DataTransfer object.
 */
export function setThingDragData(dataTransfer: DataTransfer, info: ThingDropInfo): void {
  const entries = buildThingDragData(info);
  for (const [mimeType, data] of entries) {
    dataTransfer.setData(mimeType, data);
  }
  dataTransfer.effectAllowed = 'copy';
}

/**
 * Extract ThingDropInfo from a DataTransfer, or null if not a thing drag.
 */
export function parseThingDragData(dataTransfer: DataTransfer): ThingDropInfo | null {
  const data = dataTransfer.getData(DRAG_MIME_THING);
  if (!data) return null;

  try {
    const parsed = JSON.parse(data);
    // Validate required fields
    if (
      typeof parsed.key === 'string' &&
      typeof parsed.shortId === 'number' &&
      typeof parsed.thingType === 'string'
    ) {
      return parsed as ThingDropInfo;
    }
  } catch {
    // Invalid JSON, fall through
  }

  return null;
}

/**
 * Check if a DataTransfer contains thing drag data.
 * Can be used during dragover when getData() isn't available.
 */
export function hasThingDragData(dataTransfer: DataTransfer): boolean {
  return dataTransfer.types.includes(DRAG_MIME_THING);
}

/**
 * Check if a DataTransfer contains a thing of a specific type.
 * Can be used during dragover for filtering.
 */
export function hasThingOfType(dataTransfer: DataTransfer, thingType: string): boolean {
  return dataTransfer.types.includes(DRAG_MIME_THING_TYPE_PREFIX + hexEncode(thingType));
}

/**
 * Check if a DataTransfer contains a specific thing by key.
 * Can be used during dragover for filtering.
 */
export function hasSpecificThing(dataTransfer: DataTransfer, thingKey: string): boolean {
  return dataTransfer.types.includes(DRAG_MIME_THING_KEY_PREFIX + hexEncode(thingKey));
}
