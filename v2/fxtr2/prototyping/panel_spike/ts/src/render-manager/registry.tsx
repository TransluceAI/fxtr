/**
 * Renderer registry context and hooks.
 *
 * NOTE: Eventually the registry will be dynamically populated when entity types
 * are requested. For now, it is hardcoded and provided via React context.
 */

import { createContext, useContext } from 'react';
import type { EntityType } from '../data/schema';
import type {
  RendererRegistry,
  FullRendererProps,
  RefRendererProps,
  InlineRefRendererProps,
  PreviewRendererProps,
} from '../renderers/types';

// =============================================================================
// RendererRegistryContext
// =============================================================================

/**
 * Context for accessing the renderer registry.
 * Provided by RenderRoot and used by EntityRenderer, RefLink, etc.
 *
 * NOTE: Currently hardcoded. In the future, this will support dynamic
 * loading of renderers when entity types are first encountered.
 */
export const RendererRegistryContext = createContext<RendererRegistry | null>(null);

/**
 * Hook to access the renderer registry.
 * Must be used within a RendererRegistryContext.Provider.
 */
export function useRendererRegistry(): RendererRegistry {
  const registry = useContext(RendererRegistryContext);
  if (!registry) {
    throw new Error(
      'useRendererRegistry must be used within RendererRegistryContext.Provider'
    );
  }
  return registry;
}

// =============================================================================
// useRegisteredEntityRenderer hook
// =============================================================================

/**
 * Get the registered renderer for an entity type.
 *
 * @param typename - The entity type to get a renderer for
 * @param rendertype - 'full' for card, 'ref' for block reference, 'inline' for inline reference, 'preview' for hover preview
 * @returns The React component for rendering that type
 */
export function useRegisteredEntityRenderer(
  typename: EntityType,
  rendertype: 'full'
): React.FC<FullRendererProps>;
export function useRegisteredEntityRenderer(
  typename: EntityType,
  rendertype: 'ref'
): React.FC<RefRendererProps>;
export function useRegisteredEntityRenderer(
  typename: EntityType,
  rendertype: 'inline'
): React.FC<InlineRefRendererProps>;
export function useRegisteredEntityRenderer(
  typename: EntityType,
  rendertype: 'preview'
): React.FC<PreviewRendererProps>;
export function useRegisteredEntityRenderer(
  typename: EntityType,
  rendertype: 'full' | 'ref' | 'inline' | 'preview'
): React.FC<FullRendererProps> | React.FC<RefRendererProps> | React.FC<InlineRefRendererProps> | React.FC<PreviewRendererProps> {
  const registry = useRendererRegistry();
  const entry = registry[typename];

  if (!entry) {
    // Return a fallback renderer for unknown types
    if (rendertype === 'full') {
      return FallbackFullRenderer;
    } else if (rendertype === 'ref') {
      return FallbackRefRenderer;
    } else if (rendertype === 'inline') {
      return FallbackInlineRefRenderer;
    } else {
      return FallbackPreviewRenderer;
    }
  }

  if (rendertype === 'full') {
    return entry.full;
  } else if (rendertype === 'ref') {
    return entry.ref;
  } else if (rendertype === 'inline') {
    return entry.inline;
  } else {
    return entry.preview;
  }
}

// =============================================================================
// Fallback Renderers
// =============================================================================

/**
 * Fallback full renderer for unknown entity types.
 */
function FallbackFullRenderer({ entityId }: FullRendererProps) {
  return (
    <div
      style={{
        border: '2px solid #888',
        borderRadius: 4,
        padding: 12,
        background: '#2a2a2a',
      }}
    >
      <div style={{ color: '#888' }}>
        Unknown type for entity: {entityId}
      </div>
    </div>
  );
}

/**
 * Fallback block ref renderer for unknown entity types.
 */
function FallbackRefRenderer({ entityId, isOwned }: RefRendererProps) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        padding: '4px 8px',
        background: '#2a2a2a',
        border: isOwned ? '1px solid #888' : 'none',
        textDecoration: isOwned ? 'none' : 'underline',
        borderRadius: 4,
        color: '#888',
      }}
    >
      {entityId}
    </div>
  );
}

/**
 * Fallback inline ref renderer for unknown entity types.
 */
function FallbackInlineRefRenderer({ entityId, isOwned }: InlineRefRendererProps) {
  return (
    <span
      style={{
        color: '#888',
        textDecoration: isOwned ? 'none' : 'underline',
        borderBottom: isOwned ? '1px solid #888' : 'none',
      }}
    >
      {entityId}
    </span>
  );
}

/**
 * Fallback preview renderer for unknown entity types.
 */
function FallbackPreviewRenderer({ entityId }: PreviewRendererProps) {
  return (
    <div
      style={{
        padding: 8,
        color: '#888',
      }}
    >
      Unknown type for entity: {entityId}
    </div>
  );
}
