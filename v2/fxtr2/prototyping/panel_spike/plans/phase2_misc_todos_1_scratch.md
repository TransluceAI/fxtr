# Phase 2 Misc Todos - Scratch Notes

## Summary of Changes

### 1. RefLink drag creates new view without holding option
- Currently: `onUnhandledDragOverEvent` in RenderManager.tsx rejects refs without alt key
- Change: Accept ref drags always, create new view on drop
- Files: `RenderManager.tsx` lines 57-62, 96-103

### 2. DraggableID removes viewId prop, gets from context
- Currently: Takes `viewId` as prop, uses it for drag data
- Change: Use `useCurrentViewId()` hook internally (may return null in preview context)
- In preview context (no viewId): act like RefLink (create new view)
- In view context (has viewId): act like before (move/duplicate view)
- Files: `DraggableID.tsx`

### 3. RefLink removes viewRegistry prop
- Currently: Takes `viewRegistry` as prop
- Change: Use `useViewRegistry()` hook internally
- Same for `InlineRefLink`
- Files: `EntityRenderer.tsx`

### 4. Remove `data` prop from renderers, add `useEntityData(entityId)` hook
- Currently: All renderers receive `data: DataBundle` prop
- Change: Create `useEntityData(entityId)` hook that uses `useDataBundle()` + `getData()`
- Update all renderer props interfaces to remove `data`
- Update all renderers to use hook instead of prop
- Files: `types.ts`, `contexts.tsx`, all renderers

### 5. Clean up customArgs
- Remove `customArgs` from props where not used (don't unpack if unused)
- Don't pass customArgs to children/RefLinks unless specifically needed
- Files: All renderers

### 6. Transcript passes `showPreviousTurnLink: false` to Turn children
- Transcript renderer: pass `customArgs={{ showPreviousTurnLink: false }}` to Child
- Turn renderer: check `customArgs?.showPreviousTurnLink !== false` before showing previous turn
- Files: `transcript.tsx`, `turn.tsx`

### 7. Default view on startup
- Open debug panel, collections panel (EntrypointPanel?), and simple_collection entity
- All in same group with simple_collection active
- Files: `RenderManager.tsx` onDockviewReady

## Order of Operations

1. Add `useEntityData` hook to contexts.tsx
2. Update types.ts - remove `data` from all renderer props
3. Update RefLink/InlineRefLink - remove viewRegistry prop, use hook
4. Update DraggableID - remove viewId prop, use context, handle preview case
5. Update RenderManager - accept RefLink drag without alt, change initial panels
6. Update all renderers:
   - Remove data prop usage, use useEntityData hook
   - Clean up customArgs (remove where unused)
   - Transcript: pass showPreviousTurnLink to children
   - Turn: respect showPreviousTurnLink
