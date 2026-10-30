# Phase 0 Step 6: Thing Drop into Input Box

## Goal

Allow users to drag and drop things into the CodeMirror input box, automatically inserting the `<tw-k>JSON</tw-k>` syntax that gets rendered as an `InlineNodeRef`.

## Current State

- The input uses CodeMirror 6 with a ViewPlugin that detects `<tw-k>JSON</tw-k>` patterns and renders them as `InlineNodeRef` widgets via React portals
- Users currently have to manually type the syntax
- Thingwatcher provides `useThingDropRegion` hook for creating drop zones

## Design

### Approach

1. Wrap the CodeMirror input area with `ThingDropRegion` to suppress dockview's default drop handling
2. Add standard HTML5 drag/drop event handlers to the CodeMirror container
3. On drop, extract the thing info and insert `<tw-k>JSON</tw-k>` at the current cursor position
4. Show visual feedback during drag (border/background change)

### Key API from thingwatcher

```tsx
const {
  ThingDropRegion,      // Wrapper component that suppresses dockview drops
  mimeTypeForAnyThing,  // Mime type to check if drag contains any thing
  extractDroppedThing,  // Extract ThingDropInfo from DataTransfer
} = useThingDropRegion(handle);

interface ThingDropInfo {
  key: string;      // Base64-encoded CBOR key
  keyJSON: unknown; // JSON-deserialized key
  shortId: number;
  thingType: string;
}
```

### Insertion Logic

**Simplified:** CodeMirror already handles text drops natively, and thingwatcher sets the plain text DataTransfer to `<tw-k>JSON</tw-k>` syntax. So we don't need custom insertion logic - we just:
1. Wrap with `ThingDropRegion` to suppress dockview's drop handling
2. Show visual feedback during drag
3. Let CodeMirror handle the actual text insertion

### Visual Feedback

- Normal state: existing border style (`1px solid #333`)
- Drag over (valid, enabled): green border (`2px solid #22c55e`), subtle green background tint
- Drag over (disabled): no change - completely ignored

### Disabled State Handling

- Check `triggerable` state in drag handlers
- If not triggerable, return early without any state changes or `preventDefault()`
- The drag will be ignored entirely (no visual feedback, drop rejected by browser)

## Implementation Steps

1. [x] Import `useThingDropRegion` and `ThingDropInfo` type in ClydraRoot.tsx
2. [x] Call `useThingDropRegion(handle)` to get drop helpers
3. [x] Add state for drag feedback: `isInputDragOver`
4. [x] Wrap the CodeMirror container div with `ThingDropRegion`
5. [x] Add `onDragOver`, `onDragLeave`, `onDrop` handlers to the inner div
6. [x] Implement drop handler that:
   - Extracts thing info via `extractDroppedThing()`
   - Gets CodeMirror view from `editorRef`
   - Inserts `<tw-k>JSON</tw-k>` at cursor position
7. [x] Update border/background styles based on drag state
8. [ ] Test: drag a thing from another panel into the input
9. [x] Update CODE_OVERVIEW.md

## Code Sketch

```tsx
// In FullRenderer:

// Add drop region hook
const {
  ThingDropRegion,
  mimeTypeForAnyThing,
  extractDroppedThing,
} = useThingDropRegion(handle);

// Drag state
const [isDragOver, setIsDragOver] = useState(false);

// Handlers
const handleInputDragOver = useCallback((e: React.DragEvent) => {
  if (!triggerable) return; // Reject if disabled
  if (!e.dataTransfer.types.includes(mimeTypeForAnyThing)) return;

  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
  setIsDragOver(true);
}, [triggerable, mimeTypeForAnyThing]);

const handleInputDragLeave = useCallback(() => {
  setIsDragOver(false);
}, []);

const handleInputDrop = useCallback((e: React.DragEvent) => {
  e.preventDefault();
  setIsDragOver(false);

  if (!triggerable) return;

  const dropInfo = extractDroppedThing(e.dataTransfer);
  if (!dropInfo) return;

  // Build tw-k syntax
  const twkSyntax = `<tw-k>${JSON.stringify(dropInfo.keyJSON)}</tw-k>`;

  // Insert at cursor via CodeMirror
  const view = editorRef.current?.view;
  if (view) {
    const pos = view.state.selection.main.head;
    view.dispatch({
      changes: { from: pos, insert: twkSyntax },
      selection: { anchor: pos + twkSyntax.length },
    });
  }
}, [triggerable, extractDroppedThing]);

// In JSX:
<ThingDropRegion>
  <div
    style={{
      ...existingStyles,
      border: isDragOver ? '2px solid #22c55e' : '1px solid #333',
      background: isDragOver ? 'rgba(34, 197, 94, 0.1)' : undefined,
    }}
    onDragOver={handleInputDragOver}
    onDragLeave={handleInputDragLeave}
    onDrop={handleInputDrop}
  >
    <CodeMirror ... />
  </div>
</ThingDropRegion>
```

## Testing

1. Start clydra and open another thing panel (any type)
2. Drag the thing from its panel onto the input box
3. Verify green highlight appears during drag
4. Drop and verify `<tw-k>...</tw-k>` syntax is inserted
5. Verify it immediately renders as an InlineNodeRef widget
6. Test dropping while streaming (should show red, reject drop)
