# Phase 0 Step 3e: Syntax Highlighting and Markdown Rendering

## Problem Statement

Currently, clydra displays:
- Tool inputs (Read/Write/Edit) as raw JSON via react-json-view
- Tool results as plain preformatted text
- Assistant text messages as plain preformatted text

This makes it hard to read code in tool calls and doesn't render markdown formatting in assistant responses.

## Solution Overview

1. **Syntax highlighting for code tools** using `shiki` (same as streamdown uses):
   - Read results: Parse line-numbered format, detect language, highlight
   - Write requests: Extract content, detect language from file_path, highlight
   - Edit requests: Two-column diff view with old/new both highlighted

2. **Rich markdown rendering for assistant messages** using `streamdown`:
   - Assistant TextBlocks rendered with Streamdown component
   - Supports streaming with `isAnimating` prop
   - Uses `mode="static"` for completed messages

3. **View mode pickers** for Read/Write/Edit tool calls AND assistant text:
   - Inline toggle button (right side of header) to switch between rich/raw views
   - Default to rich (highlighted/rendered) view
   - Raw view shows original format

## Design Decisions

- **Language detection**: Based on file_path extension only (not content sniffing)
- **Streaming tool inputs**: Do NOT syntax highlight (keep as plain text for performance)
- **Streaming assistant text**: Use Streamdown with `isAnimating={true}` for live updates
- **Line numbers for Read**: Strip original SDK format (`     N→content`), show clean line numbers in rich mode
- **User messages**: Keep as plain text (no markdown rendering for now)

## Files to Change

### `clydra/renderers/ClydraRoot.tsx`

The main file with all the rendering logic.

**New imports:**
```tsx
import { ShikiHighlighter } from "react-shiki";
import { Streamdown } from "streamdown";
```

(Both via esm.sh, similar to existing react-json-view import)

react-shiki handles shiki's async nature internally and provides a simple component API with `showLineNumbers` support.

**New utility functions:**

1. `getLanguageFromPath(filePath: string): string` - Extract language identifier from file extension
   - Map common extensions: `.py` → `python`, `.ts`/`.tsx` → `typescript`, `.js`/`.jsx` → `javascript`, `.rs` → `rust`, etc.
   - Default to `text` for unknown

2. `parseReadResult(text: string): { lines: string[], originalText: string }` - Parse Read tool result
   - Regex to match `^\s*(\d+)→(.*)$` pattern per line
   - Return array of content strings (without line numbers) and original text for raw mode

**Modified renderers:**

1. `TextBlockRenderer` - Add view mode picker:
   - Rich mode (default): Use Streamdown with `mode="static"`
   - Raw mode: Show plain preformatted text like before

2. `StreamingContentRenderer` - For streaming text blocks:
   - Rich mode: Use Streamdown with `isAnimating={true}`
   - Raw mode: Show plain text like before

3. `ToolUseBlockRenderer` - Add logic to detect Write/Edit tools and render specially:
   - If `name === "Write"`: Show `ViewModePicker` + either syntax-highlighted content or raw JSON
   - If `name === "Edit"`: Show `ViewModePicker` + either two-column diff view or raw JSON
   - Otherwise: Keep existing JsonTreeView behavior

4. `ToolResultBlockRenderer` - Needs access to tool name from parent context:
   - If result is for a Read tool: Show `ViewModePicker` + either syntax-highlighted code or raw text
   - Otherwise: Keep existing behavior

**New components:**

1. `ViewModePicker({ mode, onChange })` - Small inline toggle
   - Two states: "rich" | "raw"
   - Styled as small button/toggle, positioned to right of header/tool name

2. `SyntaxHighlightedCode({ code, language, showLineNumbers })` - Code block with highlighting
   - Wraps react-shiki's `ShikiHighlighter` component
   - Props: `language`, `theme`, `showLineNumbers`
   - Uses same theme as streamdown for consistency (e.g., `github-dark`)

3. `EditDiffView({ oldString, newString, language })` - Two-column diff view
   - Side-by-side display
   - Both columns syntax highlighted
   - Labels: "Old" / "New" or similar

4. `WriteContentView({ content, filePath })` - Syntax highlighted file content
   - Detects language from filePath
   - Shows with line numbers

5. `ReadResultView({ result, filePath })` - Syntax highlighted Read result
   - Parses line numbers from SDK format
   - Re-renders with clean highlighting

**Context for tool results:**

Tool results don't directly know which tool they came from. We need to pass context:
- Option A: Look up by `tool_use_id` matching in previous events
- Option B: Pass tool info via React context
- **Chosen: Both** - Use React Context (`EventsContext`) to pass events to child components, then `findToolInfoByUseId()` to scan for matching ToolUseBlock

## Implementation Steps

1. [x] Add react-shiki and streamdown imports to ClydraRoot.tsx
2. [x] Add `getLanguageFromPath()` utility function
3. [x] Add `parseReadResult()` utility function
4. [x] Add `ViewModePicker` component with styling
5. [x] Add `SyntaxHighlightedCode` component
6. [x] Modify `TextBlockRenderer` to use Streamdown with view picker
7. [x] Modify `StreamingContentRenderer` to use Streamdown with `isAnimating`
8. [x] Add `WriteContentView` component for Write tool calls
9. [x] Add `EditDiffView` component for Edit tool calls
10. [x] Add `ReadResultView` component for Read tool results
11. [x] Modify `ToolUseBlockRenderer` to handle Write and Edit specially
12. [x] Add helper to find tool info from tool_use_id in events
13. [x] Modify `ToolResultBlockRenderer` to handle Read results specially
14. [ ] Test with various file types and edge cases
15. [x] Update CODE_OVERVIEW.md

## Notes

- Shiki supports many languages out of the box; should cover most file types
- Both streamdown and our tool highlighting use shiki, ensuring consistent code appearance
- react-shiki handles async internally via `ShikiHighlighter` component
- The SDK's Read format uses `→` (unicode arrow), not ASCII `>`
- Should use the same shiki theme that streamdown uses (e.g., `github-dark`)

---

## Enhancement: Diff Views for Edit Tool

### Problem

The current `EditDiffView` shows old/new strings in two separate columns, both syntax highlighted. This doesn't show which specific lines changed - you have to visually compare the columns yourself.

### Solution

Three view modes for Edit tool:
1. **Unified** (default): Single column with all lines, removed lines red, added lines green
2. **Split**: Two columns - left shows old (removed lines red), right shows new (added lines green)
3. **Raw**: JSON view of the tool input (existing behavior)

All modes use `diffLines` from the `diff` package to compute changes, then format with prefixes for the shiki transformer.

### Transformer Approach

Based on https://usagi.io/articles/2024-04-24-adding-diff-highlighting-to-markdown-using-shiki/

The transformer processes code where lines are prefixed with `+`, `-`, or ` `:
```
-console.log('hewwo')
+console.log('hello')
 console.log('goodbye')
```

It strips the prefix character and adds CSS classes (`add`/`remove`) for styling. This avoids issues with comment-based notation (`// [!code ++]`) which is language-specific.

**Modification**: The original article's transformer only handles `+` and `-`. We modify it to also expect a space prefix for unchanged lines (and strip it), so all lines have consistent single-character prefixes.

### Diff Formatting

`computeDiff(oldStr, newStr)` returns structured diff data:
```ts
interface DiffLine {
  type: 'add' | 'remove' | 'unchanged';
  content: string;
}
```

Then we have formatters:
- `formatUnifiedDiff(lines)`: All lines with appropriate prefix (`+`/`-`/` `)
- `formatSplitLeft(lines)`: Only `remove` and `unchanged` lines (prefix `-`/` `)
- `formatSplitRight(lines)`: Only `add` and `unchanged` lines (prefix `+`/` `)

### react-shiki Integration

react-shiki's `ShikiHighlighter` accepts a `transformers` prop. We pass our custom transformer there - no need for direct `codeToHtml` calls.

### View Mode Picker

Update `ViewModePicker` to support three modes for Edit tool:
- "unified" | "split" | "raw"
- Show three buttons instead of two when in diff mode

### Implementation Steps

1. [x] Add `diff` package import to formatting.tsx
2. [x] Create `transformerDiffNotation()` - custom shiki transformer that:
   - Strips `+`/`-`/` ` prefix from each line's first text node
   - Adds `add`/`remove` class to the line element
3. [x] Create `computeDiff(oldStr, newStr)` utility:
   - Uses `diffLines` to get change hunks
   - Returns array of `DiffLine` objects
4. [x] Create diff formatting functions:
   - `formatUnifiedDiff(lines)` - all lines with prefixes
   - `formatSplitLeft(lines)` - remove + unchanged
   - `formatSplitRight(lines)` - add + unchanged
5. [x] Create `DiffHighlightedCode` component:
   - Like `SyntaxHighlightedCode` but passes the diff transformer
6. [x] Create `diff.module.css` with scoped styles for `.add` and `.remove`
7. [x] Update `EditDiffView` with three-mode picker:
   - Unified mode: single `DiffHighlightedCode` with full diff
   - Split mode: two columns, each with `DiffHighlightedCode`
   - Raw mode: existing JSON view
8. [x] Consolidate ClydraRoot.tsx to use shared EditDiffView/WriteContentView
9. [x] Register diff.module.css in main.py imports
