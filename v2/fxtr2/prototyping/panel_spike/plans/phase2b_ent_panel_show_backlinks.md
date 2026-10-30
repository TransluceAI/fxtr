# Entity panel backlinks

When an entity is open in its own panel, we should show:
- Above it: A list of refs to all of its parents. Usually this will have only one element. Can be phrased like "Contained in:"
- Below it: A list of refs to all elements that have references to this element (back-references)

---

## Implementation Plan

### Current State Analysis

The data layer already supports this feature:
- `ownership.ts` has `getOwners(bundle, id)` → returns parent entity IDs
- `ownership.ts` has `getReferencedBy(bundle, id)` → returns back-reference entity IDs

The issue is that `EntityPanel.tsx` doesn't have access to the `DataBundle`. Currently:
- `App.tsx` creates the `DataBundle` and passes it to `RenderRoot`
- `EntrypointPanel` and `DebugPanel` receive `data` via their params
- `EntityPanel` only receives `viewId` in params, no data access

### Implementation Steps

1. **Add `DataBundleContext` to `contexts.tsx`**
   - Create a new context for `DataBundle`
   - Add a `useDataBundle()` hook similar to `useViewRegistry()`

2. **Provide the context in `RenderRoot` (RenderManager.tsx)**
   - `RenderRoot` already receives `data: DataBundle` as a prop
   - Wrap its children with `DataBundleContext.Provider`
   - This keeps data management consolidated in the render layer

3. **Modify `EntityPanel.tsx`**
   - Import `useDataBundle` and ownership helpers
   - Get the `entityId` from the view
   - Call `getOwners(data, entityId)` for parents
   - Call `getReferencedBy(data, entityId)` for back-references
   - Render a "Contained in:" section above the entity (if parents exist)
   - Render a "Referenced by:" section below the entity (if back-refs exist)
   - Use clickable ref links similar to `RefLink` in `EntityRenderer.tsx`

### UI Design

```
┌─────────────────────────────────────┐
│ Contained in: [parent1] [parent2]   │  ← Only show if has parents
├─────────────────────────────────────┤
│                                     │
│    [Entity content via OutPortal]   │
│                                     │
├─────────────────────────────────────┤
│ Referenced by: [ref1] [ref2] [ref3] │  ← Only show if has back-refs
└─────────────────────────────────────┘
```

Each ref will be a clickable link that calls `viewRegistry.openEntity(id)`.
