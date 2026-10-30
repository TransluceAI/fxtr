# Phase 0 Step 2: Extra IO - Interrupt, Permission Mode, Permission Dialogs

## Goal

Add three interactive features to clydra:
1. **Interrupt button** - A separate thingwatcher "thing" that allows interrupting Claude at any time during streaming
2. **Permission mode selector** - A separate thing with dropdown + shift-tab keyboard shortcut to cycle modes
3. **Permission dialogs** - A separate thing that blocks on `receive()` for approving/denying tool use requests

## Architecture Overview

### Key Insight: Three Separate Things

Each feature is its own thingwatcher "thing" with its own `watcher.receive()` loop running in a separate anyio task. This allows:
- Interrupt: can fire while main loop is blocked on streaming
- Permission mode: can change while main loop is blocked on streaming
- Permission dialog: `can_use_tool` callback blocks on the dialog thing's `receive()`, no need for events/callbacks

### New Components

```
clydra/
├── clydra/
│   ├── manager.py           # Add permission_mode field
│   ├── interrupt.py         # NEW: InterruptController + snapshotter
│   ├── permission_mode.py   # NEW: PermissionModeController + snapshotter
│   ├── permission_dialog.py # NEW: PermissionDialogController + snapshotter
│   ├── main.py              # Wire up all three tasks
│   └── renderers/
│       ├── ClydraRoot.tsx       # Render ChildNodeViews for all three
│       ├── InterruptButton.tsx  # NEW: Interrupt button renderer
│       ├── PermissionMode.tsx   # NEW: Permission mode dropdown renderer
│       └── PermissionDialog.tsx # NEW: Permission dialog renderer
```

### Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         ClydraRoot (main thing)                          │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │ Conversation area (events stream)                                │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │ <ChildNodeView> PermissionDialog (shown when pending)            │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │ Input box + Send btn                                             │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌─────────────────────────────┐  ┌────────────────────────────────┐    │
│  │ <ChildNodeView> PermMode    │  │ <ChildNodeView> InterruptBtn   │    │
│  └─────────────────────────────┘  └────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

## Feature 1: Interrupt Button

### Design

The interrupt button is a separate "thing" that:
- Has its own `watcher.receive()` loop in a separate anyio task
- When triggered, calls `client.interrupt()` on the active SDK client
- Is rendered as a `ChildNodeView` below the input area in `ClydraRoot.tsx`

### InterruptController (`interrupt.py`)

```python
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from thingwatcher import ThingRef, ThingState
from thingwatcher.api import AnchorCapturer, StatefulOwningSnapshotter

if TYPE_CHECKING:
    from clydra.manager import ClydraManager

INTERRUPT_KEY = ["clydra_interrupt"]


@dataclass
class InterruptController:
    """Controller for interrupt functionality.

    This is a separate tracked thing that handles interrupt triggers
    in its own task, independent of the main message loop.
    """

    enabled: bool = False  # True when streaming is active
    _manager: "ClydraManager | None" = field(default=None, repr=False)


class InterruptControllerSnapshotter(StatefulOwningSnapshotter):
    """Simple snapshotter - always returns full state."""

    def snapshot_owned(self, controller: InterruptController) -> ThingState:
        return self.stateless_snapshot(controller)

    def stateless_snapshot(self, controller: InterruptController) -> ThingState:
        return ThingState(
            key=INTERRUPT_KEY,
            thing_type="InterruptButton",
            thing_data={"enabled": controller.enabled},
            links=[],
        )


class InterruptControllerAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: InterruptController) -> ThingRef:
        return ThingRef(key=INTERRUPT_KEY)
```

### Interrupt Task (`main.py`)

```python
async def interrupt_worker(
    watcher: Watcher,
    interrupt_ctrl: InterruptController,
) -> None:
    """Background task that handles interrupt triggers."""
    while True:
        payload = await watcher.receive(interrupt_ctrl)

        # Validate payload to avoid accidental triggers
        if payload.get("interrupt") is not True:
            print(f"Interrupt: ignoring invalid payload {payload}")
            continue

        manager = interrupt_ctrl._manager
        if manager and manager._client and interrupt_ctrl.enabled:
            try:
                await manager._client.interrupt()
            except Exception as e:
                print(f"Interrupt failed: {e}")
```

### Integration with Main Loop

The main `run_session()` function updates `interrupt_ctrl.enabled`:
- Set `True` when entering streaming state
- Set `False` when exiting streaming state
- Call `watcher.streaming_update(interrupt_ctrl)` after each change

### Task Spawning in worker()

The `worker()` function creates its own task group to spawn background tasks:

```python
async def worker(watcher: Watcher, manager: ClydraManager, port: int) -> None:
    # ... register snapshotters, renderers, track things ...

    # Create controllers
    interrupt_ctrl = InterruptController(_manager=manager)
    perm_mode_ctrl = PermissionModeController(_manager=manager)
    dialog_ctrl = PermissionDialogController()

    # Track all controllers
    watcher.track(interrupt_ctrl)
    watcher.track(perm_mode_ctrl)
    watcher.track(dialog_ctrl)

    async with anyio.create_task_group() as tg:
        # Spawn background workers
        tg.start_soon(interrupt_worker, watcher, interrupt_ctrl)
        tg.start_soon(permission_mode_worker, watcher, perm_mode_ctrl)

        # Main session loop (outer loop manages client lifecycle)
        session_id: str | None = None
        while True:
            # Reset permission mode to default for new session
            perm_mode_ctrl.current_mode = "default"
            watcher.streaming_update(perm_mode_ctrl)

            options = create_client_options(manager, dialog_ctrl, watcher, session_id)
            async with ClaudeSDKClient(options=options) as client:
                # Sync permission mode with SDK
                await client.set_permission_mode("default")

                result = await run_session(watcher, manager, client, interrupt_ctrl)
                if isinstance(result, SessionRestart):
                    session_id = result.session_id
                else:
                    session_id = None
```

### InterruptButton Renderer (`InterruptButton.tsx`)

```tsx
export function FullRenderer({ dataChunks, triggerable, trigger }: FullRendererProps) {
  const lastChunk = dataChunks[dataChunks.length - 1];
  const enabled = lastChunk?.enabled ?? false;

  const handleClick = async () => {
    if (triggerable && enabled) {
      await trigger({ interrupt: true });
    }
  };

  return (
    <button
      onClick={handleClick}
      disabled={!triggerable || !enabled}
      style={{
        width: '100%',
        background: enabled ? '#a33' : '#222',
        color: '#e0e0e0',
        border: '1px solid #444',
        padding: '6px 12px',
        fontFamily: 'monospace',
        fontSize: '12px',
        cursor: enabled && triggerable ? 'pointer' : 'default',
        opacity: enabled ? 1 : 0.4,
      }}
    >
      ⏹ Interrupt
    </button>
  );
}
```

## Feature 2: Permission Mode Selector

### Design

Permission mode selector is a separate "thing" that:
- Has its own `watcher.receive()` loop in a separate anyio task
- When triggered, calls `client.set_permission_mode()` on the active SDK client
- Supports shift+tab keyboard shortcut to cycle modes
- Is rendered as a `ChildNodeView` next to the input area

Available modes (from SDK):
- `"default"` - Ask for permission on dangerous tools
- `"acceptEdits"` - Auto-accept file edits
- `"plan"` - Plan mode

We explicitly exclude `"bypassPermissions"` from the UI for safety.

### PermissionModeController (`permission_mode.py`)

```python
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from thingwatcher import ThingRef, ThingState
from thingwatcher.api import AnchorCapturer, StatefulOwningSnapshotter

if TYPE_CHECKING:
    from clydra.manager import ClydraManager

PERMISSION_MODE_KEY = ["clydra_permission_mode"]

PermissionMode = Literal["default", "acceptEdits", "plan"]
PERMISSION_MODES: list[PermissionMode] = ["default", "acceptEdits", "plan"]


@dataclass
class PermissionModeController:
    """Controller for permission mode selection.

    This is a separate tracked thing that handles permission mode changes
    in its own task, independent of the main message loop.
    """

    current_mode: PermissionMode = "default"
    _manager: "ClydraManager | None" = field(default=None, repr=False)


class PermissionModeControllerSnapshotter(StatefulOwningSnapshotter):
    def snapshot_owned(self, controller: PermissionModeController) -> ThingState:
        return self.stateless_snapshot(controller)

    def stateless_snapshot(self, controller: PermissionModeController) -> ThingState:
        return ThingState(
            key=PERMISSION_MODE_KEY,
            thing_type="PermissionMode",
            thing_data={"current_mode": controller.current_mode},
            links=[],
        )


class PermissionModeControllerAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: PermissionModeController) -> ThingRef:
        return ThingRef(key=PERMISSION_MODE_KEY)
```

### Permission Mode Task (`main.py`)

```python
async def permission_mode_worker(
    watcher: Watcher,
    perm_mode_ctrl: PermissionModeController,
) -> None:
    """Background task that handles permission mode changes."""
    while True:
        payload = await watcher.receive(perm_mode_ctrl)

        new_mode = payload.get("mode")
        if new_mode not in PERMISSION_MODES:
            print(f"Permission mode: ignoring invalid mode {new_mode}")
            continue

        manager = perm_mode_ctrl._manager
        if manager and manager._client:
            try:
                await manager._client.set_permission_mode(new_mode)
                # Only update state if SDK call succeeded
                perm_mode_ctrl.current_mode = new_mode
                watcher.streaming_update(perm_mode_ctrl)
            except Exception as e:
                print(f"Set permission mode failed: {e}")
                # Don't update state on failure
```

### PermissionMode Renderer (`PermissionMode.tsx`)

```tsx
import { useState, useEffect } from "react";
import type { FullRendererProps } from "thingwatcher-bindings";

const MODES = [
  { value: "default", label: "Ask" },
  { value: "acceptEdits", label: "Accept Edits" },
  { value: "plan", label: "Plan" },
] as const;

export function FullRenderer({ dataChunks, triggerable, trigger }: FullRendererProps) {
  const lastChunk = dataChunks[dataChunks.length - 1];
  const currentMode = lastChunk?.current_mode ?? "default";

  const handleChange = async (newMode: string) => {
    if (triggerable) {
      await trigger({ mode: newMode });
    }
  };

  // Shift+Tab cycles through modes
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.shiftKey && e.key === "Tab") {
        e.preventDefault();
        const currentIndex = MODES.findIndex((m) => m.value === currentMode);
        const nextIndex = (currentIndex + 1) % MODES.length;
        handleChange(MODES[nextIndex].value);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [currentMode, triggerable]);

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{ color: "#666", fontSize: "11px" }}>Mode:</span>
      <select
        value={currentMode}
        onChange={(e) => handleChange(e.target.value)}
        disabled={!triggerable}
        style={{
          background: "#1a1a1a",
          color: "#e0e0e0",
          border: "1px solid #333",
          padding: "2px 6px",
          fontFamily: "monospace",
          fontSize: "11px",
          cursor: triggerable ? "pointer" : "default",
        }}
      >
        {MODES.map(({ value, label }) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
      <span style={{ color: "#444", fontSize: "10px" }}>(Shift+Tab)</span>
    </div>
  );
}

// Minimal ref renderers
export function RefRenderer({ dataChunks }: { dataChunks: unknown[] }) {
  const lastChunk = dataChunks[dataChunks.length - 1] as { current_mode?: string } | undefined;
  return <span style={{ color: "#888" }}>Mode: {lastChunk?.current_mode ?? "?"}</span>;
}

export function InlineRefRenderer({ dataChunks }: { dataChunks: unknown[] }) {
  return RefRenderer({ dataChunks });
}

export function PreviewRenderer({ dataChunks }: { dataChunks: unknown[] }) {
  return RefRenderer({ dataChunks });
}
```

## Feature 3: Permission Dialogs

### Design

When Claude wants to use a tool requiring permission, the SDK calls the `can_use_tool` callback. Instead of using events, we:
1. Store the pending permission request in `PermissionDialogController`
2. Have `can_use_tool` callback block on `watcher.receive(dialog_ctrl)`
3. When user responds, the receive unblocks and returns the response

The dialog appears inline above the input area when a permission is pending.

### PermissionDialogController (`permission_dialog.py`)

```python
from dataclasses import dataclass, field
from typing import Any

from thingwatcher import ThingRef, ThingState
from thingwatcher.api import AnchorCapturer, StatefulOwningSnapshotter

PERMISSION_DIALOG_KEY = ["clydra_permission_dialog"]


@dataclass
class PermissionRequest:
    """A pending permission request."""

    tool_name: str
    tool_input: dict[str, Any]
    suggestions: list[dict[str, Any]]


@dataclass
class PermissionDialogController:
    """Controller for permission dialogs.

    This thing's receive() is used to block until user responds to a permission request.
    The can_use_tool callback sets pending_request, updates the thing, then awaits receive().
    """

    pending_request: PermissionRequest | None = None


class PermissionDialogControllerSnapshotter(StatefulOwningSnapshotter):
    def snapshot_owned(self, controller: PermissionDialogController) -> ThingState:
        return self.stateless_snapshot(controller)

    def stateless_snapshot(self, controller: PermissionDialogController) -> ThingState:
        return ThingState(
            key=PERMISSION_DIALOG_KEY,
            thing_type="PermissionDialog",
            thing_data={
                "pending_request": (
                    {
                        "tool_name": controller.pending_request.tool_name,
                        "tool_input": controller.pending_request.tool_input,
                        "suggestions": controller.pending_request.suggestions,
                    }
                    if controller.pending_request
                    else None
                ),
            },
            links=[],
        )


class PermissionDialogControllerAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: PermissionDialogController) -> ThingRef:
        return ThingRef(key=PERMISSION_DIALOG_KEY)
```

### can_use_tool Callback Implementation

The callback is created in `main.py` and blocks on the dialog thing's `receive()`:

```python
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

def create_can_use_tool_callback(
    watcher: Watcher,
    dialog_ctrl: PermissionDialogController,
):
    """Create a can_use_tool callback that blocks on dialog thing's receive()."""

    async def can_use_tool(
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        # Set pending request
        dialog_ctrl.pending_request = PermissionRequest(
            tool_name=tool_name,
            tool_input=input_data,
            suggestions=[
                s.to_dict() if hasattr(s, "to_dict") else s
                for s in context.suggestions
            ],
        )

        # Notify frontend that dialog should appear
        watcher.streaming_update(dialog_ctrl)

        # Block until user responds via the dialog thing's receive()
        payload = await watcher.receive(dialog_ctrl)

        # Clear pending request
        dialog_ctrl.pending_request = None
        watcher.streaming_update(dialog_ctrl)

        # Process response
        action = payload.get("action", "deny")
        if action == "allow":
            return PermissionResultAllow(
                updated_input=payload.get("updated_input"),
                updated_permissions=payload.get("updated_permissions"),
            )
        else:
            return PermissionResultDeny(
                message=payload.get("message", "User denied"),
                interrupt=payload.get("interrupt", False),
            )

    return can_use_tool
```

### Wiring into ClaudeAgentOptions

```python
def create_client_options(
    manager: ClydraManager,
    dialog_ctrl: PermissionDialogController,
    watcher: Watcher,
    session_id: str | None = None,
) -> ClaudeAgentOptions:
    # ... existing hook setup ...

    can_use_tool = create_can_use_tool_callback(watcher, dialog_ctrl)

    return ClaudeAgentOptions(
        # ... existing options ...
        can_use_tool=can_use_tool,
    )
```

### PermissionDialog Renderer (`PermissionDialog.tsx`)

```tsx
import { useState, useEffect } from "react";
import type { FullRendererProps } from "thingwatcher-bindings";

interface PermissionSuggestion {
  type: string;
  behavior?: string;
  mode?: string;
  rules?: Array<{ tool_name: string; rule_content?: string }>;
}

interface PendingRequest {
  tool_name: string;
  tool_input: Record<string, unknown>;
  suggestions: PermissionSuggestion[];
}

const styles = {
  container: {
    background: "#1a1a2a",
    border: "1px solid #4a4a6a",
    padding: "12px",
    fontFamily: "monospace",
    fontSize: "12px",
  },
  header: {
    color: "#e0e0e0",
    marginBottom: "8px",
    fontWeight: "bold" as const,
  },
  toolName: {
    color: "#8be9fd",
  },
  inputSection: {
    marginBottom: "12px",
  },
  summary: {
    cursor: "pointer",
    color: "#888",
    fontSize: "11px",
  },
  codeBlock: {
    background: "#111",
    border: "1px solid #333",
    padding: "8px",
    overflow: "auto",
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word" as const,
    fontSize: "11px",
    marginTop: "4px",
    maxHeight: "200px",
  },
  optionsContainer: {
    display: "flex",
    flexDirection: "column" as const,
    gap: "4px",
    marginBottom: "8px",
  },
  option: {
    padding: "6px 10px",
    border: "1px solid #333",
    cursor: "pointer",
    transition: "background 0.1s",
  },
  optionFocused: {
    background: "#2a4a6a",
    borderColor: "#4a90d9",
  },
  hint: {
    color: "#666",
    fontSize: "10px",
  },
};

function formatSuggestion(suggestion: PermissionSuggestion): string {
  switch (suggestion.type) {
    case "addRules":
      const rule = suggestion.rules?.[0];
      return `Add rule: ${suggestion.behavior} ${rule?.tool_name || "tool"}`;
    case "setMode":
      return `Set mode: ${suggestion.mode}`;
    default:
      return `Apply: ${suggestion.type}`;
  }
}

export function FullRenderer({ dataChunks, triggerable, trigger }: FullRendererProps) {
  const lastChunk = dataChunks[dataChunks.length - 1] as { pending_request?: PendingRequest } | undefined;
  const pendingRequest = lastChunk?.pending_request;

  const [focusedIndex, setFocusedIndex] = useState(0);

  // Build options list
  const options = pendingRequest
    ? [
        { type: "allow", label: "Allow" },
        { type: "deny", label: "Deny" },
        ...pendingRequest.suggestions.map((s) => ({
          type: "suggestion",
          label: formatSuggestion(s),
          suggestion: s,
        })),
      ]
    : [];

  // Reset focus when options change
  useEffect(() => {
    setFocusedIndex(0);
  }, [pendingRequest?.tool_name]);

  // Keyboard navigation
  useEffect(() => {
    if (!pendingRequest || !triggerable) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      switch (e.key) {
        case "ArrowUp":
          e.preventDefault();
          setFocusedIndex((i) => Math.max(0, i - 1));
          break;
        case "ArrowDown":
          e.preventDefault();
          setFocusedIndex((i) => Math.min(options.length - 1, i + 1));
          break;
        case "Enter":
          e.preventDefault();
          selectOption(options[focusedIndex]);
          break;
        case "Escape":
          e.preventDefault();
          trigger({ action: "deny", message: "User cancelled" });
          break;
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [pendingRequest, focusedIndex, options, triggerable]);

  const selectOption = async (option: (typeof options)[number]) => {
    if (!triggerable) return;

    if (option.type === "allow") {
      await trigger({ action: "allow" });
    } else if (option.type === "deny") {
      await trigger({ action: "deny" });
    } else if (option.type === "suggestion" && "suggestion" in option) {
      await trigger({ action: "allow", updated_permissions: [option.suggestion] });
    }
  };

  // Don't render anything if no pending request
  if (!pendingRequest) {
    return null;
  }

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        Permission Request:{" "}
        <span style={styles.toolName}>{pendingRequest.tool_name}</span>
      </div>

      <details open style={styles.inputSection}>
        <summary style={styles.summary}>Input</summary>
        <pre style={styles.codeBlock}>
          {JSON.stringify(pendingRequest.tool_input, null, 2)}
        </pre>
      </details>

      <div style={styles.optionsContainer}>
        {options.map((option, i) => (
          <div
            key={i}
            style={{
              ...styles.option,
              ...(i === focusedIndex ? styles.optionFocused : {}),
            }}
            onClick={() => selectOption(option)}
            onMouseEnter={() => setFocusedIndex(i)}
          >
            {option.label}
          </div>
        ))}
      </div>

      <div style={styles.hint}>↑↓ navigate, Enter select, Esc deny</div>
    </div>
  );
}

// Minimal ref renderers (dialog is not typically shown as a ref)
export function RefRenderer() {
  return <span style={{ color: "#888" }}>Permission Dialog</span>;
}

export function InlineRefRenderer() {
  return RefRenderer();
}

export function PreviewRenderer() {
  return RefRenderer();
}
```

## Snapshotter Updates

The `ClydraManagerSnapshotter` links to all three child things using `ThingLink`:

```python
from clydra.interrupt import INTERRUPT_KEY
from clydra.permission_mode import PERMISSION_MODE_KEY
from clydra.permission_dialog import PERMISSION_DIALOG_KEY
from thingwatcher import ThingLink

CHILD_LINKS = [
    ThingLink(link_type="interrupt", link_key="interrupt", to_thing_key=INTERRUPT_KEY),
    ThingLink(link_type="permission_mode", link_key="permission_mode", to_thing_key=PERMISSION_MODE_KEY),
    ThingLink(link_type="permission_dialog", link_key="permission_dialog", to_thing_key=PERMISSION_DIALOG_KEY),
]

def stateless_snapshot(self, manager: ClydraManager) -> ThingState:
    return ThingState(
        key=CLYDRA_ROOT_KEY,
        thing_type="ClydraRoot",
        thing_data={
            "session_id": manager.session_id,
            "events": manager.events,
            "state": manager.state,
        },
        links=CHILD_LINKS,
    )
```

## Rendering in ClydraRoot.tsx

Update the main renderer to include all three child things:

```tsx
import { ChildNodeView } from "thingwatcher-bindings";

// In FullRenderer:
return (
  <div style={styles.container}>
    {/* Status bar */}
    <div style={styles.statusBar}>
      {/* ... existing status info ... */}
    </div>

    {/* Conversation area */}
    <div ref={conversationRef} style={styles.conversationArea}>
      {/* ... existing event rendering ... */}
    </div>

    {/* Permission dialog (above input, shown when pending) */}
    <ChildNodeView thingKey={["clydra_permission_dialog"]} />

    {/* Input area */}
    <div style={styles.inputArea}>
      <input ... />
      <button>Send</button>
    </div>

    {/* Controls bar: permission mode + interrupt button side by side */}
    <div style={{
      display: "flex",
      gap: "8px",
      padding: "8px",
      borderTop: "1px solid #333",
      alignItems: "center",
    }}>
      <ChildNodeView thingKey={["clydra_permission_mode"]} />
      <div style={{ flex: 1 }}>
        <ChildNodeView thingKey={["clydra_interrupt"]} />
      </div>
    </div>
  </div>
);
```

## Implementation Steps

### Backend
- [x] Create `interrupt.py` with `InterruptController`, snapshotter, anchor capturer
- [x] Create `permission_mode.py` with `PermissionModeController`, snapshotter, anchor capturer
- [x] Create `permission_dialog.py` with `PermissionDialogController`, `PermissionRequest`, snapshotter, anchor capturer
- [x] Update `ClydraManagerSnapshotter` to link to all three child things
- [x] Add `interrupt_worker` task to `main.py`
- [x] Add `permission_mode_worker` task to `main.py`
- [x] Implement `create_can_use_tool_callback` in `main.py`
- [x] Update `create_client_options` to accept and use `can_use_tool` callback
- [x] Register all three new thing types in `worker()`
- [x] Spawn interrupt and permission mode background tasks in task group
- [x] Wire `interrupt_ctrl._manager` and `perm_mode_ctrl._manager`
- [x] Set `interrupt_ctrl.enabled` based on streaming state in `run_session()`
- [x] Reset `perm_mode_ctrl.current_mode` to `"default"` and call `client.set_permission_mode("default")` at session start

### Frontend
- [x] Create `InterruptButton.tsx` renderer with all exports
- [x] Create `PermissionMode.tsx` renderer with Shift+Tab cycling
- [x] Create `PermissionDialog.tsx` renderer with keyboard navigation
- [x] Register all three renderers in `main.py` via `register_renderer()`
- [x] Update `ClydraRoot.tsx` to include three `ChildNodeView` components
- [x] Update layout: permission dialog above input, permission mode + interrupt side by side below input

### Testing
- [ ] Manual test: interrupt button appears disabled when not streaming
- [ ] Manual test: interrupt button becomes active during streaming
- [ ] Manual test: clicking interrupt stops Claude mid-response
- [ ] Manual test: permission mode dropdown changes mode
- [ ] Manual test: Shift+Tab cycles through modes
- [ ] Manual test: permission dialog appears for tool requiring permission
- [ ] Manual test: arrow keys navigate dialog options
- [ ] Manual test: Enter selects option, Esc denies
- [ ] Manual test: suggestions from SDK appear as options

### Documentation
- [x] Update CODE_OVERVIEW.md with new files and concepts

## Trigger Payload Types

For documentation, here are the trigger payloads each thing accepts:

```typescript
// Main thing (ClydraRoot) - unchanged
{ text: string }

// Interrupt thing - must include interrupt: true for safety
{ interrupt: true }

// Permission mode thing
{ mode: "default" | "acceptEdits" | "plan" }

// Permission dialog thing
{
  action: "allow" | "deny";
  updated_input?: Record<string, unknown>;
  updated_permissions?: PermissionSuggestion[];
  message?: string;
  interrupt?: boolean;
}
```

## Open Questions

1. **Interrupt timing**: The SDK's `interrupt()` is async. If the user spams the interrupt button, should we debounce, or let multiple interrupts queue?
   - **Decision**: Single interrupt is sufficient; button should be disabled briefly after click.

2. **Permission dialog auto-focus**: Should the permission dialog steal keyboard focus automatically?
   - **Decision**: Yes, use a `useEffect` to add global keydown listener when dialog appears.

3. **Suggestion display**: Some SDK suggestions may be complex (multiple rules). How detailed should the display be?
   - **Decision**: Start with simple label formatting; can enhance later based on real usage.

4. **Error handling**: What if `set_permission_mode` fails (e.g., SDK error)?
   - **Decision**: Log to console, but don't break the UI.

5. **Session restart during pending permission**: What if `/clear` is issued while permission dialog is pending?
   - **Decision**: This shouldn't happen in practice since the main input isn't triggerable while we're blocked on the permission dialog's `receive()`. If it does happen somehow, let the SDK handle cancellation - it should just work.

6. **Permission mode initial sync**: How do we ensure the controller's mode matches the SDK's actual mode?
   - **Decision**: Explicitly set both to `"default"` when starting a new session. Call `client.set_permission_mode("default")` and set `perm_mode_ctrl.current_mode = "default"` at session start.
