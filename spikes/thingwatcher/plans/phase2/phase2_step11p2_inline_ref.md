# Phase 2 Step 11 Part 2: InlineNodeRef and InlineRefRenderer

## Overview

Add `InlineNodeRef` component and `InlineRefRenderer` export to complement the existing block-level `NodeRef` and `RefRenderer`. This enables custom renderers to provide both block and inline representations of references, following the pattern established in the panel_spike prototype.

**Key distinction:**
- `NodeRef` + `RefRenderer`: Block-level reference that fills its container width (one-line, can be in a row or flexbox)
- `InlineNodeRef` + `InlineRefRenderer`: Inline reference that flows with text, as compact as possible

## User Decisions

- **API Design**: `InlineRefRenderer` is a separate third export from custom renderers (alongside `FullRenderer` and `RefRenderer`)
- **Fallback Style**: Minimal badge for fallback - just colored type dot + shortId
- **NodeRef Styling**: Update NodeRef to be explicitly block-level (`display: block` or `flex`)

## Current State

### What exists

1. **NodeRef** (`lib/ThingWatcherBindings.tsx`): Renders clickable refs using `RefRenderer`. Currently styled as `inline-block`. Draggable.

2. **RefRenderer / RefRendererProps**: Props interface for block refs. Custom renderers export `RefRenderer`.

3. **FallbackRefRenderer** (`components/DynamicRenderer/FallbackRenderers.tsx`): Shows type badge + key/shortId in a padded inline-flex container.

4. **DynamicRefRenderer** (`components/DynamicRenderer/DynamicRenderer.tsx`): Compiles and renders custom RefRenderer or falls back.

5. **CompiledRenderers type** (`esbuildSetup.ts`): Currently `{ full, ref }`.

### Panel_spike reference

The panel_spike uses:
- `RefLink` wraps with `<div>` for block refs
- `InlineRefLink` wraps with `<span>` for inline refs
- Separate `ref` and `inline` renderer types per entity
- Inline renderers show minimal info (type dot + ID only)

## Design

### Types

No new props type needed - `InlineRefRenderer` reuses `RefRendererProps` since the data requirements are identical. The difference is purely in how the renderer chooses to display (block vs inline).

Custom renderers export:
```typescript
export function RefRenderer(props: RefRendererProps) { ... }      // Block display
export function InlineRefRenderer(props: RefRendererProps) { ... } // Inline display
```

### Updated CompiledRenderers

```typescript
export interface CompiledRenderers {
  full: React.ComponentType<FullRendererProps> | null;
  ref: React.ComponentType<RefRendererProps> | null;
  inline: React.ComponentType<RefRendererProps> | null;  // Same props as ref
}
```

### NodeRef Changes

Update `NodeRef` to be explicitly block-level:

```css
.noderef {
  display: flex;
  width: 100%;
  cursor: pointer;
}
```

The wrapper becomes a block element that fills width. The inner `RefRenderer` content determines the actual layout.

### New InlineNodeRef Component

Mirror of `NodeRef` but:
- Uses `<span>` wrapper with `display: inline`
- Calls `DynamicInlineRefRenderer` instead of `DynamicRefRenderer`
- Same click/drag/context-menu behavior

```typescript
export function InlineNodeRef({ thingKey, keyJSON, args = {} }: NodeRefProps) {
  // ... same data fetching and props building as NodeRef ...
  // refRendererProps: RefRendererProps built identically to NodeRef

  return (
    <ThingContextMenu thingKey={thing.key}>
      <span
        className={styles.inlineNoderef}
        onClick={handleClick}
        draggable
        onDragStart={handleDragStart}
        title="Click to select, drag to open in panel"
      >
        <DynamicInlineRefRenderer
          thingType={thing.thingType}
          tsxCode={rendererSource}
          rendererProps={refRendererProps}  // Same RefRendererProps as NodeRef
          fallbackExtra={{ shortId: thing.shortId, keyJSON: thing.keyJSON }}
        />
      </span>
    </ThingContextMenu>
  );
}
```

### FallbackInlineRefRenderer

Minimal badge - colored type dot + shortId:

```typescript
// Reuses FallbackRefRendererProps (extends RefRendererProps with shortId, thingType, keyJSON)
export function FallbackInlineRefRenderer({ shortId }: FallbackRefRendererProps) {
  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 4,
      color: '#8cf',
      fontSize: 12,
    }}>
      <span style={{
        width: 6,
        height: 6,
        borderRadius: 2,
        backgroundColor: '#4a8',  // Could be type-colored in future
      }} />
      #{shortId}
    </span>
  );
}
```

### DynamicInlineRefRenderer

New component in `DynamicRenderer.tsx`:

```typescript
export function DynamicInlineRefRenderer({
  thingType,
  tsxCode,
  rendererProps,
  fallbackExtra,
}: DynamicInlineRefRendererProps) {
  const { renderers, error, loading } = useCompiledRenderers(thingType, tsxCode);

  if (loading && tsxCode) {
    return <span className={styles.loadingInline}>...</span>;
  }

  // Use custom inline renderer if available, otherwise fallback
  const InlineRenderer = renderers?.inline;
  if (InlineRenderer) {
    return (
      <ErrorBoundary fallback={() => <FallbackInlineRefRenderer ... />}>
        <InlineRenderer {...rendererProps} />
      </ErrorBoundary>
    );
  }

  return <FallbackInlineRefRenderer ... />;
}
```

### CSS Updates

```css
/* NodeRef - block level, fills width */
.noderef {
  display: flex;
  width: 100%;
  cursor: pointer;
}

/* InlineNodeRef - inline, minimal */
.inlineNoderef {
  display: inline;
  cursor: pointer;
}

.inlineNoderef:hover {
  text-decoration: underline;
}
```

### Example Renderer Updates

Update `demo_embed.py` Widget renderer to include all three exports:

```tsx
export function FullRenderer({ payload, ... }) {
  // Full card view
}

export function RefRenderer({ payload }) {
  // Block ref - fills width, shows preview
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: '#2a4a2a',
      borderRadius: 4,
    }}>
      <span style={{ width: 8, height: 8, background: '#4a9', borderRadius: 2 }} />
      <span style={{ color: '#4a9' }}>Widget: {payload?.title}</span>
      <span style={{ color: '#888', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>
        ({payload?.messageCount} msgs)
      </span>
    </div>
  );
}

export function InlineRefRenderer({ payload }) {
  // Inline ref - minimal, flows with text
  return (
    <span style={{ color: '#4a9' }}>
      <span style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        background: '#4a9',
        borderRadius: 2,
        marginRight: 4
      }} />
      {payload?.title}
    </span>
  );
}
```

## Implementation Steps

- [x] 1. Update `CompiledRenderers` type in `esbuildSetup.ts` to include `inline`
- [x] 2. Update `compileRenderer` in `esbuildSetup.ts` to extract `InlineRefRenderer` export
- [x] 3. Add `FallbackInlineRefRenderer` to `FallbackRenderers.tsx`
- [x] 4. Add `DynamicInlineRefRenderer` component to `DynamicRenderer.tsx`
- [x] 5. Update CSS in `ThingWatcherBindings.module.css`:
   - Make `.noderef` explicitly block (display: flex, width: 100%)
   - Add `.inlineNoderef` class (display: inline)
- [x] 6. Add `InlineNodeRef` component to `lib/ThingWatcherBindings.tsx` (with shared `useNodeRefData` hook)
- [x] 7. Export `InlineNodeRef` from the module
- [x] 8. Update `demo_embed.py` example to export all three renderer variants
- [x] 9. Update `demo_multi_chunk.py` renderers with InlineRefRenderer
- [x] 10. Update documentation in `LOCAL_CODE_OVERVIEW.md`:
    - Add InlineNodeRef to component list
    - Update usage examples showing InlineRefRenderer
- [ ] 11. Manual testing:
    - Verify NodeRef now fills width (block behavior)
    - Verify InlineNodeRef flows inline with text
    - Verify custom InlineRefRenderer is used when exported
    - Verify fallback works when InlineRefRenderer not exported
    - Verify drag-to-panel works for both NodeRef and InlineNodeRef
    - Verify click and context menu work for both
- [x] 12. Update CODE_OVERVIEW.md

## Files Changed

- `lib/ThingWatcherBindings.tsx` - Add InlineNodeRef component
- `lib/ThingWatcherBindings.module.css` - Update .noderef, add .inlineNoderef
- `components/DynamicRenderer/esbuildSetup.ts` - Update CompiledRenderers type
- `components/DynamicRenderer/FallbackRenderers.tsx` - Add FallbackInlineRefRenderer
- `components/DynamicRenderer/DynamicRenderer.tsx` - Add DynamicInlineRefRenderer
- `examples/demo_embed.py` - Update Widget renderer
- `examples/demo_multi_chunk.py` - Update renderers (if applicable)
- `frontend/LOCAL_CODE_OVERVIEW.md` - Documentation updates
- `CODE_OVERVIEW.md` - High-level documentation updates

## Testing Notes

Manual testing is sufficient. Key scenarios:

1. **Block behavior**: NodeRef in a details panel should fill the width
2. **Inline behavior**: InlineNodeRef in flowing text should not break the line
3. **Custom renderers**: Both RefRenderer and InlineRefRenderer should be picked up
4. **Fallback**: When InlineRefRenderer not exported, fallback shows minimal badge
5. **Interactions**: Click/drag/context-menu work identically for both variants
6. **Examples**: Run demo_embed.py and verify both ref types display correctly

## Future Considerations

- Part 3 (hover preview cards) will use these ref components
- The `inline` renderer type aligns with panel_spike's four-tier system (full, ref, inline, preview)
- Color for type indicator in fallback could come from renderingConfig in future
