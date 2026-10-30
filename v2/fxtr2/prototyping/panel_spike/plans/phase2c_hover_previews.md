
# Goal

In this phase we will add hover preview cards.

The existing renderer registry should be extended with a new rendering type "previewcard". The renderer component for "previewcard" should be as described in the spec doc, and should render a small preview of the element. (Any children of the element should render as owned refs rather than as inline children.)

There should be two types of preview card that show, depending on the context.

- Entity preview:
    - Shows the entity being hovered over. Uses the "previewcard" renderer.
    - Appears when hovering over a RefLink / InlineRefLink OR hovering over a slot placeholder.
- Backlink preview:
    - Shows the list of backlinks to a given entity, similar to how they appear in the entity panel.
    - Appears when hovering over a DraggableID.
    - Does not use "previewcard", instead is a different component "BacklinksPreviewCard"

The implementation can use baseui similar to the DepsTest.tsx. Use controlled components with the multiple different "open" flags, to make it possible to have hover cards inside hover cards (where the children keep the parent open); we will use this. In particular, the preview rendering should be allowed to contain RefLink and DraggableID itself, and hovering those should keep their parent open. We may have to plumb this through via a new hover context.

As a side feature, while implementing this, we will also add the following:
- The DraggableID, after rendering the ID, should also render a backlinks count; if nonzero, it should show as `message-square-reply` from lucide followed by a backlinks count. This way you know how many backlinks it has and can hover to see them.

---

# Implementation Plan

## 1. Add `preview` renderer type to registry

**types.ts:**
- Add `PreviewRendererProps` (like `FullRendererProps` but without `isTopLevel`)
- Add `preview: React.FC<PreviewRendererProps>` to `RendererEntry`

**registry.tsx:**
- Update `useRegisteredEntityRenderer` to handle `'preview'` type
- Add `FallbackPreviewRenderer`

## 2. Create hover context for nested preview propagation

**contexts.tsx:**
- Add `HoverKeptOpenContext` and `useSetKeptOpen()` hook
- Pattern: parent passes `setKeptOpenByChild` down, children call it with `true`/`false`

## 3. Create HoverPreview.tsx with wrapper components

**New file: render-manager/HoverPreview.tsx**

Two components using `@base-ui/react/preview-card`:

**EntityPreviewCard** - wraps trigger, shows entity preview on hover:
- Controlled open state: `open || keptOpenByChild || keptOpenByHover`
- `useEffect` propagates `isOpen` to parent via `useSetKeptOpen()`
- Popup: `side="top"`, `alignOffset={10}`, `maxWidth: 300`, `1px solid #555` border
- Inside popup: provides `HoverKeptOpenContext` so nested RefLinks can keep it open
- Renders entity's `preview` renderer

**BacklinksPreviewCard** - wraps trigger, shows backlinks list on hover:
- Same open state pattern
- Inside: list of `RefLink` for each backlink (from `getReferencedBy`)
- If no backlinks: show "(no backlinks)" or similar

## 4. Wrap existing components with preview cards

**EntityRenderer.tsx:**
- Wrap `RefLink`'s inner content with `EntityPreviewCard`
- Wrap `InlineRefLink`'s inner content with `EntityPreviewCard`

**Child.tsx:**
- Wrap `Placeholder`'s inner content with `EntityPreviewCard`

**DraggableID.tsx:**
- Add `useDataBundle()` to get backlinks count via `getReferencedBy()`
- If count > 0: show `MessageSquareReply` icon (from lucide-react) + count
- Wrap entire component with `BacklinksPreviewCard`

## 5. Add preview renderers for each entity type

**For each file in renderers/by_entity_type/:**

Add `EntityTypePreview` component:
- Similar to full renderer but more compact
- Children render as `RefLink` (not `Child`) so they're refs, not inline
- References also render as `RefLink`
- Can include `DraggableID` for the entity ID

Update export: `{ full, ref, inline, preview }`

**index.ts:**
- Update `defaultRegistry` to include preview renderers

## File Change Summary

| File | Changes |
|------|---------|
| `renderers/types.ts` | Add `PreviewRendererProps`, update `RendererEntry` |
| `render-manager/contexts.tsx` | Add `HoverKeptOpenContext`, `useSetKeptOpen` |
| `render-manager/HoverPreview.tsx` | NEW: `EntityPreviewCard`, `BacklinksPreviewCard` |
| `render-manager/registry.tsx` | Handle `'preview'` type, add fallback |
| `render-manager/EntityRenderer.tsx` | Wrap RefLink/InlineRefLink with EntityPreviewCard |
| `render-manager/Child.tsx` | Wrap Placeholder with EntityPreviewCard |
| `render-manager/DraggableID.tsx` | Add backlinks count + icon, wrap with BacklinksPreviewCard |
| `renderers/by_entity_type/*.tsx` | Add preview renderer to each (7 files) |
| `renderers/index.ts` | Include preview in registry |

## Implementation Order

1. Types + context (foundation)
2. HoverPreview.tsx (the wrapper components)
3. registry.tsx updates (so preview renderers can be looked up)
4. One preview renderer (e.g., transcript) to test the system
5. Wire up RefLink/InlineRefLink with EntityPreviewCard
6. Wire up Placeholder with EntityPreviewCard
7. DraggableID backlinks count + BacklinksPreviewCard
8. Remaining preview renderers (6 more entity types)
9. Test nested hover behavior
