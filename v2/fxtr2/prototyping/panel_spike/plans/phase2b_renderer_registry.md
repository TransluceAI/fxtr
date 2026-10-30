
# Goals

Implement the renderer registry system. There should be a registry that manages the renderers for different types in different contexts.

The `EntityRenderer`, `Child`, and `RefLink` components should use a new hook `useRegisteredEntityRenderer(typename, rendertype)` to get a function component for that type in that context, where rendertype can be "full" or "ref" for now. (Child should either use full, if inlined, or make a placeholder that includes the reflink version.)

The signature of the resulting component should match what is in the spec for "full" or "ref" renderings.

The existing entity rendering logic in EntityRenderer should be transformed into individual files that register "full" and "ref" renderers for their corresponding types.

Internally, the `useRegisteredEntityRenderer` should use a React context to fetch from the registry. The RenderRoot should hardcode the mapping of types to their renderers and provide it to this context. (Please add comments for this saying that eventually these will be dynamically fetche when requested, but for now they are hardcoded via a context.)

For now, we don't have to include the "preview" rendering mode; this will be added later.


# Implementation plan

## Types

```typescript
// In src/renderers/types.ts
interface FullRendererProps {
  entityId: EntityId;
  data: DataBundle;
  isTopLevel: boolean;
  customArgs?: Record<string, unknown>;
}

interface RefRendererProps {
  entityId: EntityId;
  data: DataBundle;
  isOwned: boolean;      // rendering owned child vs unowned ref
  expandToFill: boolean; // fill space vs inline
  customArgs?: Record<string, unknown>;
}

type RendererEntry = {
  full: React.FC<FullRendererProps>;
  ref: React.FC<RefRendererProps>;
};

type RendererRegistry = Record<EntityType, RendererEntry>;
```

## New Files

1. **`src/renderers/types.ts`** - Shared prop types and registry type
2. **`src/renderers/helpers.ts`** - Shared utilities (getTypeColor, etc.)
3. **`src/renderers/by_entity_type/{type}.tsx`** - One file per entity type (collection, transcript, turn, judgement, judgement-set, rubric, misconfigured), each exporting `{ full, ref }` renderers
4. **`src/renderers/index.ts`** - Builds registry by importing all type files, exports `defaultRegistry`
5. **`src/render-manager/registry.tsx`** - `RendererRegistryContext` and `useRegisteredEntityRenderer` hook

## File Changes

1. **`RenderManager.tsx`**
   - Import `RendererRegistryContext` and `defaultRegistry`
   - Wrap children with `<RendererRegistryContext.Provider value={defaultRegistry}>`
   - Add comment: "Eventually these will be dynamically fetched; hardcoded via context for now"
   - Pass `isTopLevel={view.panelId !== null}` to EntityRenderer (panel-owned = top-level, slot-owned = nested)

2. **`EntityRenderer.tsx`**
   - Add `isTopLevel` prop (passed from RenderManager based on `view.panelId !== null`)
   - Use `useRegisteredEntityRenderer(entity.type, 'full')` to get full renderer
   - Keep highlight/viewId wrapper
   - Forward `isTopLevel` and `customArgs` to the full renderer
   - Remove all `{Type}Content` functions (moved to separate files)

3. **`RefLink`**
   - Add `isOwned?: boolean` prop (defaults to false)
   - Add `customArgs?: Record<string, unknown>` prop
   - Use `useDataBundle()` to get entity type from entityId
   - Use `useRegisteredEntityRenderer(type, 'ref')` to get ref renderer
   - Wrap ref renderer output with click/drag/context-menu behavior
   - Pass `isOwned`, `expandToFill={false}`, and `customArgs` to ref renderer

4. **`Child`**
   - Add `customArgs?: Record<string, unknown>` prop
   - Forward `customArgs` via OutPortal props (OutPortal forwards props to InPortal's child)

## Renderer File Structure

Each `src/renderers/by_entity_type/{type}.tsx`:
```typescript
import { FullRendererProps, RefRendererProps } from '../types';
import { getTypeColor } from '../helpers';
import { Child } from '../../render-manager/Child';
// Import RefLink for showing refs

export function CollectionFull({ entityId, data, isTopLevel, customArgs }: FullRendererProps) {
  // Card wrapper with border, type header
  // Content using <Child> for owned entities
}

export function CollectionRef({ entityId, data, isOwned, expandToFill, customArgs }: RefRendererProps) {
  // Single-line display: type color indicator + truncated ID
  // Brief content preview ONLY if expandToFill is true
}

export const collection = { full: CollectionFull, ref: CollectionRef };
```

## Hook Signature

```typescript
// Overloaded for type safety
function useRegisteredEntityRenderer(typename: EntityType, rendertype: 'full'): React.FC<FullRendererProps>;
function useRegisteredEntityRenderer(typename: EntityType, rendertype: 'ref'): React.FC<RefRendererProps>;
```

## Notes

- `isTopLevel` is determined by whether the view is panel-owned (`view.panelId !== null`) vs slot-owned (`view.ownerSlotId !== null`)
- The full renderer handles the complete card (border, header, content) - EntityRenderer only handles highlight wrapper
- RefLink wraps the ref renderer with click/drag/context-menu behavior
- Ref renderers show: type color indicator + truncated ID; content preview only when `expandToFill=true`
- OutPortal can forward props to the component rendered in InPortal, so customArgs flows through Child → OutPortal → EntityRenderer
