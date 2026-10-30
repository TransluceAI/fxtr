# Hover Previews Implementation Scratch Notes

## Current State Analysis

### Relevant Components
- **RefLink** (`EntityRenderer.tsx:86-130`): Block reference, clickable/draggable. Need hover preview.
- **InlineRefLink** (`EntityRenderer.tsx:152-196`): Inline reference. Need hover preview.
- **Placeholder** (`Child.tsx:34-151`): Shows when entity rendered elsewhere. Need hover preview.
- **DraggableID** (`DraggableID.tsx`): Draggable ID badge. Need backlinks preview + backlinks count.

### Existing Hover Pattern (DepsTest.tsx)
The `HoverableLink` component shows the nested hover pattern:
```tsx
const [open, setOpen] = useState(false);           // from PreviewCard
const [keptOpenByChild, setKeptOpenByChild] = useState(false);  // child keeping us open
const [keptOpenByHover, setKeptOpenByHover] = useState(false);  // mouse on popup

const isOpen = open || keptOpenByChild || keptOpenByHover;

<PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
  <PreviewCard.Trigger delay={200}>...</PreviewCard.Trigger>
  <PreviewCard.Portal>
    <PreviewCard.Positioner>
      <PreviewCard.Popup
        onMouseEnter={() => setKeptOpenByHover(true)}
        onMouseLeave={() => setKeptOpenByHover(false)}
      >
        <NestedComponent setKeptOpen={setKeptOpenByChild} />
      </PreviewCard.Popup>
    </PreviewCard.Positioner>
  </PreviewCard.Portal>
</PreviewCard.Root>
```

## Architecture Design

### 1. New Renderer Type: `preview`
Add to `RendererEntry`:
```ts
interface RendererEntry {
  full: React.FC<FullRendererProps>;
  ref: React.FC<RefRendererProps>;
  inline: React.FC<InlineRefRendererProps>;
  preview: React.FC<PreviewRendererProps>;  // NEW
}
```

Preview props similar to Full but without isTopLevel (always a preview context):
```ts
interface PreviewRendererProps {
  entityId: EntityId;
  data: DataBundle;
  customArgs?: Record<string, unknown>;
}
```

### 2. Hover Context for Propagation
Need a context to propagate `setKeptOpen` to nested hoverable items:
```tsx
const HoverKeptOpenContext = createContext<((open: boolean) => void) | undefined>(undefined);

function useSetKeptOpen() {
  return useContext(HoverKeptOpenContext);
}
```

### 3. EntityPreviewCard Component
Wraps content with PreviewCard, shows entity preview on hover:
```tsx
function EntityPreviewCard({ entityId, children }: { entityId: EntityId; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [keptOpenByChild, setKeptOpenByChild] = useState(false);
  const [keptOpenByHover, setKeptOpenByHover] = useState(false);
  const parentSetKeptOpen = useSetKeptOpen();

  const isOpen = open || keptOpenByChild || keptOpenByHover;

  // Propagate to parent
  useEffect(() => {
    parentSetKeptOpen?.(isOpen);
  }, [isOpen, parentSetKeptOpen]);

  return (
    <PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
      <PreviewCard.Trigger asChild delay={200}>{children}</PreviewCard.Trigger>
      <PreviewCard.Portal>
        <PreviewCard.Positioner>
          <PreviewCard.Popup onMouseEnter/Leave...>
            <HoverKeptOpenContext.Provider value={setKeptOpenByChild}>
              <PreviewRenderer entityId={entityId} />
            </HoverKeptOpenContext.Provider>
          </PreviewCard.Popup>
        </PreviewCard.Positioner>
      </PreviewCard.Portal>
    </PreviewCard.Root>
  );
}
```

### 4. BacklinksPreviewCard Component
Similar but shows backlinks list instead of entity preview:
```tsx
function BacklinksPreviewCard({ entityId, children }: Props) {
  // Same open state pattern...

  return (
    <PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
      <PreviewCard.Trigger asChild delay={200}>{children}</PreviewCard.Trigger>
      <PreviewCard.Portal>
        <PreviewCard.Popup ...>
          <HoverKeptOpenContext.Provider value={setKeptOpenByChild}>
            <BacklinksContent entityId={entityId} />
          </HoverKeptOpenContext.Provider>
        </PreviewCard.Popup>
      </PreviewCard.Portal>
    </PreviewCard.Root>
  );
}

function BacklinksContent({ entityId }: { entityId: EntityId }) {
  const data = useDataBundle();
  const backRefs = getReferencedBy(data, entityId);
  // Render list of RefLinks...
}
```

### 5. Preview Renderer Pattern
Each entity type's preview renderer should:
- Show a compact version of the entity (similar to ref but with more detail)
- Render children as `RefLink` not `Child` (so they show as refs, not inline)
- May contain `DraggableID` (for navigation/dragging)
- May contain `RefLink`/`InlineRefLink` for references

Example for Transcript:
```tsx
function TranscriptPreview({ entityId, data }: PreviewRendererProps) {
  const entity = getData(data, entityId) as Transcript;
  return (
    <div style={...}>
      <div>transcript: {entityId}</div>
      <div>Topic: {entity.topic}</div>
      <div>Turns ({entity.turns.length}):</div>
      {entity.turns.map(turnId => (
        <RefLink key={turnId} entityId={turnId} isOwned={true} />
      ))}
    </div>
  );
}
```

### 6. DraggableID Backlinks Count
Modify DraggableID to:
1. Accept `data` prop or use `useDataBundle()`
2. Call `getReferencedBy(data, entityId)` to get count
3. If count > 0, render lucide `MessageSquareReply` icon + count
4. Wrap the whole thing in `BacklinksPreviewCard`

## File Changes Needed

1. **types.ts**: Add `PreviewRendererProps` and update `RendererEntry`
2. **contexts.tsx**: Add `HoverKeptOpenContext` and `useSetKeptOpen`
3. **New file: HoverPreview.tsx**: `EntityPreviewCard`, `BacklinksPreviewCard`
4. **EntityRenderer.tsx**: Wrap `RefLink` and `InlineRefLink` with `EntityPreviewCard`
5. **Child.tsx**: Wrap `Placeholder` with `EntityPreviewCard`
6. **DraggableID.tsx**: Add backlinks count, wrap with `BacklinksPreviewCard`
7. **registry.tsx**: Update `useRegisteredEntityRenderer` to handle 'preview' type
8. **Each renderer file**: Add `preview` renderer to each entity type
9. **index.ts**: Update exports to include preview renderers

## Questions to Consider

1. Should the preview card arrow point to the trigger? (DepsTest uses it)
2. What's the max width for preview cards? (~300px seems reasonable)
3. Should previews have a border matching the entity type color?
4. For DraggableID with no backlinks, should it still show the BacklinksPreviewCard (empty state) or skip it entirely?
