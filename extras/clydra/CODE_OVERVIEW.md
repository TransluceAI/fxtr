# Clydra Code Overview

A thingwatcher-based frontend for Claude Code, using the Claude Agent SDK.

## Directory Structure

```
clydra/
├── pyproject.toml           # Package config (uv, deps on thingwatcher + claude-agent-sdk)
├── CODE_OVERVIEW.md         # This file
├── CLAUDE.md                # Instructions for Claude
├── plans/                   # Implementation plans
│   ├── phase0_overview.md   # High-level goals
│   ├── phase0_step1_setup.md # Detailed setup plan
│   ├── phase0_step2_extra_io.md # Interrupt, permission mode, permission dialogs
│   └── phase0_step3e_formatting.md # Syntax highlighting and markdown rendering
├── specs/                   # Design specifications (if any)
└── clydra/                  # Python package
    ├── __init__.py          # Re-exports ClydraManager
    ├── manager.py           # ClydraManager dataclass
    ├── snapshotter.py       # StatefulOwningSnapshotter + AnchorCapturer
    ├── interrupt.py         # InterruptController for interrupt button
    ├── permission_mode.py   # PermissionModeController for mode selector
    ├── model.py             # ModelController for model picker
    ├── permission_dialog.py # PermissionDialogController for tool permission dialogs
    ├── file_list.py         # FileListController for @-mention file autocompletion
    ├── main.py              # Entry point, worker loop, session management
    └── renderers/           # TSX renderers for thingwatcher frontend
        ├── package.json     # npm dependencies (esbuild, codemirror, etc.)
        ├── build.mjs        # esbuild build script
        ├── .gitignore       # Ignores node_modules/ and dist/
        ├── dist/            # Built output (run `npm run build` to generate)
        │   ├── main.js      # Bundled ESM module
        │   └── main.css     # Extracted CSS
        └── src/             # Source files
            ├── index.tsx        # Entry point, exports all renderers
            ├── ClydraRoot.tsx   # Main conversation renderer with CodeMirror input
            ├── formatting.tsx   # Shared syntax highlighting utilities
            ├── diff.module.css  # CSS module for diff highlighting
            ├── InterruptButton.tsx  # Interrupt button renderer
            ├── PermissionMode.tsx   # Permission mode dropdown renderer
            ├── ModelPicker.tsx      # Model picker text input renderer
            ├── PermissionDialog.tsx # Permission dialog renderer
            └── FileListIndicator.tsx # File list indicator (provides file list to parent)
```

## Key Concepts

- **ClydraManager**: The main tracked thing with key `["clydra_root"]`. Holds session state, events list, and SDK client. Links to child things for interrupt, permission mode, and permission dialog.
- **Events**: Raw SDK messages converted to dicts via `dataclasses.asdict()`. Managed via `ClydraManager` methods that record operations for the snapshotter.
- **State**: One of `"setup"`, `"streaming"`, or `"waiting_input"`. Included in each delta chunk so frontend reads from last chunk.
- **Worker Loop**: Waits on `watcher.receive()` for user input, sends to SDK, streams responses via `streaming_update()`.
- **Streaming**: With `include_partial_messages=True`, the SDK emits `StreamEvent` messages containing raw Anthropic API deltas. The renderer aggregates these into live-updating text/thinking blocks.
- **Child Things**: Five separate thingwatcher things handle async operations:
  - **InterruptController** (`["clydra_interrupt"]`): Interrupt button, has its own `receive()` loop in a separate task
  - **PermissionModeController** (`["clydra_permission_mode"]`): Permission mode dropdown with Shift+Tab cycling
  - **ModelController** (`["clydra_model"]`): Model picker text input, calls `client.set_model()` on submit
  - **PermissionDialogController** (`["clydra_permission_dialog"]`): Tool permission dialogs that block on `receive()` for user response
  - **FileListController** (`["clydra_file_list"]`): Recursively scans cwd for files, provides list for @-mention autocompletion
- **Init Message Handling**: When a `SystemMessage` with `subtype: "init"` is received, the model and permission mode controllers are updated from `data.model` and `data.permissionMode`. Slash commands are extracted from `data.slash_commands` for autocomplete.
- **CodeMirror Input**: The input area uses CodeMirror 6 with:
  - Markdown syntax highlighting
  - `@`-mentions for file autocompletion (data from FileListController)
  - `/`-mentions for slash command autocompletion (data from init message)
  - `<tw-k>JSON</tw-k>` syntax for inline thing references (rendered as InlineNodeRef via React portal in input box, and via rehype plugin in rendered messages)

## Module Breakdown

### `clydra/manager.py`
The `ClydraManager` dataclass:
- `session_id`: Current SDK session ID (from `ResultMessage`)
- `transcript_path`: Path to session transcript file (from hook)
- `state`: Current state for UI display
- `cwd`: Working directory for Claude Code
- `_events`: Private list of message dicts (use methods to modify)
- `_pending_ops`: Operation log for snapshotter (push/pop/clear operations)
- `_client`: Runtime-only `ClaudeSDKClient` instance
- `_transcript_loaded`: Flag for resume history loading

Event management methods:
- `append_event(event)`: Append one event, records `{"op": "push", "count": 1}`
- `append_events(events)`: Append multiple events, records `{"op": "push", "count": N}`
- `clear_events()`: Clear all events, records `{"op": "clear"}`
- `pop_events(n)`: Pop N events from end, records `{"op": "pop", "count": N}`
- `get_events()`: Read-only access to events list
- `_consume_pending_ops()`: Returns and clears pending ops (for snapshotter only)

### `clydra/snapshotter.py`
- `ClydraManagerSnapshotter`: StatefulOwningSnapshotter that cooperates with manager's pending ops to compute minimal deltas.
  - Tracks `_chunk_event_counts` per manager: how many events in each emitted chunk
  - Processes pending ops (push/pop/clear) to compute `pop_chunks` and events suffix to push
  - Uses algorithm: `suffix_len` tracks events to push; on pop, "borrows" from chunks if needed
  - **Important**: Must track every pushed chunk (including empty state-only changes) to keep chunk counts in sync with database
- `ClydraManagerAnchorCapturer`: Returns `ThingRef` to the root thing for `watcher.receive()`.
- `CLYDRA_ROOT_KEY`: The thing key `["clydra_root"]`.
- `CHILD_LINKS`: List of `ThingRef` to child things (interrupt, permission mode, model, permission dialog, file list).

### `clydra/interrupt.py`
- `InterruptController`: Dataclass with `enabled` flag. Has its own `receive()` loop in a separate task.
- `InterruptControllerSnapshotter`: Simple snapshotter returning `{"enabled": bool}`.
- `INTERRUPT_KEY`: The thing key `["clydra_interrupt"]`.

### `clydra/permission_mode.py`
- `PermissionModeController`: Dataclass with `current_mode` field (one of `"default"`, `"acceptEdits"`, `"plan"`).
- `PermissionModeControllerSnapshotter`: Simple snapshotter returning `{"current_mode": str}`.
- `PERMISSION_MODES`: List of valid permission modes.
- `PERMISSION_MODE_KEY`: The thing key `["clydra_permission_mode"]`.

### `clydra/model.py`
- `ModelController`: Dataclass with `current_model` (string) and `error` (bool) fields.
- `ModelControllerSnapshotter`: Simple snapshotter returning `{"current_model": str, "error": bool}`.
- `MODEL_KEY`: The thing key `["clydra_model"]`.

### `clydra/permission_dialog.py`
- `PermissionRequest`: Dataclass for pending permission request with `request_id`, `tool_name`, `tool_input`, `suggestions`.
- `QuestionOption`: Dataclass for a question option with `label` and `description`.
- `Question`: Dataclass for a question with `question`, `header`, `options`, `multi_select`.
- `QuestionRequest`: Dataclass for pending question request with `request_id` and `questions` list.
- `PermissionDialogController`: Dataclass with `pending_request: PermissionRequest | QuestionRequest | None`. The `can_use_tool` callback blocks on this thing's `receive()`.
- `PermissionDialogControllerSnapshotter`: Snapshotter returning `{"pending_request": {...} | null}` with `type` discriminator ("permission" or "question").
- `PERMISSION_DIALOG_KEY`: The thing key `["clydra_permission_dialog"]`.

### `clydra/file_list.py`
- `FileListController`: Dataclass with `cwd` (string) and `files` (list of relative paths).
- `scan_files()`: Shells out to `fd --type f` to list files. Automatically respects gitignore.
- `FileListControllerSnapshotter`: Rescans directory on each snapshot, returns `{"cwd": str, "files": list, "count": int}`.
- `FILE_LIST_KEY`: The thing key `["clydra_file_list"]`.

### `clydra/main.py`
- `load_transcript_history()`: Parses JSONL transcript for resume history.
- `parse_transcript_entry()`: Converts transcript entries to our event format.
- `SessionRestart`: Dataclass used to signal session restart with optional session ID.
- `create_can_use_tool_callback()`: Creates the `can_use_tool` callback that blocks on permission dialog's `receive()`. Handles both regular tool permission requests and `AskUserQuestion` tool calls. Supports `action: "abort"` for interrupt with denial.
- `create_client_options()`: Creates `ClaudeAgentOptions` for new or resumed sessions. Enables `include_partial_messages` for streaming and passes `can_use_tool` callback.
- `run_session()`: Inner loop that handles messages and returns `SessionRestart` on `/clear` or `/resume`. Manages `interrupt_ctrl.enabled` state. Also handles init `SystemMessage` to update permission mode and model controllers. Pops trailing `StreamEvent`s when a non-stream event arrives (optimization to keep events list shorter).
- `interrupt_worker()`: Background task that handles interrupt triggers via `receive()`.
- `permission_mode_worker()`: Background task that handles permission mode changes via `receive()`.
- `model_worker()`: Background task that handles model changes via `receive()`. On failure, sets error state and pushes `ClydraInternalError` event.
- `worker()`: Outer loop that manages client lifecycle via `async with`. Creates task group for background workers. Registers all snapshotters, anchor capturers, and renderers.
- `async_main()`: Sets up watcher and signal handling.
- `run()`: Sync entry point for console script.

### `clydra/renderers/ClydraRoot.tsx`
TUI-like React component:
- Dark theme (#0a0a0a background, monospace font)
- Status bar showing state and session ID (clickable to copy transcript path)
- **Collapsible messages**: User and assistant messages are collapsible `<details open>` with single-line summaries showing label + preview
- **Tool Result label**: Messages containing only tool results show "Tool Result" instead of "You"
- **JSON tree view**: Uses react18-json-view for interactive expandable JSON displays (tool inputs, results, raw events)
- **Streaming support**: `aggregateStreamEvents()` processes `StreamEvent` messages, accumulating text/thinking/tool_use deltas into displayable blocks. Supports `input_json_delta` for streaming tool inputs. `StreamingContentRenderer` shows live-updating content with a pulsing indicator.
- **Rich text rendering**: Assistant text blocks and user messages use Streamdown for markdown rendering with view mode toggle (rich/raw). The sanitize schema is extended to allow `<tw-k>` elements, which are rendered as `InlineNodeRef` components via a custom component. Streaming text uses `isAnimating` prop for live updates.
- **Syntax highlighting**: Read/Write/Edit tool calls have specialized renderers with shiki-based syntax highlighting via react-shiki:
  - `WriteContentView`: Shows file content with syntax highlighting based on file extension
  - `EditDiffView`: Two-column diff view showing old/new strings side by side
  - `ReadResultView`: Parses SDK line-numbered format, shows syntax-highlighted code
  - All have view mode toggle (rich/raw) to switch between highlighted and raw views
- **EventsContext**: React context passes events list to child components for tool result lookups
- **CodeMirror input**: Uses CodeMirror 6 with markdown mode, dark theme, no line numbers:
  - `@`-mentions for file autocompletion (data from FileListController via `onFilesChange` callback)
  - `/`-mentions for slash command autocompletion (extracted from init SystemMessage's `slash_commands`)
  - `<tw-k>JSON</tw-k>` syntax: ViewPlugin scans for this pattern, parses JSON, renders `InlineNodeRef` via React portal (for live editing)
  - **Drag and drop**: Things can be dragged from other panels and dropped into the input. Thingwatcher sets plain text to `<tw-k>JSON</tw-k>` syntax, which CodeMirror inserts natively. The ViewPlugin then renders it as an InlineNodeRef widget. Uses `ThingDropRegion` to suppress dockview's drop handling. Shows green highlight during valid drag. Disabled when input is not active.
  - Enter sends message, Shift+Enter inserts newline
- Auto-scroll on new messages and streaming updates
- Uses `ChildNodeView` to embed child things: permission dialog (above input), file list indicator (hidden, for file data), permission mode + model picker + interrupt button (below input)
- Handles `ClydraInternalError` events with red error display

### `clydra/renderers/FileListIndicator.tsx`
File list indicator renderer:
- Uses `onFilesChange` callback arg to pass file list to parent component
- Renders small badge showing file count
- Used by ClydraRoot to provide file data for @-mention autocompletion

### `clydra/renderers/InterruptButton.tsx`
Simple button renderer:
- Red background when enabled, grayed out when disabled
- Triggers `{ interrupt: true }` when clicked
- Enabled only during streaming state

### `clydra/renderers/PermissionMode.tsx`
Permission mode dropdown:
- Dropdown with options: Ask, Accept Edits, Plan
- Shift+Tab keyboard shortcut cycles through modes
- Triggers `{ mode: "..." }` when changed

### `clydra/renderers/ModelPicker.tsx`
Model picker text input:
- Text input with placeholder showing current model
- Orange text when user has unsaved input, black for placeholder
- Red border when error state is set (last set_model failed)
- Triggers `{ model: "..." }` on Enter key submit
- Empty submissions are ignored (no backend trigger)

### `clydra/renderers/formatting.tsx`
Shared syntax highlighting utilities used by ClydraRoot.tsx and PermissionDialog.tsx:
- `getLanguageFromPath()`: Map file extensions to language identifiers for shiki
- `ViewModePicker`: Toggle buttons to switch between rich/raw view modes
- `SyntaxHighlightedCode`: Syntax highlighted code block using react-shiki
- `DiffHighlightedCode`: Syntax highlighted code with diff notation support (strips +/-/space prefixes, colorizes lines)
- `WriteContentView`: Rich view for Write tool calls with syntax highlighting
- `EditDiffView`: Three-mode diff view (unified/split/raw) for Edit tool calls
- `computeDiff()`, `formatUnifiedDiff()`, `formatSplitLeft()`, `formatSplitRight()`: Diff computation and formatting utilities
- `transformerDiffNotation()`: Shiki transformer for diff line highlighting (based on usagi.io article)

### `clydra/renderers/diff.module.css`
CSS module with scoped styles for diff highlighting (`.add` green background, `.remove` red background).

### `clydra/renderers/PermissionDialog.tsx`
Dialog for tool permissions and Claude's clarifying questions (AskUserQuestion):
- **Permission mode**: Shows tool name, input JSON (or rich syntax-highlighted view for Write/Edit tools), SDK suggestions. Keyboard: up/down navigate, Enter select, Esc abort.
- **Rich views for Write/Edit**: Uses shared `WriteContentView` and `EditDiffView` components with view mode toggle
- **Question mode**: Tab-based UI for 1-4 questions. Keyboard: left/right switch tabs, up/down navigate options, Enter/Space select, Esc abort.
  - Each question shows predefined options plus "Other" with multiline textarea for free text
  - "Other" is mutually exclusive with other selections (even in multi-select questions)
  - Submit tab shows summary with warnings for unanswered questions
  - Uses `usePersistentThingState` to preserve state across re-mounts
  - `request_id` comparison resets state when a new request arrives
- **Abort button**: Both modes have an "Abort" button that sends `{ action: "abort" }` → `PermissionResultDeny(interrupt=True)`
- Only renders when `pending_request` is set

## Usage

```bash
# Install dependencies
cd extras/clydra
uv sync

# Run clydra
uv run clydra

# Or with custom port
uv run clydra --port 8770
```

The UI is available at:
- Full UI: `http://localhost:8770/`
- Embed view: `http://localhost:8770/embed?key=<base64-encoded-key>`

## Commands

In the input box:
- `/clear` - Start a new session
- `/resume <session-id>` - Resume a previous session

## Data Flow

### Main message loop
1. User types message in frontend, triggers with `{text: "message"}`
2. Backend receives trigger, sets `state="streaming"` and `interrupt_ctrl.enabled=True`, calls `client.query(text)`
3. For each SDK message, converts to dict, appends to events, calls `streaming_update()`
4. After `ResultMessage`, captures `session_id`, sets `state="waiting_input"` and `interrupt_ctrl.enabled=False`, calls `tick()`
5. Frontend flattens events from all chunks, reads state from last chunk

### Interrupt
1. User clicks interrupt button in frontend, triggers `{ interrupt: true }` to interrupt thing
2. `interrupt_worker()` receives trigger, validates payload, calls `client.interrupt()` if enabled
3. SDK handles the interrupt, stops current response

### Permission mode
1. User changes dropdown or presses Shift+Tab, triggers `{ mode: "..." }` to permission mode thing
2. `permission_mode_worker()` receives trigger, calls `client.set_permission_mode()`, updates state on success

### Model change
1. User types model name and presses Enter, triggers `{ model: "..." }` to model thing
2. `model_worker()` receives trigger, calls `client.set_model()`
3. On success: updates `current_model`, clears `error` state
4. On failure: sets `error=true` (red border), pushes `ClydraInternalError` event to conversation

### Permission dialog
1. SDK calls `can_use_tool` callback when a tool requires permission
2. Callback sets `pending_request` (PermissionRequest) on dialog controller, calls `streaming_update()` to show dialog
3. Callback blocks on `watcher.receive(dialog_ctrl)` waiting for user response
4. User selects option in frontend, triggers `{ action: "allow" | "deny" | "abort", ... }` to dialog thing
5. Callback receives response, clears `pending_request`, returns result to SDK
6. If `action: "abort"`, returns `PermissionResultDeny(interrupt=True)` to stop Claude

### AskUserQuestion (clarifying questions)
1. Claude calls `AskUserQuestion` tool when it needs user input
2. SDK triggers `can_use_tool` callback with `tool_name == "AskUserQuestion"`
3. Callback parses questions from input, sets `pending_request` (QuestionRequest) on dialog controller
4. Frontend renders tab-based question UI with `usePersistentThingState` for answer tracking
5. User selects options (or types "Other" free text) for each question, navigates to Submit tab
6. User submits, frontend triggers `{ action: "allow", answers: {...} }` with question→answer mapping
7. Callback returns `PermissionResultAllow(updated_input={questions: [...], answers: {...}})`
8. Claude receives the answers and continues its task

## Dependencies

- `thingwatcher` (local, editable)
- `claude-agent-sdk`
- `anyio`
- `cbor2`

## Building Renderers

The renderers are pre-bundled with esbuild before being loaded by thingwatcher. This ensures consistent CodeMirror package versions.

```bash
cd clydra/renderers

# Install dependencies (first time)
npm install

# Build once
npm run build

# Watch mode for development
npm run watch
```

The build outputs `dist/main.js` (bundled ESM) and `dist/main.css` (extracted CSS). These are loaded by `main.py` via `register_renderer()`.

**External packages** (not bundled, provided by runtime):
- `react`, `react-dom`, `thingwatcher-bindings`
- `https://esm.sh/*` URLs (react-shiki stays as CDN import)

## Testing

```bash
# Run pytest (when tests exist)
uv run pytest
```
