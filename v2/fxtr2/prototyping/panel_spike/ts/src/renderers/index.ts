/**
 * Renderer registry index.
 *
 * This file builds the default renderer registry by importing all type-specific
 * renderers and combining them into a single registry object.
 *
 * NOTE: Currently all renderers are statically imported. In the future, this
 * could be replaced with dynamic imports when entity types are first encountered.
 */

import type { RendererRegistry } from './types';
import { collection } from './by_entity_type/collection';
import { transcript } from './by_entity_type/transcript';
import { turn } from './by_entity_type/turn';
import { judgement } from './by_entity_type/judgement';
import { judgementSet } from './by_entity_type/judgement-set';
import { rubric } from './by_entity_type/rubric';
import { misconfigured } from './by_entity_type/misconfigured';

/**
 * The default renderer registry with all built-in entity type renderers.
 *
 * NOTE: This is currently hardcoded. Eventually these will be dynamically
 * fetched/loaded when entity types are first encountered.
 */
export const defaultRegistry: RendererRegistry = {
  collection,
  transcript,
  turn,
  judgement,
  judgement_set: judgementSet,
  rubric,
  misconfigured,
};

// Re-export types for convenience
export type { RendererRegistry, RendererEntry, FullRendererProps, RefRendererProps, InlineRefRendererProps, PreviewRendererProps } from './types';
