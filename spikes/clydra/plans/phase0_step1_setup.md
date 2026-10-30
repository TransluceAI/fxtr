# Phase 0 Step 1: Initial Setup and MVP

## Goal

Create a working MVP that:
- Tracks a single root thing `["clydra_root"]` representing a ClydraManager
- Connects to Claude Code via the Agent SDK
- Allows sending messages and receiving streaming responses
- Supports `/clear` and `/resume {session-id}` commands
- Renders a TUI-like conversation view

## Architecture Overview

```
clydra/
├── pyproject.toml          # uv package config, deps on thingwatcher + claude-agent-sdk
├── clydra/
│   ├── __init__.py         # Re-exports public API
│   ├── main.py             # Entry point: watcher setup, signal handling, worker task
│   ├── manager.py          # ClydraManager class (the tracked thing)
│   ├── snapshotter.py      # StatefulOwningSnapshotter for manager
│   └── renderers/
│       ├── deno.json       # Deno config for typechecking (maps thingwatcher-bindings)
│       └── ClydraRoot.tsx  # Main conversation renderer
├── plans/                  # (already exists)
└── specs/                  # (already exists)
```

## Key Components

### 1. ClydraManager (`manager.py`)

The manager is the single tracked thing. It holds:

```python
@dataclass
class ClydraManager:
    session_id: str | None = None
    events: list[dict] = field(default_factory=list)  # Raw SDK messages as dicts
    state: Literal["setup", "streaming", "waiting_input"] = "setup"
    cwd: Path = field(default_factory=Path.cwd)

    # Runtime only (not serialized)
    _client: ClaudeSDKClient | None = field(default=None, repr=False)
```

**Event structure**: We preserve raw SDK message structure via `dataclasses.asdict(message)`. The SDK uses dataclasses (not Pydantic), so this is how we convert to dicts. Each event has a discriminator we can check:
- `UserMessage`: has `content` (str or list), optional `uuid`, `parent_tool_use_id`
- `AssistantMessage`: has `content` (list of blocks), `model`, optional `error`
- `SystemMessage`: has `subtype`, `data` dict
- `ResultMessage`: has `session_id`, `duration_ms`, `total_cost_usd`, `num_turns`, etc.

Content blocks within messages:
- `TextBlock`: `{"type": "text", "text": "..."}`
- `ToolUseBlock`: `{"type": "tool_use", "id": "...", "name": "...", "input": {...}}`
- `ToolResultBlock`: `{"type": "tool_result", "tool_use_id": "...", "content": "..."}`
- `ThinkingBlock`: `{"type": "thinking", "thinking": "...", "signature": "..."}`

**State transitions**:
- `setup` → `waiting_input`: After creating initial session
- `waiting_input` → `streaming`: User sends a message
- `streaming` → `waiting_input`: Assistant finishes response (ResultMessage received)

### 2. Snapshotter (`snapshotter.py`)

Uses `StatefulOwningSnapshotter` to support `streaming_update()`:

```python
class ClydraManagerSnapshotter(StatefulOwningSnapshotter):
    def __init__(self):
        self._last_event_count: dict[int, int] = {}

    def snapshot_owned(self, manager: ClydraManager) -> ThingState | ThingDelta:
        obj_id = id(manager)
        last_count = self._last_event_count.get(obj_id, 0)
        current_count = len(manager.events)

        if last_count == 0:
            # First snapshot or after resume: full state
            self._last_event_count[obj_id] = current_count
            return ThingState(
                key=["clydra_root"],
                thing_type="ClydraRoot",
                thing_data={
                    "session_id": manager.session_id,
                    "events": manager.events,
                    "state": manager.state,
                },
                links=[],
            )
        elif current_count > last_count:
            # New events: append delta with current state in each chunk
            new_events = manager.events[last_count:]
            self._last_event_count[obj_id] = current_count
            return ThingDelta(
                key=["clydra_root"],
                data_diff_kind="append",
                new_data={
                    "events": new_events,
                    "state": manager.state,  # Always include current state
                    "session_id": manager.session_id,
                },
                added_links=[],
                removed_link_keys=[],
            )
        else:
            # No new events - emit no_diff delta (state unchanged)
            return ThingDelta(
                key=["clydra_root"],
                data_diff_kind="no_diff",
                new_data=None,
                added_links=[],
                removed_link_keys=[],
            )
```

**Note on delta design**: Each delta chunk includes the current `state` and `session_id` alongside the new events. The frontend reads the current state from the last chunk, ignoring state from previous chunks. This avoids having to track state changes separately and makes the data model simpler - events accumulate, state is always "current as of this chunk".

### 3. Worker Task (`main.py`)

The main control flow:

```python
import dataclasses
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage

async def worker(watcher: Watcher, manager: ClydraManager):
    """Main worker loop: receive input, send to SDK, stream responses."""

    # Register and track
    register_snapshotter(ClydraManager, ClydraManagerSnapshotter())
    register_anchor_capturer(ClydraManager, ClydraManagerAnchorCapturer())
    register_renderer("ClydraRoot", load_renderer("ClydraRoot"))
    configure_rendering("ClydraRoot", "#4a90d9")

    watcher.track(manager)
    watcher.add_root("clydra", manager)

    # Create initial session
    await start_new_session(manager)
    manager.state = "waiting_input"
    watcher.tick("info", "Session started")

    # Print embed URL for convenience
    import base64
    key_b64 = base64.urlsafe_b64encode(cbor2.dumps(["clydra_root"])).decode()
    print(f"Embed view: http://localhost:{port}/embed?key={key_b64}")

    while True:
        # Wait for user input (blocks until triggered)
        payload = await watcher.receive(manager)
        text = payload.get("text", "").strip()

        # Handle special commands
        if text == "/clear":
            await start_new_session(manager)
            manager.state = "waiting_input"
            watcher.tick("info", "New session started")
            continue

        if text.startswith("/resume "):
            session_id = text.split(" ", 1)[1].strip()
            await resume_session(manager, session_id)
            manager.state = "waiting_input"
            watcher.tick("info", f"Resumed session {session_id}")
            continue

        # Send to Claude
        manager.state = "streaming"
        watcher.streaming_update(manager)

        await manager._client.query(text)
        async for message in manager._client.receive_response():
            event_dict = dataclasses.asdict(message)
            manager.events.append(event_dict)
            watcher.streaming_update(manager)

            # Capture session_id from ResultMessage
            if isinstance(message, ResultMessage):
                manager.session_id = message.session_id

        manager.state = "waiting_input"
        watcher.tick("info", "Turn complete")
```

### 4. Session Management

```python
async def start_new_session(manager: ClydraManager):
    """Close existing session if any, create a new one."""
    if manager._client:
        await manager._client.__aexit__(None, None, None)

    manager.session_id = None
    manager.events = []

    options = ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["user", "project", "local"],
        max_thinking_tokens=31999,
        cwd=manager.cwd,
    )
    manager._client = ClaudeSDKClient(options=options)
    await manager._client.__aenter__()
    # session_id is assigned by SDK after first message or available from client


async def resume_session(manager: ClydraManager, session_id: str):
    """Resume an existing session by ID."""
    if manager._client:
        await manager._client.__aexit__(None, None, None)

    manager.session_id = session_id
    manager.events = []  # Will be populated from transcript when hook fires
    manager._transcript_loaded = False  # Flag to track if we've loaded history

    # Hook to capture transcript_path and load history
    async def on_user_prompt(input_data, tool_use_id, context):
        if not manager._transcript_loaded:
            transcript_path = input_data.get("transcript_path")
            if transcript_path:
                try:
                    history = load_transcript_history(transcript_path)
                    # Prepend history to events
                    manager.events = history + manager.events
                    manager._transcript_loaded = True
                except Exception as e:
                    print(f"Warning: Could not load transcript: {e}")
        return {}  # No modifications to the prompt

    options = ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["user", "project", "local"],
        max_thinking_tokens=31999,
        cwd=manager.cwd,
        resume=session_id,
        hooks={
            "UserPromptSubmit": [HookMatcher(matcher=None, hooks=[on_user_prompt])]
        },
    )
    manager._client = ClaudeSDKClient(options=options)
    await manager._client.__aenter__()


def load_transcript_history(transcript_path: str) -> list[dict]:
    """Load and parse JSONL transcript into event dicts."""
    events = []
    with open(transcript_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                event = parse_transcript_entry(entry)
                if event:
                    events.append(event)
            except json.JSONDecodeError:
                continue
    return events


def parse_transcript_entry(entry: dict) -> dict | None:
    """Convert a transcript JSONL entry to our event format."""
    entry_type = entry.get("type")
    message = entry.get("message", {})

    if entry_type == "user" and message.get("role") == "user":
        # User message - convert to UserMessage-like structure
        return {
            "type": "user_message",
            "content": message.get("content"),
            "uuid": entry.get("uuid"),
            "timestamp": entry.get("timestamp"),
        }
    elif entry_type == "assistant" and message.get("role") == "assistant":
        # Assistant message - convert to AssistantMessage-like structure
        return {
            "type": "assistant_message",
            "content": message.get("content", []),
            "model": message.get("model"),
            "uuid": entry.get("uuid"),
            "timestamp": entry.get("timestamp"),
        }
    elif entry_type == "queue-operation":
        # Skip queue operations - not useful for display
        return None
    else:
        # Unknown entry type - wrap as-is
        return {
            "type": "unknown_history_event",
            "raw": entry,
        }
```

**Resume with history loading**: When resuming a session, we want to show past conversation history. The approach:

1. Register a `UserPromptSubmit` hook that captures `transcript_path` from the hook input
2. When the hook fires (on first user message after resume), read the JSONL transcript file
3. Parse the transcript entries and convert them to our event format
4. Prepend historical events to `manager.events` and emit a ThingState with full history

The transcript format (JSONL) has entries like:
```json
{"type": "user", "sessionId": "...", "uuid": "...", "message": {"role": "user", "content": "..."}}
{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "..."}]}}
```

We'll do best-effort parsing:
- `type: "user"` with `message.role: "user"` → convert to UserMessage-like dict
- `type: "assistant"` with `message.content` → convert to AssistantMessage-like dict
- Unrecognized entries → wrap in `{"type": "unknown_history_event", "raw": ...}`

**Note**: SessionStart hook isn't supported in Python SDK, but `transcript_path` is available in all hook inputs via `BaseHookInput`. We use `UserPromptSubmit` as it fires early.

For MVP, if we can't get the transcript (hook doesn't fire, file missing), we start with empty history - the user will only see new messages. This is acceptable as a fallback.

### 5. Entry Point (`main.py`)

```python
async def main():
    port = 8770  # Or configurable
    db_path = f"/tmp/clydra_{datetime.now().isoformat()}.db"

    manager = ClydraManager(cwd=Path.cwd())
    watcher = Watcher(db_path=db_path, port=port)

    shutdown_event = asyncio.Event()

    async def signal_handler():
        with open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for signum in signals:
                if shutdown_event.is_set():
                    outer_cancel_scope.cancel()
                    return
                print("\nShutting down...")
                shutdown_event.set()
                workflow_cancel_scope.cancel()

    with anyio.CancelScope() as outer_cancel_scope:
        async with anyio.create_task_group() as signals_tg:
            signals_tg.start_soon(signal_handler)
            async with watcher.watching_and_serving():
                try:
                    with anyio.CancelScope() as workflow_cancel_scope:
                        await worker(watcher, manager)
                except Exception as e:
                    print(f"Error: {e}")
                finally:
                    shutdown_event.set()
                await shutdown_event.wait()
            signals_tg.cancel_scope.cancel()
```

### 6. Frontend Renderer (`ClydraRoot.tsx`)

TUI-like styling with:
- Black background (`#0a0a0a`)
- Monospace font throughout
- Minimal colors: white for text, dim gray for metadata, accent color for user messages
- Square outlines using borders
- Character-aligned widths using `calc()` with `ch` units

```tsx
// Basic structure (detailed implementation TBD)
export function FullRenderer({ payload, dataChunks, triggerable, trigger, DraggableIDMarker }: FullRendererProps) {
  // Flatten events from all chunks
  const allEvents = dataChunks.flatMap(chunk => chunk?.events || []);

  // Get current state from last chunk (state is duplicated in each chunk for simplicity)
  const lastChunk = dataChunks[dataChunks.length - 1];
  const state = lastChunk?.state || "setup";
  const sessionId = lastChunk?.session_id;

  // Detect if we're in a resumed session waiting for history
  const isResumedWithoutHistory = sessionId && allEvents.length === 0;

  const [inputText, setInputText] = useState("");

  const handleSend = async () => {
    if (!inputText.trim() || !triggerable) return;
    await trigger({ text: inputText });
    setInputText("");
  };

  return (
    <div style={{
      background: '#0a0a0a',
      color: '#e0e0e0',
      fontFamily: 'monospace',
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
    }}>
      {/* Conversation area */}
      <div style={{ flex: 1, overflow: 'auto', padding: '8px' }}>
        {isResumedWithoutHistory && (
          <div style={{ color: '#666', fontStyle: 'italic', padding: '8px 0' }}>
            (Resumed session {sessionId} - history will load after first message)
          </div>
        )}
        {allEvents.map((event, i) => (
          <MessageRenderer key={i} event={event} />
        ))}
      </div>

      {/* Input area */}
      <div style={{
        borderTop: '1px solid #333',
        padding: '8px',
      }}>
        <input
          value={inputText}
          onChange={e => setInputText(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSend()}
          disabled={!triggerable}
          placeholder={triggerable ? "Type a message..." : "Waiting..."}
          style={{
            width: '100%',
            background: '#1a1a1a',
            color: '#e0e0e0',
            border: '1px solid #333',
            padding: '8px',
            fontFamily: 'inherit',
          }}
        />
      </div>
    </div>
  );
}

function MessageRenderer({ event }) {
  // Render based on event type (check for content structure)
  // SDK messages have different shapes:
  // - UserMessage: { content: string | [...] }
  // - AssistantMessage: { content: [{type: "text", text: "..."}, ...], model: "..." }
  // - ResultMessage: { session_id: "...", duration_ms: ..., total_cost_usd: ... }
  // - History events: { type: "user_message" | "assistant_message" | "unknown_history_event" }
  //
  // For text blocks: show directly
  // For tool_use blocks: collapse in <details>
  // For thinking blocks: collapse in <details> (dimmer styling)
  // For ResultMessage: small footer with cost info, collapsed
}
```

**Key styling rules**:
- Integer character widths: Use `calc(Nch - 2px)` for borders
- No rounded corners (use square)
- Colors: `#e0e0e0` (text), `#888` (dim), `#4a90d9` (accent), `#2a2a2a` (panels)
- Collapsible sections use native `<details>/<summary>`

## Dependencies

`pyproject.toml`:
```toml
[project]
name = "clydra"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "anyio>=4.0",
    "claude-agent-sdk>=0.1.0",
    "cbor2>=5.0",
]

[tool.uv.sources]
thingwatcher = { path = "../thingwatcher", editable = true }

[project.scripts]
clydra = "clydra.main:run"
```

## Implementation Steps

- [x] Create `pyproject.toml` with dependencies
- [x] Create basic package structure (`clydra/__init__.py`)
- [x] Implement `ClydraManager` dataclass
- [x] Implement snapshotter with ThingState/ThingDelta support
- [x] Implement anchor capturer (returns ThingRef)
- [x] Implement `start_new_session()` and `resume_session()` with hook setup
- [x] Implement `load_transcript_history()` and `parse_transcript_entry()` for resume
- [x] Implement worker loop with receive/streaming_update pattern
- [x] Implement signal handling and graceful shutdown
- [x] Create entry point that starts watcher and worker
- [x] Create `renderers/deno.json` for typechecking
- [x] Implement `ClydraRoot.tsx` renderer:
  - [x] Basic conversation display
  - [x] Input box with trigger
  - [x] Message rendering with details/summary for tool calls
  - [x] TUI styling
- [ ] Test manually: start clydra, send messages, verify streaming works
- [ ] Test `/clear` and `/resume` commands
- [x] Update CODE_OVERVIEW.md

## Resolved Questions (from SDK research)

1. **SDK session_id access**: ✅ Available from `ResultMessage.session_id` after each query completes. We capture it in the streaming loop.

2. **SDK message dict conversion**: ✅ Use `dataclasses.asdict(message)` - the SDK uses Python dataclasses, not Pydantic.

3. **Triggerable state**: ✅ Thingwatcher handles triggerability based on pending `receive()`. We track `state` in thing data for UI display only.

## Remaining Uncertainties

1. **Error handling**: If SDK connection fails or Claude returns an error (e.g., `AssistantMessage.error` is set), how should this surface? For MVP, we'll include error messages in the event stream and render them distinctly in the UI.

2. **Interrupt during streaming**: The SDK supports `client.interrupt()` but only while actively consuming messages. For the "afterward" interrupt feature, we'll need a separate task that can call interrupt. This is out of scope for step 1.

3. **Resume history timing**: The `UserPromptSubmit` hook fires on first user message, so history loads after user sends their first message in the resumed session. This means there's a brief moment where the UI shows empty. This is acceptable for MVP.
