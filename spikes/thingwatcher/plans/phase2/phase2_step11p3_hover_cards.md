# Phase 2 Step 11 Part 3: Hover Preview Cards

## Overview

Add hover preview cards that appear when hovering over `NodeRef`, `InlineNodeRef`, and `DraggableIDMarker`. This completes the four-tier renderer system (full, ref, inline, preview) and enables quick entity inspection without navigating away.

**Features:**
- `PreviewRenderer`: Fourth renderer export, read-only preview of a thing
- Hover over `NodeRef`/`InlineNodeRef` shows the thing's preview
- Hover over `DraggableIDMarker` shows backlinks with link types
- Infinite nesting: hovering items inside a preview card opens nested previews
- Clicking a link inside a hover card closes all hovers

## User Decisions

- **PreviewRenderer API**: Read-only (payload, dataChunks, links, metadata, DraggableIDMarker) - no trigger, no args
- **PreviewRenderer content**: Should always include draggable ID (via DraggableIDMarker) and show links using NodeRef/InlineNodeRef
- **DraggableIDMarker hover**: Shows backlinks with link types only (no ownership hierarchy)
- **Close on navigation**: Yes, clicking links inside hovers closes them
- **Timing**: 500ms delay to open, 300ms delay to close
- **Fallback preview**: Shows type badge, keyJSON, and list of outgoing links (like fallback full renderer)
- **Data fetching**: PreviewContent re-queries via `useThing` (hits urql cache, simple implementation)
- **EmbedApp**: Yes, hover previews work in embed view too (EmbedApp provides HoverCloseContext)
- **ChildNodeView restriction**: Document as convention only, no technical enforcement
- **HoverCloseContext location**: DockviewLayout (and EmbedApp) provide it

## Current State

### What exists

1. **NodeRef / InlineNodeRef** (`lib/ThingWatcherBindings.tsx`): Clickable/draggable refs. No hover preview yet.

2. **DraggableIDMarker** (`lib/DraggableIDMarker.tsx`): Draggable ID wrapper. No hover preview yet.

3. **DynamicRenderer system**: Compiles `{ full, ref, inline }` from custom renderer TSX.

4. **FallbackRenderers**: FallbackFullRenderer shows type, key, data tree, and links.

5. **@base-ui/react**: Already installed (used for context menu). Has `PreviewCard` component.

6. **ThingUIEventsContext** (`lib/ThingUIEventsContext.tsx`): Provides `onClickThing` and `onOpenInPanel` callbacks.

### Panel_spike reference

The panel_spike uses:
- `EntityPreviewCard`: Wraps trigger, shows preview renderer on hover
- `BacklinksPreviewCard`: Wraps trigger, shows backlinks list on hover
- `HoverKeptOpenContext`: React context to propagate open state to parent hovers
- `registerHoverCloseListener`: ViewRegistry method to close hovers when panels open
- `@base-ui/react/preview-card` for the popover UI

## Design

### PreviewRenderer Props

```typescript
export interface PreviewRendererProps {
  /** First data chunk (use for simple single-chunk things) */
  payload: unknown;
  /** All data chunks in order (use for multi-chunk things) */
  dataChunks: unknown[];
  /** Outgoing links */
  links: LinkInfo[];
  /** Thing metadata */
  shortId: string;
  keyJSON: unknown;
  thingType: string;
}
```

Key differences from FullRendererProps:
- No `triggerable` / `trigger()` (read-only)
- No `args` (previews don't receive custom args)
- No `DraggableIDMarker` (not needed in previews)
- No `isTopLevel` (previews are always nested)

Renderers can use `NodeRef`/`InlineNodeRef` inside PreviewRenderer (enabling nested hovers), but NOT `ChildNodeView` (which would be too heavy for previews). This is a documented convention, not technically enforced.

### Hover Context System

```typescript
// lib/HoverContext.tsx

/**
 * Context for nested hover cards. When a child hover opens, it calls the
 * parent's setKeptOpen(true) to prevent the parent from closing.
 */
export const HoverKeptOpenContext = createContext<((open: boolean) => void) | null>(null);

/**
 * Context for closing all hovers when navigation happens.
 * Provides a callback that any component can call to close all open hovers.
 */
export const HoverCloseContext = createContext<(() => void) | null>(null);

/**
 * Hook to get the close-all-hovers callback.
 */
export function useCloseAllHovers(): (() => void) | null;

/**
 * Hook to get the parent's setKeptOpen callback for nested hovers.
 */
export function useSetKeptOpen(): ((open: boolean) => void) | null;
```

### HoverPreviewCard Component

Wraps a trigger element and shows an entity preview on hover.

```typescript
// lib/HoverPreview.tsx

interface HoverPreviewCardProps {
  /** Thing key to preview */
  thingKey: string;
  /** Trigger element */
  children: ReactNode;
}

export function HoverPreviewCard({ thingKey, children }: HoverPreviewCardProps) {
  const [open, setOpen] = useState(false);
  const [keptOpenByChild, setKeptOpenByChild] = useState(false);
  const [keptOpenByHover, setKeptOpenByHover] = useState(false);
  const [touchedBeforePanelAction, setTouchedBeforePanelAction] = useState(false);
  const parentSetKeptOpen = useSetKeptOpen();
  const closeAllHovers = useCloseAllHovers();

  const isOpen = touchedBeforePanelAction && (open || keptOpenByChild || keptOpenByHover);

  // Propagate open state to parent
  useEffect(() => {
    parentSetKeptOpen?.(isOpen);
  }, [isOpen, parentSetKeptOpen]);

  return (
    <PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
      <PreviewCard.Trigger
        delay={500}
        closeDelay={300}
        onMouseEnter={() => setTouchedBeforePanelAction(true)}
      >
        {children}
      </PreviewCard.Trigger>
      <PreviewCard.Portal>
        <PreviewCard.Positioner side="top" align="start" alignOffset={20}>
          <PreviewCard.Popup
            onMouseEnter={() => setKeptOpenByHover(true)}
            onMouseLeave={() => setKeptOpenByHover(false)}
            className={styles.hoverPopup}
          >
            <HoverKeptOpenContext.Provider value={setKeptOpenByChild}>
              <PreviewContent thingKey={thingKey} />
            </HoverKeptOpenContext.Provider>
          </PreviewCard.Popup>
        </PreviewCard.Positioner>
      </PreviewCard.Portal>
    </PreviewCard.Root>
  );
}
```

### PreviewContent Component

Internal component that fetches thing data and renders the preview:

```typescript
interface PreviewContentProps {
  thingKey: string;
}

function PreviewContent({ thingKey }: PreviewContentProps) {
  // Re-query via useThing - hits urql cache so usually instant
  const { thing, loading } = useThing({ keyInput: { key: thingKey }, includeData: true });
  const config = useThingTypeConfig(thing?.thingType);

  if (loading || !thing) {
    return <div className={styles.loadingPreview}>...</div>;
  }

  // Build PreviewRendererProps from thing data
  const previewProps: PreviewRendererProps = {
    payload: thing.data,
    dataChunks: thing.dataChunks?.map(c => c.data) ?? [],
    links: thing.links.map(link => ({
      linkType: link.linkType,
      linkKey: link.linkKey,
      linkKeyJSON: link.linkKeyJSON,
      toThingKey: link.toThing.key,
      toThingShortId: link.toThing.shortId,
      toThingType: link.toThing.thingType,
    })),
    shortId: thing.shortId,
    keyJSON: thing.keyJSON,
    thingType: thing.thingType,
  };

  return (
    <DynamicPreviewRenderer
      thingType={thing.thingType}
      tsxCode={config?.renderer ?? null}
      rendererProps={previewProps}
    />
  );
}
```

### BacklinksHoverCard Component

Wraps DraggableIDMarker and shows backlinks on hover.

```typescript
interface BacklinksHoverCardProps {
  thingKey: string;
  children: ReactNode;
}

export function BacklinksHoverCard({ thingKey, children }: BacklinksHoverCardProps) {
  // Similar structure to HoverPreviewCard, but content shows:
  // - "Referenced by:" header
  // - List of backlinks, each as a NodeRef with link type label
  // - "(none)" if no backlinks
}
```

### BacklinksContent Component

Internal component that fetches backlinks and renders them:

```typescript
function BacklinksContent({ thingKey }: { thingKey: string }) {
  // Query thing with backlinks
  const { thing, loading } = useThing({ keyInput: { key: thingKey }, includeData: false });

  if (loading || !thing) {
    return <div className={styles.loadingPreview}>...</div>;
  }

  const backlinks = thing.backlinks ?? [];

  return (
    <div className={styles.backlinksSection}>
      <div className={styles.sectionHeader}>Referenced by:</div>
      {backlinks.length === 0 ? (
        <div className={styles.emptyBacklinks}>(none)</div>
      ) : (
        <div className={styles.backlinksList}>
          {backlinks.map((backlink, i) => (
            <div key={i} className={styles.backlinkItem}>
              <span className={styles.linkType}>{backlink.linkType}:</span>
              <NodeRef thingKey={backlink.fromThing.key} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

Note: The GraphQL `Link` type has `linkType: String!` and `fromThing: Thing!`, which gives us everything needed. The `fromThing` is rendered as a NodeRef which will show the thing's type/shortId via its RefRenderer.

### Close on Navigation

When clicking a link inside a hover, we need to close all hovers. This integrates with the existing `ThingUIEventsContext`:

```typescript
// In ThingUIEventsProvider (DockviewLayout.tsx)
const [closeHoversCallback, setCloseHoversCallback] = useState<(() => void) | null>(null);

// Wrap onClickThing to also close hovers
const handleClickThing = useCallback((key: string) => {
  closeHoversCallback?.();
  onSelectThing(key);
}, [closeHoversCallback, onSelectThing]);
```

The `HoverCloseContext` provider wraps the content and provides a way to register the close callback.

### FallbackPreviewRenderer

Shows type badge, keyJSON, and outgoing links:

```typescript
export function FallbackPreviewRenderer({
  thingType,
  keyJSON,
  links,
}: PreviewRendererProps) {
  return (
    <div className={styles.fallbackPreview}>
      <div className={styles.previewHeader}>
        <span className={styles.typeBadge}>{thingType}</span>
        <span className={styles.keyDisplay}>
          {keyJSON !== null ? JSON.stringify(keyJSON) : '(binary key)'}
        </span>
      </div>
      {links.length > 0 && (
        <div className={styles.previewLinks}>
          {links.map((link, i) => (
            <div key={i} className={styles.previewLinkItem}>
              <span className={styles.linkType}>{link.linkType}:</span>
              <InlineNodeRef thingKey={link.toThingKey} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

### Updated CompiledRenderers

```typescript
export interface CompiledRenderers {
  full: React.ComponentType<FullRendererProps> | null;
  ref: React.ComponentType<RefRendererProps> | null;
  inline: React.ComponentType<RefRendererProps> | null;
  preview: React.ComponentType<PreviewRendererProps> | null;
}
```

### Wrapping NodeRef and InlineNodeRef

Both components get wrapped with `HoverPreviewCard`:

```typescript
// In NodeRef
return (
  <HoverPreviewCard thingKey={thing.key}>
    <ThingContextMenu thingKey={thing.key}>
      <div className={styles.noderef} onClick={handleClick} ...>
        <DynamicRefRenderer ... />
      </div>
    </ThingContextMenu>
  </HoverPreviewCard>
);

// In InlineNodeRef - same pattern with <span>
```

### Wrapping DraggableIDMarker

```typescript
// In DraggableIDMarker
return (
  <BacklinksHoverCard thingKey={thingKey}>
    <span draggable onDragStart={handleDragStart} className={styles.draggableId}>
      {children}
    </span>
  </BacklinksHoverCard>
);
```

### Styles

```css
/* lib/HoverPreview.module.css */

.hoverPopup {
  background: #1a1a1a;
  border: 2px solid #555;
  border-radius: 4px;
  padding: 8px;
  max-width: 300px;
  z-index: 1000;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
}

.sectionHeader {
  color: #888;
  font-size: 0.9em;
  margin-bottom: 4px;
}

.emptyBacklinks {
  color: #666;
  font-style: italic;
}

.backlinksList {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.backlinkItem {
  display: flex;
  align-items: center;
  gap: 8px;
}

.linkType {
  color: #888;
  font-size: 0.85em;
  min-width: 60px;
}

/* Fallback preview */
.fallbackPreview {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.previewHeader {
  display: flex;
  align-items: center;
  gap: 8px;
}

.typeBadge {
  background: #444;
  padding: 2px 6px;
  border-radius: 3px;
  font-size: 0.85em;
}

.keyDisplay {
  color: #8cf;
  font-size: 0.9em;
}

.previewLinks {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.previewLinkItem {
  display: flex;
  align-items: center;
  gap: 8px;
}
```

## Implementation Steps

- [x] 1. Add `PreviewRendererProps` type to `lib/ThingWatcherBindings.tsx`
- [x] 2. Create `lib/HoverContext.tsx` with `HoverKeptOpenContext`, `HoverCloseContext`, and hooks
- [x] 3. Update `CompiledRenderers` in `esbuildSetup.ts` to include `preview`
- [x] 4. Update `compileRenderer` to extract `PreviewRenderer` export
- [x] 5. Add `FallbackPreviewRenderer` to `FallbackRenderers.tsx`
- [x] 6. Add `DynamicPreviewRenderer` component to `DynamicRenderer.tsx`
- [x] 7. Create `lib/HoverPreview.tsx` with `HoverPreviewCard` component
- [x] 8. Create `lib/HoverPreview.module.css` with hover card styles
- [x] 9. Add `BacklinksHoverCard` component to `lib/HoverPreview.tsx`
- [x] 10. Update `DockviewLayout.tsx` to provide `HoverCloseContext` and integrate with `onClickThing`
- [x] 11. Update `EmbedApp.tsx` to provide `HoverCloseContext` (hovers work in embed view too)
- [x] 12. Wrap `NodeRef` with `HoverPreviewCard` in `ThingWatcherBindings.tsx`
- [x] 13. Wrap `InlineNodeRef` with `HoverPreviewCard` in `ThingWatcherBindings.tsx`
- [x] 14. Wrap `DraggableIDMarker` content with `BacklinksHoverCard`
- [x] 15. Update all examples in `examples/` to export `PreviewRenderer`
- [x] 16. Update `LOCAL_CODE_OVERVIEW.md` documentation
- [ ] 17. Manual testing:
    - Hover over NodeRef → preview card appears after 500ms
    - Hover over InlineNodeRef → preview card appears
    - Hover over DraggableIDMarker → backlinks card appears
    - Hover over NodeRef inside a preview → nested preview opens
    - Moving mouse to preview card keeps it open
    - Clicking link inside preview → hovers close, navigation happens
    - Preview uses custom PreviewRenderer when exported
    - Preview uses fallback when PreviewRenderer not exported
    - Hovers work in embed view
- [x] 18. Update CODE_OVERVIEW.md

## Files Changed

**New files:**
- `lib/HoverContext.tsx` - Hover context providers and hooks
- `lib/HoverPreview.tsx` - HoverPreviewCard and BacklinksHoverCard components
- `lib/HoverPreview.module.css` - Hover card styles

**Modified files:**
- `lib/ThingWatcherBindings.tsx` - Add PreviewRendererProps, wrap NodeRef/InlineNodeRef with hover
- `lib/DraggableIDMarker.tsx` - Wrap with BacklinksHoverCard
- `components/DockviewLayout/DockviewLayout.tsx` - Provide HoverCloseContext, wrap onClickThing to close hovers
- `EmbedApp.tsx` - Provide HoverCloseContext (hovers work in embed view)
- `components/DynamicRenderer/esbuildSetup.ts` - Add preview to CompiledRenderers
- `components/DynamicRenderer/DynamicRenderer.tsx` - Add DynamicPreviewRenderer
- `components/DynamicRenderer/DynamicRenderer/index.ts` - Export DynamicPreviewRenderer and FallbackPreviewRenderer
- `components/DynamicRenderer/FallbackRenderers.tsx` - Add FallbackPreviewRenderer
- `examples/*.py` - Add PreviewRenderer export to all examples with custom renderers
- `frontend/LOCAL_CODE_OVERVIEW.md` - Documentation
- `CODE_OVERVIEW.md` - High-level documentation

## Testing Notes

Manual testing scenarios:

1. **Basic hover**: Hover over a NodeRef in the DetailsPanel → preview appears after 500ms delay
2. **Nested hover**: In the preview card, hover over a NodeRef → second preview opens, first stays open
3. **Leave timing**: Move mouse away from preview → closes after 300ms delay
4. **Keep open**: Move mouse into preview popup → stays open until mouse leaves
5. **Navigation close**: Click a NodeRef inside a preview → all hovers close, thing is selected
6. **Backlinks hover**: Hover over DraggableIDMarker in a custom renderer → shows backlinks with link types
7. **Custom preview**: Run demo with PreviewRenderer exported → custom preview content shown
8. **Fallback preview**: Thing without custom PreviewRenderer → shows type, key, links

## Example PreviewRenderer

```tsx
// In a custom renderer file
export function PreviewRenderer({ payload, links, thingType, shortId }: PreviewRendererProps) {
  return (
    <div style={{ padding: 4 }}>
      <div style={{ fontWeight: 'bold', marginBottom: 4 }}>
        {thingType} #{shortId}
      </div>
      <div style={{ color: '#aaa', fontSize: '0.9em' }}>
        {payload?.title || 'Untitled'}
      </div>
      {links.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {links.slice(0, 3).map((link, i) => (
            <InlineNodeRef key={i} thingKey={link.toThingKey} />
          ))}
          {links.length > 3 && <span> +{links.length - 3} more</span>}
        </div>
      )}
    </div>
  );
}
```

## Future Considerations

- The hover system could be extended to show previews in the graph view (on node hover)
- Preview cards could potentially support keyboard navigation (arrow keys to move between links)
- The `HoverCloseContext` pattern could be reused for other overlay dismissal scenarios

## Implementation Notes / Deviations

1. **ThingUIEventsContext not modified**: The original plan (step 10) said to "Update ThingUIEventsContext to include hover close callback registration". Instead, we kept `ThingUIEventsContext` unchanged and created a separate `HoverCloseContext`. The close-on-navigation behavior is achieved by wrapping the `onClickThing` callback in `DockviewLayout` and `EmbedApp` to call `closeAllHovers()` before the original handler. This keeps concerns more separated.

2. **Base-ui PreviewCard API**: Implementation assumes `@base-ui/react/preview-card` has the API used in panel_spike (delay/closeDelay props, render prop on Trigger, Positioner with side/align/sideOffset/alignOffset). If the actual API differs, adjustments may be needed.

3. **Potential circular import**: `HoverPreview.tsx` imports `NodeRef` from `ThingWatcherBindings.tsx`, and `ThingWatcherBindings.tsx` imports `HoverPreviewCard` from `HoverPreview.tsx`. ES modules should handle this, but watch for import errors at runtime.

4. **Backlinks query assumption**: `BacklinksContent` uses `useThing({ includeData: false })` assuming backlinks are still fetched. If backlinks require `includeData: true`, this would need adjustment.

5. **Step numbering changed**: Original plan had 19 steps including a separate step for ThingUIEventsContext. Final implementation has 18 steps with the ThingUIEventsContext step removed and DockviewLayout/EmbedApp steps consolidated.
