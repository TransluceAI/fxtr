# Phase 0 Step 3c: Folding, JSON Tree, Tool Result Label, Streaming Tool Input

## Features

1. **Message folding** - All messages (user/assistant) collapsible with manual triangle indicator
2. **JSON tree view** - Interactive collapsible tree for JSON displays using react-json-view
3. **Tool Result label** - Tool result messages show as "Tool Result" not "You"
4. **Streaming tool input** - Support `input_json_delta` for live tool input display
5. **Single-line collapsed** - Headers in summary with preview only when collapsed ("▶ You: preview...")
6. **Clickable session ID** - Copies transcript path to clipboard
7. **Smart default collapse** - Tool results and thinking/tool messages start collapsed
8. **Transcript parsing fix** - Map `type` field to `_block_type` for restored sessions

## Problem Details

### Current state
- User/assistant messages are static boxes, not collapsible
- JSON displays use `JSON.stringify(obj, null, 2)` in code blocks
- Tool results appear inside UserMessage with "You:" label
- Streaming only handles text/thinking deltas, not tool use input
- Message headers are separate from collapsible content
- Session ID shown but not interactive

### Desired state
- All messages wrapped in `<details open>` for collapsibility
- JSON rendered as interactive tree with expand/collapse per node
- Messages containing only tool results labeled "Tool Result"
- Tool use inputs stream in real-time via `input_json_delta`
- Collapsed view shows single line: "▶ Claude: [preview]"
- Clicking session ID copies transcript path

## Implementation

### 1. Add transcript_path to manager and data flow

**manager.py:**
- Add `transcript_path: str | None = None` field to ClydraManager

**main.py:**
- In `on_user_prompt` hook, store `input_data.get("transcript_path")` on manager
- This captures the path both for new sessions and resumed ones

**snapshotter.py:**
- Include `transcript_path` in snapshot data alongside `session_id`

### 2. Message folding with single-line headers

**ClydraRoot.tsx - UserMessageRenderer:**
```tsx
// Before: static div with "You:" label
// After: <details open> with summary containing label + preview
<details open style={styles.userMessage}>
  <summary style={styles.messageSummary}>
    <span style={styles.label}>You:</span>
    <span style={styles.preview}>{getPreview(content)}</span>
  </summary>
  <div style={styles.messageContent}>
    {/* full content */}
  </div>
</details>
```

**AssistantMessageRenderer:** Same pattern with "Claude:" label

**Helper function:** `getPreview(content)` - extract first ~50 chars of text content

### 3. Tool Result label detection

**ClydraRoot.tsx - UserMessageRenderer:**
```tsx
// Check if all content blocks are tool results
const isAllToolResults = Array.isArray(content) &&
  content.every(block => block._block_type === "ToolResultBlock");

const label = isAllToolResults ? "Tool Result" : "You";
```

### 4. JSON tree view component

**Add react-json-view import (direct URL, pinned version):**
```tsx
import ReactJson from "https://esm.sh/react-json-view@1.21.3";
```

**Create JsonTreeView wrapper:**
```tsx
function JsonTreeView({ data }: { data: unknown }) {
  if (typeof data !== "object" || data === null) {
    return <span>{String(data)}</span>;
  }
  return (
    <ReactJson
      src={data as object}
      theme="monokai"
      displayDataTypes={false}
      collapsed={1}
      style={{ background: "transparent" }}
    />
  );
}
```

**Replace JSON.stringify calls:**
- `ToolUseBlockRenderer`: tool input
- `ToolResultBlockRenderer`: result content (when object)
- `ResultMessageRenderer`: event details
- Unknown block/event fallbacks

### 5. Streaming tool input (input_json_delta)

**Types - add to StreamingBlock:**
```tsx
interface StreamingBlock {
  type: "text" | "thinking" | "tool_use";
  content: string;
  toolName?: string;  // for tool_use blocks
  toolId?: string;    // for tool_use blocks
}
```

**aggregateStreamEvents updates:**
```tsx
case "content_block_start": {
  const block = streamEvent.content_block;
  // ... existing text/thinking handling ...
  if (block?.type === "tool_use") {
    state.blocks.push({
      type: "tool_use",
      content: "",  // JSON accumulates here
      toolName: block.name,
      toolId: block.id,
    });
    state.currentBlockIndex = state.blocks.length - 1;
  }
}

case "content_block_delta": {
  const delta = streamEvent.delta;
  // ... existing text_delta/thinking_delta handling ...
  if (delta?.type === "input_json_delta" && delta.partial_json) {
    state.blocks[state.currentBlockIndex].content += delta.partial_json;
  }
}
```

**StreamingContentRenderer - render tool_use blocks:**
```tsx
{block.type === "tool_use" && (
  <details style={styles.details} open>
    <summary style={styles.summary}>
      <span style={styles.label}>Tool:</span>
      <span style={styles.toolName}>{block.toolName}</span>
    </summary>
    <div style={styles.codeBlock}>
      {block.content || "..."}
    </div>
  </details>
)}
```

### 6. Clickable session ID

**ClydraRoot.tsx - status bar:**
```tsx
{sessionId && transcriptPath && (
  <span
    style={{ cursor: "pointer", textDecoration: "underline" }}
    onClick={() => navigator.clipboard.writeText(transcriptPath)}
    title={`Click to copy: ${transcriptPath}`}
  >
    Session: <span style={{ color: "#888" }}>{sessionId}</span>
  </span>
)}
```

## Files to Change

1. **clydra/manager.py** - Add `transcript_path` field
2. **clydra/main.py** - Store transcript_path from hook
3. **clydra/snapshotter.py** - Include transcript_path in snapshot
4. **clydra/renderers/ClydraRoot.tsx** - All UI changes:
   - Add react-json-view import
   - Create JsonTreeView component
   - Refactor UserMessageRenderer (folding + tool result label)
   - Refactor AssistantMessageRenderer (folding)
   - Update aggregateStreamEvents for tool_use + input_json_delta
   - Update StreamingContentRenderer for tool_use blocks
   - Update status bar for clickable session ID
   - Replace JSON.stringify with JsonTreeView
   (react-json-view imported directly from esm.sh URL, no deno.json changes needed)

## Implementation Order

1. [x] Add transcript_path to manager.py
2. [x] Store transcript_path in main.py hook
3. [x] Include transcript_path in snapshotter.py
4. [x] Add JsonTreeView component and react-json-view import
5. [x] Refactor UserMessageRenderer (folding + preview + tool result label)
6. [x] Refactor AssistantMessageRenderer (folding + preview)
7. [x] Update aggregateStreamEvents for tool_use/input_json_delta
8. [x] Update StreamingContentRenderer for tool_use blocks
9. [x] Make session ID clickable
10. [x] Replace JSON.stringify calls with JsonTreeView
11. [x] Update CODE_OVERVIEW.md

### Additional refinements (added during implementation)

12. [x] Add manual triangle indicator (CSS border-based, since display:flex breaks native triangle)
13. [x] Show preview only when collapsed (track open state, conditionally render preview)
14. [x] Tool Result messages start collapsed by default
15. [x] Assistant messages with thinking/tool_use start collapsed by default
16. [x] Fix transcript parsing - add `add_block_types()` to map `type` to `_block_type`
17. [x] Remove redundant inner collapsers from ThinkingBlock, ToolUseBlock, ToolResultBlock renderers

## Design Decisions

- **react-json-view**: Popular library with dark theme support. Required type cast to work around React 17/18 type mismatch.
- **Tool result detection**: Check `_block_type === "ToolResultBlock"` on all content blocks. If all are tool results, use "Tool Result" label.
- **Preview generation**: First ~60 chars of text content, ellipsis if truncated. For tool results, show count. For thinking, show `[Thinking] preview...`. For tool use, show `[Tool: name] description...`.
- **Streaming tool JSON**: Accumulate `partial_json` strings. Display may be invalid JSON mid-stream but that's expected/acceptable.
- **Transcript path timing**: Starts as null, populated on first hook callback. UI handles null gracefully (session ID not clickable until path known).
- **Manual triangle**: Native `<details>` disclosure triangle breaks with `display: flex` on summary. Solution: CSS border-based triangle with rotation transform for open/closed states.
- **Smart default collapse**: Tool results and messages with thinking/tool_use blocks start collapsed to reduce visual noise. Users can expand to see details. Plain text messages remain expanded.
- **No nested collapsers**: Content blocks (thinking, tool use, tool result) render inline without their own `<details>`. The top-level message collapse is sufficient.
- **Transcript block type mapping**: Restored sessions have `type` field (e.g., "text", "thinking", "tool_use", "tool_result") but need `_block_type` (e.g., "TextBlock", "ThinkingBlock") for renderer dispatch.
