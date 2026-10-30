# Phase 2 Misc 3: Show Trigger Records for Dynamic Renderers

## Problem Statement

Trigger records do not appear in the DetailsPanel when a thing has a custom (dynamic) renderer. They should show up under the rendering, similar to how backlinks are displayed.

## Investigation Findings

### Current Code Structure in `DetailsPanel.tsx`

The DetailsPanel has a conditional rendering structure:

```tsx
{customRenderer && rendererProps ? (
  // Custom renderer path (lines 193-239)
  <div className={styles.customRenderer}>
    <DynamicRenderer ... />
  </div>
) : (
  // Fallback path (lines 240-341)
  <>
    {/* Trigger form (if triggerable) */}
    {/* Data section */}
    {/* Trigger records section */}  // <-- ONLY shown here!
    {/* Links section */}
  </>
)}

{/* Backlinks section (lines 345-377) */}
{thing.backlinks && thing.backlinks.length > 0 && (
  <div className={styles.section}>
    {/* ... backlinks ... */}
  </div>
)}
```

### The Issue

- **Trigger records** (lines 284-304): Currently rendered **inside** the fallback path (the `else` branch). When a custom renderer is present, this entire branch is skipped.

- **Backlinks** (lines 345-377): Rendered **outside** the conditional, after both paths. They appear for **all** things regardless of whether they have a custom renderer.

### Result

| Feature | With Custom Renderer | Without Custom Renderer |
|---------|---------------------|------------------------|
| Custom content | ✅ Shown | N/A |
| Trigger records | ❌ NOT shown | ✅ Shown |
| Backlinks | ✅ Shown | ✅ Shown |

## Solution

Move the trigger records section outside the `{customRenderer ? (...) : (...)}` conditional, positioning it similar to backlinks. This will make trigger records visible for all things, regardless of whether they have a custom renderer.

## Implementation Steps

- [x] 1. In `DetailsPanel.tsx`, extract the trigger records section (lines 284-304) from the fallback branch
- [x] 2. Place it after the custom renderer conditional and before or after the backlinks section
- [ ] 3. Test that trigger records now appear for things with custom renderers
- [ ] 4. Update CODE_OVERVIEW.md (minor update to DetailsPanel description if needed)

## Code Change Preview

```tsx
// After the customRenderer conditional closes...

        {/* Trigger records section - now shown for ALL things */}
        {triggerRecords.length > 0 && (
          <div className={styles.section}>
            <h4 className={styles.sectionTitle}>Triggered at this tick</h4>
            <div className={styles.triggerRecords}>
              {triggerRecords.map(tr => (
                <div key={tr.id} className={styles.triggerRecord}>
                  <span className={styles.triggerRecordId}>#{tr.id}</span>
                  {tr.payload !== null && (
                    <div className={styles.triggerRecordPayload}>
                      <JsonTree data={tr.payload} />
                    </div>
                  )}
                  {tr.payload === null && (
                    <span className={styles.noData}>(no payload)</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Backlinks section - already shown outside custom renderers */}
        {thing.backlinks && thing.backlinks.length > 0 && (
          ...
        )}
```

## Ordering Consideration

The sections after the custom renderer should appear in this order:
1. Trigger records (new position) - contextual info about what triggered this thing at this tick
2. Backlinks - structural info about relationships

This puts the more "event-like" information (trigger records) before the more "static" relationship information (backlinks).

## Testing

1. Create a thing type with a custom renderer registered
2. Trigger that thing
3. Verify the trigger record appears in the DetailsPanel below the custom renderer content
4. Verify backlinks still appear as expected
