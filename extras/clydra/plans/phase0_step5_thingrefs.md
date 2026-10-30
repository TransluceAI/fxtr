# Phase 0 Step 5: Thing References in Messages

## Goal

Extend the `<tw-k>JSON</tw-k>` syntax handling from the input box to also work in:
1. Sent user messages (with raw/rich view toggle)
2. Received assistant messages

Currently this syntax is only detected in the CodeMirror input box via a ViewPlugin. We want it to also render `InlineNodeRef` components in the message history.

## Approach: Extended Sanitize Schema + Custom Component

The cleanest approach is to extend `rehype-sanitize`'s schema to allow `<tw-k>` elements, then provide a custom component to render them:

1. **Extended sanitize schema** that adds `tw-k` to allowed tag names
   - `rehype-raw` parses the HTML into HAST
   - `rehype-sanitize` with extended schema allows `tw-k` through (instead of stripping it)
   - No need for a separate transform plugin

2. **Custom `tw-k` component** that renders `InlineNodeRef`
   - Receives children containing the JSON key
   - Parses JSON and renders `InlineNodeRef`
   - Simple and direct - no need to intercept other elements

This approach:
- Works within Streamdown's existing architecture
- Preserves React context naturally (no widget registry needed)
- Minimal code - just schema extension and one component
- Doesn't intercept unrelated elements like `<code>`

### Implementation

```tsx
import { Streamdown, defaultRehypePlugins } from "streamdown";
import { defaultSchema } from "hast-util-sanitize";

// Extend schema to allow tw-k elements
const twkSanitizeSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames || []), "tw-k"],
};

// Custom rehype plugins with extended sanitize schema
const streamdownRehypePlugins = [
  defaultRehypePlugins.raw,
  [defaultRehypePlugins.sanitize[0], twkSanitizeSchema],
  defaultRehypePlugins.katex,
  defaultRehypePlugins.harden,
];

// Custom tw-k component
function TwKComponent({ children }: { children?: React.ReactNode }) {
  const keyJSON = String(children || "");
  try {
    const parsed = JSON.parse(keyJSON);
    return <InlineNodeRef keyJSON={parsed} />;
  } catch {
    return <span style={{ color: "#ff6b6b" }}>[Invalid ref]</span>;
  }
}

const streamdownComponents = {
  "tw-k": TwKComponent,
};

// Usage with Streamdown
<Streamdown
  rehypePlugins={streamdownRehypePlugins}
  components={streamdownComponents}
>
  {content}
</Streamdown>
```

## Implementation Steps

### 1. Create shared Streamdown configuration
- [x] Add `twkSanitizeSchema` that extends `defaultSchema` to allow `tw-k` elements
- [x] Add `TwKComponent` custom component that renders `InlineNodeRef`
- [x] Create `streamdownRehypePlugins` and `streamdownComponents` for consistent configuration

### 2. Update UserMessageRenderer with raw/rich toggle
- [x] Add `viewMode` state (`"rich" | "raw"`)
- [x] Add `ViewModePicker` to the UI
- [x] In rich mode: render string content with Streamdown + tw-k support
- [x] In raw mode: render as plain text (current `whiteSpace: pre-wrap` behavior)
- [x] Handle array content blocks appropriately (uses existing ContentBlockRenderer which routes TextBlocks to TextBlockRenderer)

### 3. Update TextBlockRenderer (assistant messages)
- [x] Pass the tw-k rehype plugin and custom component to Streamdown
- [x] Already has raw/rich toggle, just needs the plugin added

### 4. Update StreamingContentRenderer
- [x] Pass the tw-k rehype plugin and custom component to Streamdown
- [x] Already has raw/rich toggle, just needs the plugin added

### 5. Update CODE_OVERVIEW.md
- [x] Updated to document rehype plugin and tw-k support in message rendering

## Out of Scope / Future Work

- **Thinking blocks**: Currently thinking blocks render as plain text. Adding tw-k support there could be a follow-up.
- **Tool use/result blocks**: Not transforming tw-k in tool content.
- **Input box raw view**: The input box stays as-is (rich only with CodeMirror decorations).

## Files to Modify

- `clydra/renderers/src/ClydraRoot.tsx` - Main changes
- `CODE_OVERVIEW.md` - Documentation update

## Testing

1. Send a user message containing `<tw-k>{"foo": "bar"}</tw-k>` and verify it renders as InlineNodeRef in rich mode
2. Toggle to raw mode and verify it shows the raw `<tw-k>` syntax
3. Have Claude respond with text containing `<tw-k>` tags and verify they render as InlineNodeRef
4. Verify streaming messages also show InlineNodeRef in rich mode
5. Verify regular code blocks still render normally (no false positives)
