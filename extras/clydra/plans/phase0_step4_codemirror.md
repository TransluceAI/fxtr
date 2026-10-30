# Phase 0 Step 4: CodeMirror 6 Input with Mentions

## Problem Statement

The current input box is a basic `<textarea>` that doesn't support:
- Rich autocompletion for file references (@-mentions)
- Slash command autocompletion
- Inline thing references (`<tw-i>N</tw-i>` syntax rendered as InlineNodeRef)

## Solution Overview

Replace the textarea with CodeMirror 6 using `@uiw/react-codemirror`, configured for:
1. Markdown formatting (syntax highlighting for markdown in the input)
2. No line numbers (keep it simple, chat-like)
3. `@`-mentions for filenames (autocomplete from file list provided by backend)
4. `/`-commands for slash commands (autocomplete at start of input only)
5. `<tw-i>N</tw-i>` syntax transformed into atomic decorations containing `InlineNodeRef`

## Design Decisions

### CodeMirror Configuration
- Use `@uiw/react-codemirror` for React integration
- Use `@codemirror/lang-markdown` for syntax highlighting
- Disable line numbers via `basicSetup` options
- Dark theme to match existing UI
- Enter to send (intercept Enter key), Shift+Enter for newline

### File List Source
Create a new backend thing `FileListController` that:
- Scans the cwd directory recursively (large max depth, e.g. 10-20 levels)
- Snapshots the file list at each tick
- Frontend component subscribes to changes via `onFilesChange` callback prop (passed as renderer arg)
- This pattern allows the root component to pass file data to the CodeMirror mentions extension

### Slash Commands Source
- Extract from the init `SystemMessage` event in the events array
- Find event where `data.subtype === "init"` and read `data.slash_commands` (array of strings like `["/compact", "/clear", ...]`)
- Ideally trigger completion only when `/` is at position 0, but if the extension doesn't support that, anywhere is fine

### Mentions Extension
Use `@uiw/codemirror-extensions-mentions` for both `@` and `/` triggers. The extension needs:
- Custom data source (dynamic, from file list thing and events)
- `@` trigger for files, `/` trigger for commands
- May need two separate mention extension instances with different configs

If the uiw extension doesn't provide enough control, we can use CodeMirror's built-in autocomplete system (`@codemirror/autocomplete`) directly with custom completion sources.

### Thing References (`<tw-k>JSON</tw-k>`)
This is a CodeMirror decoration that:
1. Matches the pattern `<tw-k>...</tw-k>` in the document
2. Attempts to parse the interior as JSON
3. If valid JSON, replaces it visually with an atomic widget containing `InlineNodeRef`
4. If invalid JSON, leaves the text unchanged (graceful degradation)

Implementation approach:
- Create a ViewPlugin that scans for `<tw-k>...</tw-k>` tags
- For each match, try `JSON.parse()` on the interior
- If parse succeeds, create Decoration.replace with a custom WidgetType
- The WidgetType:
  - Creates a DOM container in `toDOM()`
  - Uses `ReactDOM.createRoot()` to render `InlineNodeRef` with the parsed keyJSON
  - Handles cleanup in `destroy()` method
- InlineNodeRef updates live as the referenced thing changes (built-in behavior)

## Files to Change

### New: `clydra/file_list.py`
New module for file list tracking:
- `FileListController` dataclass with `files: list[str]` and `cwd: str`
- `FileListControllerSnapshotter` that rescans directory on snapshot
- `FILE_LIST_KEY = ["clydra_file_list"]`

### Modify: `clydra/main.py`
- Import and register FileListController snapshotter
- Create FileListController instance at startup
- Track it alongside other controllers
- Register its renderer

### New: `clydra/renderers/FileListIndicator.tsx`
Small renderer that:
- Receives `onFilesChange` callback via args
- Uses `usePayload()` to get file list
- Calls `onFilesChange(files)` via useEffect when payload changes
- Renders a small badge showing file count (or nothing if not needed visually)

### Modify: `clydra/renderers/ClydraRoot.tsx`
Major changes to the input section:
1. Replace `<textarea>` with CodeMirror
2. Add state for file list (updated via FileListIndicator callback)
3. Extract slash commands from init event in events list
4. Configure CodeMirror extensions for mentions
5. Create the `<tw-i>` decoration view plugin
6. Handle Enter key for sending (prevent default, call handleSend)

New imports needed:
```tsx
import CodeMirror from "@uiw/react-codemirror";
import { markdown } from "@codemirror/lang-markdown";
import { EditorView, ViewPlugin, Decoration, WidgetType } from "@codemirror/view";
// Possibly: import { mentions } from "@uiw/codemirror-extensions-mentions";
// Or: import { autocompletion } from "@codemirror/autocomplete";
```

### Possible: `clydra/renderers/ThingRefWidget.tsx`
Separate file for the widget class and portal logic if it gets complex.

## Implementation Steps

1. [x] Create `clydra/file_list.py` with FileListController, snapshotter, and key
2. [x] Register FileListController in `main.py` (snapshotter, thing creation, renderer)
3. [x] Create `FileListIndicator.tsx` renderer with onFilesChange callback pattern
4. [x] Add basic CodeMirror setup to ClydraRoot.tsx (replace textarea, markdown mode, no line numbers, dark theme)
5. [x] Implement Enter key handling for send (preserve Shift+Enter for newline)
6. [x] Add file list state to ClydraRoot with FileListIndicator integration
7. [x] Add slash command extraction from init event
8. [x] Implement @-mention completion for files (via uiw extension)
9. [x] Implement /-command completion (combined with file mentions via uiw extension)
10. [x] Create `<tw-k>` decoration plugin with WidgetType and React portal
11. [x] Test with various inputs and edge cases
12. [x] Update CODE_OVERVIEW.md

## Technical Notes

### CodeMirror Decoration with React Portal
The pattern for rendering React inside CodeMirror decorations:

```tsx
class ThingRefWidget extends WidgetType {
  constructor(private keyJSON: unknown) {
    super();
  }

  toDOM() {
    const container = document.createElement("span");
    container.className = "tw-inline-ref";
    // Store root for cleanup
    const root = ReactDOM.createRoot(container);
    root.render(<InlineNodeRef keyJSON={this.keyJSON} />);
    (container as any).__reactRoot = root;
    return container;
  }

  destroy(dom: HTMLElement) {
    const root = (dom as any).__reactRoot;
    if (root) root.unmount();
  }

  eq(other: ThingRefWidget) {
    // Compare keyJSON for equality
    return JSON.stringify(this.keyJSON) === JSON.stringify(other.keyJSON);
  }
}
```

### Decoration Plugin Pattern
Scan document for `<tw-k>...</tw-k>` and create decorations:

```tsx
const twkDecorations = ViewPlugin.fromClass(class {
  decorations: DecorationSet;

  constructor(view: EditorView) {
    this.decorations = this.buildDecorations(view);
  }

  update(update: ViewUpdate) {
    if (update.docChanged || update.viewportChanged) {
      this.decorations = this.buildDecorations(update.view);
    }
  }

  buildDecorations(view: EditorView) {
    const widgets: Range<Decoration>[] = [];
    const doc = view.state.doc.toString();
    const regex = /<tw-k>([\s\S]*?)<\/tw-k>/g;
    let match;
    while ((match = regex.exec(doc)) !== null) {
      try {
        const keyJSON = JSON.parse(match[1]);
        widgets.push(
          Decoration.replace({
            widget: new ThingRefWidget(keyJSON),
          }).range(match.index, match.index + match[0].length)
        );
      } catch {
        // Invalid JSON, skip this match
      }
    }
    return Decoration.set(widgets);
  }
}, { decorations: v => v.decorations });
```

### File List Scanning
Backend scans recursively with configurable max depth:
- Use `os.walk()` with depth tracking
- Large max depth (e.g., 15-20 levels)
- Return relative paths from cwd
- Include both files and directories (directories useful for @-mentioning paths)

## Implementation Notes / Things to Revisit

1. **Mentions extension behavior** - Both `@` and `/` mentions are combined into a single `mentions()` call by prefixing files with `@` in their labels. Not 100% certain the uiw extension correctly triggers on both characters. If autocomplete only works for one trigger, may need to use two separate mentions extensions or switch to CodeMirror's native `@codemirror/autocomplete` with custom completion sources.

2. **React portal context** - ~~The `ThingRefWidget` creates a separate React root via `createRoot()` to render `InlineNodeRef`. This might not have access to thingwatcher's React context (graphcache, etc.).~~ **FIXED**: Refactored to use a widget registry + `createPortal()` from ClydraRoot, preserving React context.

3. **Enter key vs autocomplete** - The Enter key handler is on the wrapper div. When the autocomplete dropdown is open, pressing Enter might both select an autocomplete option AND try to send the message. May need to use CodeMirror's keymap extension with proper event handling instead.

4. **File scanning frequency** - `FileListControllerSnapshotter.rescan()` runs on every tick/snapshot. Now uses `fd` which is fast, but for very large repos might still want caching/debouncing.

## Follow-up Fixes (2026-01-16)

### Issues Identified

1. **Dark theme not applying** - CodeMirror shows light background despite custom theme in extensions
2. **Enter on autocomplete sends message** - Pressing Enter to select autocomplete also triggers message send
3. **Slash commands not working** - `/command` autocomplete doesn't trigger
4. **Tab should select autocomplete** - Tab key should accept the current autocomplete suggestion

### Root Causes

1. **Theme**: `@uiw/react-codemirror` has a `theme` prop that defaults to `"light"`. The custom theme extension styles the editor but doesn't override this base theme class.

2. **Enter key**: The `onKeyDown` handler on the wrapper div intercepts Enter before CodeMirror's autocomplete can process it.

3. **Slash commands**: The `@uiw/codemirror-extensions-mentions` extension is hardcoded to only match `@(\w+)?` regex - no support for other trigger characters like `/`.

### Implementation Plan

1. [x] **Fix theme**: Add `theme="dark"` prop to the CodeMirror component.

2. [x] **Fix Enter/Tab handling**:
   - Remove `onKeyDown` from wrapper div
   - Create a custom keymap extension using `keymap.of([...])` from `@codemirror/view`
   - Use `completionStatus()` from `@codemirror/autocomplete` to check if autocomplete is open
   - Only trigger send when autocomplete is NOT open and Enter is pressed without Shift
   - Configure autocomplete to use Tab for accepting completions via `acceptCompletion` command

3. [x] **Fix slash commands**:
   - Replace `mentions()` extension with custom `autocompletion({ override: [...] })`
   - Create two completion sources:
     - One matching `@[\w/.+-]*` for file mentions (supports paths like `@src/foo.ts`)
     - One matching `/\w*` for slash commands (only at start of line)
   - Each source returns appropriate options from fileList and slashCommands

### Implementation Notes

- Used `handleSendRef` (a ref) to store the send function, avoiding recreation of the keymap on every state change
- The slash command completion only triggers at the start of a line (checks `textBefore.trim() === ""`)
- File completion matches on `includes()` rather than `startsWith()` for more flexible matching
- Results limited to 50 files to avoid overwhelming the dropdown
