# Scratch notes for phase0_step3c

## Feature breakdown

### 1. Message folding (all messages collapsible, expanded by default)

Currently:
- UserMessage: renders as a box with "You:" label, content below
- AssistantMessage: renders as a box with "Claude:" label, content below
- These are NOT in `<details>` elements

Need:
- Wrap in `<details open>` so they're collapsible but start expanded
- Summary should be on one line: "▶ You: [preview]" or "▶ Claude: [preview]"
- When expanded, full content shows

Implementation:
- Modify `UserMessageRenderer` and `AssistantMessageRenderer`
- Use `<details open>` with summary containing label + preview
- Content goes inside details body

### 2. JSON tree view with react-json-view

Currently: `JSON.stringify(obj, null, 2)` in a `<pre>`/code block

Need: Interactive tree view using react-json-view

Implementation:
- Add react-json-view as a dependency (need to figure out how - it's a React component)
- Actually, this is a TSX renderer running in thingwatcher's Deno environment
- Need to check if react-json-view works in Deno or if we need an alternative
- Could use: https://www.npmjs.com/package/react-json-view
- Or a simpler alternative that works with Deno

Actually, thingwatcher renderers run in the browser via the thingwatcher server - they're compiled/bundled. So we need a package that:
1. Is available via npm/esm
2. Works with React

react-json-view or @microlink/react-json-view should work.

Places to add JSON tree view:
- `ToolUseBlockRenderer` - tool input
- `ToolResultBlockRenderer` - result content (when JSON)
- `ResultMessageRenderer` - full event dump
- Unknown event/block fallbacks
- SystemMessage details

### 3. Tool results show as "Tool Result" not "You"

Currently: Tool results are `ToolResultBlock` inside `UserMessage` content.
`UserMessageRenderer` shows "You:" for all user messages.

Need: Detect when UserMessage content is ALL tool results, show differently.

Implementation:
- In `UserMessageRenderer`, check if all content blocks are `ToolResultBlock`
- If so, render as "Tool Result" instead of "You"
- Or: create separate `ToolResultMessageRenderer`

### 4. Result streaming for tool calls (input_json_delta)

Currently: `aggregateStreamEvents` handles:
- `content_block_start` (text, thinking)
- `content_block_delta` (text_delta, thinking_delta)
- `content_block_stop`
- `message_stop`

Need: Also handle tool_use blocks and input_json_delta:
- `content_block_start` with `type: "tool_use"` - has `id`, `name`
- `content_block_delta` with `type: "input_json_delta"` - has `partial_json`

The partial_json pieces concatenate to form the full JSON input.

Implementation:
- Add `tool_use` type to `StreamingBlock`
- In `content_block_start`, handle `type: "tool_use"`
- In `content_block_delta`, handle `delta.type === "input_json_delta"`
- Render streaming tool use with name and accumulating JSON

### 5. Collapsed boxes single line (headers in summary)

This overlaps with #1 - when collapsed, show single line like:
"▶ You: How do I..." or "▶ Claude: Let me help..."

For nested blocks (thinking, tool use), they're already using `<details>` with summary.
The issue is the outer message wrapper.

### 6. Session ID clickable to copy transcript path

Currently:
- `sessionId` shown in status bar
- `transcript_path` received in hook but not stored

Need:
- Store transcript_path on manager
- Pass to frontend
- Make session ID clickable, copies transcript path

Implementation:
- Add `transcript_path: str | None` to ClydraManager
- In `on_user_prompt` hook, store `input_data.get("transcript_path")` on manager
- Include in snapshot data
- In renderer, make session ID a clickable element that copies path

## Dependency question: react-json-view

Options:
1. `react-json-view` - popular but may have issues
2. `@microlink/react-json-view` - fork, maintained
3. `react-json-tree` - alternative
4. Build simple custom tree component

Let's go with trying react-json-view first, or a simple custom implementation if that doesn't work in the thingwatcher Deno environment.

Actually, looking at the deno.json in renderers/, the imports come from esm.sh which can serve npm packages. So we should be able to import react-json-view from esm.sh.

## Order of implementation

1. Add transcript_path to manager and hook
2. Message folding (UserMessage, AssistantMessage)
3. Tool Result label fix
4. JSON tree view component
5. Streaming tool use (input_json_delta)
6. Session ID clickable
7. Update CODE_OVERVIEW.md
