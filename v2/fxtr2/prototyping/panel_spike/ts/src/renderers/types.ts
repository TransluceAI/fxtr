/**
 * Type definitions for the renderer registry system.
 *
 * NOTE: Renderers should use the useEntityData(entityId) hook from contexts.tsx
 * to fetch entity data instead of receiving it as a prop.
 */

import type { EntityId, EntityType } from '../data/schema';

/**
 * Props for full (card) renderers.
 * Full renderers display the complete entity content in a card format.
 */
export interface FullRendererProps {
  entityId: EntityId;
  /** True if this is a top-level panel view, false if nested in a parent entity */
  isTopLevel: boolean;
  /** Optional custom arguments for renderer customization */
  customArgs?: Record<string, unknown>;
}

/**
 * Props for block ref renderers.
 * Block ref renderers display a one-line block reference that expands to fill space.
 * Shows full ID, type info, and content preview.
 * Owned refs get a colored border, non-owned refs get underline styling.
 */
export interface RefRendererProps {
  entityId: EntityId;
  /** True if rendering an owned child, false if an unowned reference */
  isOwned: boolean;
}

/**
 * Props for inline ref renderers.
 * Inline ref renderers display a minimal inline reference.
 * Shows only the ID with type-indicating styling.
 */
export interface InlineRefRendererProps {
  entityId: EntityId;
  /** True if rendering an owned child, false if an unowned reference */
  isOwned: boolean;
}

/**
 * Props for preview card renderers.
 * Preview renderers display a compact preview of the entity for hover cards.
 * Children should be rendered as RefLinks (not inline children).
 */
export interface PreviewRendererProps {
  entityId: EntityId;
}

/**
 * A renderer entry contains full, ref (block), inline, and preview renderers for a type.
 */
export interface RendererEntry {
  full: React.FC<FullRendererProps>;
  /** Block ref renderer - one line, expands to fill, shows content preview */
  ref: React.FC<RefRendererProps>;
  /** Inline ref renderer - minimal, inline display, ID + type styling only */
  inline: React.FC<InlineRefRendererProps>;
  /** Preview card renderer - compact preview for hover cards */
  preview: React.FC<PreviewRendererProps>;
}

/**
 * The renderer registry maps entity types to their renderers.
 */
export type RendererRegistry = Record<EntityType, RendererEntry>;
