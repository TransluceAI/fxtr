# Renderer Registry Implementation Notes

## Current State

`EntityRenderer.tsx` has:
- Generic wrapper (border, background, highlight, type header with DraggableID)
- Type-specific content via `{Type}Content` functions (CollectionContent, TurnContent, etc.)
- `RefLink` for showing clickable/draggable references

## Spec Requirements

**Full renderer props:**
- entityId, data
- isTopLevel boolean
- optional customArgs dict

**Ref renderer props:**
- entityId, data
- isOwned (owned child vs unowned ref)
- expandToFill (fill space vs inline)
- optional customArgs dict

## Key Design Decisions

### 1. What does `useRegisteredEntityRenderer` return?

Returns `React.FC<FullRendererProps>` or `React.FC<RefRendererProps>` depending on rendertype.

```typescript
function useRegisteredEntityRenderer(typename: EntityType, rendertype: 'full'): React.FC<FullRendererProps>;
function useRegisteredEntityRenderer(typename: EntityType, rendertype: 'ref'): React.FC<RefRendererProps>;
```

### 2. Separation of concerns for Full renderers

Option A: Full renderer handles entire card (border, header, content)
Option B: EntityRenderer keeps wrapper, full renderer is just content

**Decision: Option A** - Each full renderer handles its own complete card.
- Allows per-type customization of card styling
- Can share common styles via helper functions (getTypeColor, etc.)
- The current viewId/highlight logic stays in EntityRenderer which *calls* the full renderer

Wait, this is tricky. EntityRenderer currently:
1. Gets viewId from context
2. Uses useIsHighlighted(viewId)
3. Sets data-view-id attribute
4. Renders highlight glow

This needs to stay at the EntityRenderer level because:
- viewId comes from CurrentViewContext set by RenderManager
- data-view-id is needed for scrollIntoView in jumpToView
- highlight state is per-view, not per-entity

**Revised Decision:** EntityRenderer keeps the highlight/viewId wrapper, but delegates to full renderer for card content. The full renderer gets isTopLevel prop so it knows whether to render the outer border.

Actually, re-reading the task:
> The signature of the resulting component should match what is in the spec

So full renderer signature should match spec. But the spec doesn't mention highlight. So:
- EntityRenderer wraps with highlight/viewId handling
- Calls full renderer component for the actual card content
- Full renderer handles card styling (border, type header, content)

### 3. RefLink behavior separation

Current RefLink has:
- Click handler (openEntity)
- Drag handler
- Context menu
- Visual display (truncated ID)

The spec says ref renderer should show "ID + type + possibly truncated contents" and "clicking will open the linked element".

**Decision:**
- Ref renderer is the visual component (shows type, ID, maybe content preview)
- RefLink is a thin wrapper that uses ref renderer and adds click/drag/context menu
- This gives flexibility if we need ref renderer in other contexts

### 4. Where does RefLink get entity type?

Currently RefLink only has entityId. To call `useRegisteredEntityRenderer(typename, 'ref')`, it needs the typename.

Options:
- A: Pass typename as prop
- B: Use useDataBundle() to look up entity data and get type

**Decision: Option B** - Use useDataBundle() to look up. Keeps API simple (just entityId).

### 5. Registry structure

```typescript
type RenderType = 'full' | 'ref';

interface FullRendererProps {
  entityId: EntityId;
  data: DataBundle;
  isTopLevel: boolean;
  customArgs?: Record<string, unknown>;
}

interface RefRendererProps {
  entityId: EntityId;
  data: DataBundle;
  isOwned: boolean;
  expandToFill: boolean;
  customArgs?: Record<string, unknown>;
}

interface RendererEntry {
  full: React.FC<FullRendererProps>;
  ref: React.FC<RefRendererProps>;
}

type RendererRegistry = Record<EntityType, RendererEntry>;
```

### 6. File organization

```
src/render-manager/
  registry.tsx           # Context, hook, types
  EntityRenderer.tsx     # Modified to use registry
  Child.tsx              # Added customArgs prop
  ...

src/renderers/
  types.ts               # Shared types (FullRendererProps, RefRendererProps)
  helpers.ts             # Shared functions (getTypeColor)
  by_entity_type/        # Subfolder for type-specific renderers
    collection.tsx       # { full: CollectionFull, ref: CollectionRef }
    transcript.tsx
    turn.tsx
    judgement.tsx
    judgement-set.tsx
    rubric.tsx
    misconfigured.tsx
  index.ts               # Builds and exports the registry
```

## Questions resolved

1. **Should Child use the registry?**
   - Child renders OutPortal when content is view, Placeholder when not
   - The actual full rendering happens via EntityRenderer in RenderManager
   - **Answer: No direct registry use** - but Child gets new `customArgs` prop that forwards via OutPortal

2. **Should Placeholder show ref-style rendering?**
   - Current: `[{entityId.slice(0, 8)}...] open elsewhere`
   - Could use ref renderer for consistency
   - **Answer: Could enhance later, not required for this task**

3. **How does isTopLevel get passed?**
   - RenderManager knows if view is panel-owned (`view.panelId !== null`) vs slot-owned
   - **Decision: RenderManager passes `isTopLevel={view.panelId !== null}` to EntityRenderer**

4. **What about Child/Ref components used by full renderers?**
   - Full renderers need to render Child for owned entities
   - Full renderers need RefLink for ref entities
   - These should be imported from render-manager
   - **No change needed** - they'll import Child and RefLink as before

5. **RefLink props**
   - **Add `isOwned?: boolean`** prop, defaults to false, forwarded to ref renderer
   - **Add `customArgs?: Record<string, unknown>`** prop, forwarded to ref renderer

6. **Ref renderer content preview**
   - Brief content only shows when `expandToFill=true`
   - Inline refs (expandToFill=false) just show type color + truncated ID
