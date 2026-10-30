# Phase 2 Step 8: Single-Thing Embed View

## Overview

Create a new embeddable route for viewing a single thing at a time, designed for use in VS Code webviews and similar embedding scenarios. The view receives thing keys and custom args via `postMessage`, always shows the latest tick with streaming updates, and supports full custom rendering and triggering.

## Goals

1. **`/embed` route** - Lightweight single-thing viewer page
   - Separate HTML page with minimal UI (no dockview, timeline, etc.)
   - Receives thing key + args via postMessage from parent window
   - Always live - shows latest tick and updates with streaming
   - Full custom rendering support (DynamicRenderer)
   - Full triggering support (when thing is triggerable)

2. **`/debug-embed` route** - Debug/test UI for the embed view
   - Separate HTML page that embeds `/embed` in an iframe
   - Controls: input for thing key (keyJSON or base64), textarea for args JSON
   - Button to send postMessage to the iframe
   - Useful for testing the postMessage protocol

3. **Demo server** - Test the feature end-to-end
   - Spawn entities with custom renderers that use args
   - Verify args flow through correctly

## postMessage Protocol

### Incoming Messages (to `/embed`)

```typescript
// Show a specific thing with optional args
interface ShowThingMessage {
  type: 'showThing';
  // Provide exactly one of these:
  key?: string;      // base64-encoded CBOR key
  keyJSON?: unknown; // JSON-compatible key value
  // Optional args passed to the custom renderer
  args?: Record<string, unknown>;
}
```

When the embed view receives this message, it:
1. Resolves the thing at the current (latest) tick
2. Renders it with the custom renderer (if registered) or fallback
3. Passes `args` to the renderer's props

## Architecture

### New Files

```
thingwatcher/frontend/
├── embed.html              # Minimal HTML for embed view
├── embed.tsx               # Entry point for embed app
├── EmbedApp.tsx            # Single-thing viewer component
├── debug-embed.html        # HTML for debug UI
└── debug-embed.tsx         # Entry point for debug UI
```

### Component Structure

**EmbedApp.tsx**:
```
EmbedApp
├── Sets up urql client (same as main app)
├── Subscribes to newTick (with streaming updates)
├── Listens for postMessage
├── Renders single thing:
│   ├── RendererProvider (for ThingView/ThingLink support)
│   └── SingleThingRenderer
│       ├── Fetches thing data at latest tick
│       ├── DynamicRenderer (if custom renderer exists)
│       └── Fallback content (JSON tree)
```

**DebugEmbedApp**:
```
DebugEmbedApp
├── iframe pointing to /embed
├── Input field for thing key (with keyJSON/base64 toggle)
├── Textarea for args JSON
├── "Send" button to postMessage to iframe
└── Status display (current thing being shown)
```

### Server Changes

Add new routes in `server.py`:
- `GET /embed` - serves embed.html
- `GET /embed.js` - serves compiled embed app
- `GET /debug-embed` - serves debug-embed.html
- `GET /debug-embed.js` - serves compiled debug app

The `FrontendHandler` will be extended to compile these additional entry points.

## Implementation Details

### EmbedApp State

```typescript
interface EmbedState {
  // Current thing to display (from postMessage)
  thingKey: string | null;      // base64 key
  keyJSON: unknown | null;      // or JSON key
  args: Record<string, unknown>;

  // Live tick tracking
  currentTick: number;

  // urql client
  client: Client;
}
```

### Message Handling

```typescript
useEffect(() => {
  const handler = (event: MessageEvent) => {
    const data = event.data;
    if (data?.type === 'showThing') {
      // Update state with new thing key + args
      setThingKey(data.key ?? null);
      setKeyJSON(data.keyJSON ?? null);
      setArgs(data.args ?? {});
    }
  };
  window.addEventListener('message', handler);
  return () => window.removeEventListener('message', handler);
}, []);
```

### SingleThingRenderer Component

Reuses logic from `DetailsPanel` but simplified:
- No selection management (always shows the one thing)
- No backlinks section (keep it minimal for embed)
- Standard header with thingType + shortId + key
- Custom renderer with args passed through
- Trigger form (if triggerable)
- Fallback JSON tree view

Key difference from DetailsPanel: The `args` prop is populated from the postMessage, not hardcoded to `{}`.

### Rendering with Args

The `ThingRendererProps.args` field is already defined in `ThingWatcherBindings.tsx`. Custom renderers can use it like:

```tsx
export default function MyRenderer({ payload, args }: ThingRendererProps) {
  // args comes from postMessage
  const highlightField = args.highlight as string | undefined;

  return (
    <div>
      <h3 style={{ color: highlightField === 'title' ? 'yellow' : 'white' }}>
        {payload.title}
      </h3>
    </div>
  );
}
```

### Live Updates

The embed app subscribes to `newTick` with `includeStreamingUpdates: true`:

```typescript
const [subscriptionResult] = useSubscription({
  query: NEW_TICK_SUBSCRIPTION,
  variables: { includeStreamingUpdates: true },
});

// When new tick arrives, refetch thing data
useEffect(() => {
  if (subscriptionResult.data?.newTick) {
    setCurrentTick(subscriptionResult.data.newTick.index);
    // This triggers re-query of thing data
  }
}, [subscriptionResult.data]);
```

### Debug UI

The debug page is simple HTML + React:

```html
<!-- debug-embed.html -->
<div id="controls">
  <label>Key Type: <select id="keyType">...</select></label>
  <label>Key: <input id="keyInput" /></label>
  <label>Args (JSON): <textarea id="argsInput">{}</textarea></label>
  <button id="sendBtn">Send to Embed</button>
</div>
<iframe id="embedFrame" src="/embed" style="..."></iframe>
```

```typescript
// Send button handler
sendBtn.onclick = () => {
  const message: ShowThingMessage = {
    type: 'showThing',
    [keyType]: keyType === 'keyJSON' ? JSON.parse(keyInput.value) : keyInput.value,
    args: JSON.parse(argsInput.value),
  };
  embedFrame.contentWindow.postMessage(message, '*');
};
```

## Demo Server

Create `examples/embed_demo_server.py`:

```python
import thingwatcher as tw
from dataclasses import dataclass, field
import anyio

@dataclass
class DemoWidget:
    id: str
    title: str
    messages: list[str] = field(default_factory=list)

# Register snapshotter
class WidgetSnapshotter(tw.StatelessSnapshotter):
    def snapshot(self, obj: DemoWidget) -> tw.ThingState:
        return tw.ThingState(
            key={"widget": obj.id},
            thing_type="Widget",
            thing_data={
                "title": obj.title,
                "messages": obj.messages,
            },
            links=[],
        )

tw.register_snapshotter(DemoWidget, WidgetSnapshotter())

# Anchor capturer for receive() - widget is its own anchor
tw.register_anchor_capturer(DemoWidget, WidgetSnapshotter())

# Register custom renderer that uses args and shows trigger UI
tw.register_renderer("Widget", '''
import React from 'react';

export default function WidgetRenderer({ payload, args, triggerable, trigger }) {
  const [input, setInput] = React.useState('');
  const highlight = args.highlight;

  const handleTrigger = async () => {
    if (input.trim()) {
      await trigger({ message: input });
      setInput('');
    }
  };

  return (
    <div style={{ padding: 16, background: '#222', borderRadius: 8 }}>
      <h2 style={{ color: highlight === 'title' ? '#ff0' : '#fff' }}>
        {payload.title}
      </h2>

      {/* Show received messages */}
      <div style={{ marginTop: 12 }}>
        <h4 style={{ color: '#888', margin: '0 0 8px 0' }}>
          Messages ({payload.messages.length}):
        </h4>
        {payload.messages.length === 0 ? (
          <p style={{ color: '#666', fontStyle: 'italic' }}>No messages yet</p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 20, color: '#aaa' }}>
            {payload.messages.map((msg, i) => (
              <li key={i}>{msg}</li>
            ))}
          </ul>
        )}
      </div>

      {/* Trigger form */}
      {triggerable && (
        <div style={{ marginTop: 16, display: 'flex', gap: 8 }}>
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleTrigger()}
            placeholder="Type a message..."
            style={{
              flex: 1,
              padding: '8px 12px',
              background: '#333',
              border: '1px solid #555',
              borderRadius: 4,
              color: '#fff',
            }}
          />
          <button
            onClick={handleTrigger}
            style={{
              padding: '8px 16px',
              background: '#4a7',
              border: 'none',
              borderRadius: 4,
              color: '#fff',
              cursor: 'pointer',
            }}
          >
            Send
          </button>
        </div>
      )}

      {/* Show args from postMessage */}
      {args.message && (
        <p style={{ marginTop: 12, color: '#8f8' }}>
          Args message: {args.message}
        </p>
      )}
    </div>
  );
}
''')

async def widget_loop(widget: DemoWidget):
    """Loop receiving messages and adding them to the widget."""
    while True:
        # Wait for trigger with receive()
        payload = await tw.receive(widget, DemoWidget)

        # Add message to widget data
        if payload and isinstance(payload, dict) and 'message' in payload:
            widget.messages.append(payload['message'])

        # Tick to save the new state
        tw.tick("info", f"Widget {widget.id} received message")

async def main():
    watcher = tw.Watcher(db_path=":memory:", port=8765)

    async with watcher.watching_and_serving():
        # Create widgets
        w1 = DemoWidget("alpha", "First Widget")
        w2 = DemoWidget("beta", "Second Widget")

        tw.track(w1, DemoWidget)
        tw.track(w2, DemoWidget)

        tw.tick("info", "Initial widgets created")

        print("Server running at http://localhost:8765")
        print("Debug embed UI at http://localhost:8765/debug-embed")
        print()
        print("Try these steps:")
        print('  1. Open /debug-embed')
        print('  2. Enter key (keyJSON): {"widget": "alpha"}')
        print('  3. Enter args: {"highlight": "title", "message": "Hello!"}')
        print('  4. Click Send to show the widget')
        print('  5. Type in the input and click Send to trigger the widget')
        print('  6. Watch messages accumulate in the widget data!')

        # Run receive loops for both widgets
        async with anyio.create_task_group() as tg:
            tg.start_soon(widget_loop, w1)
            tg.start_soon(widget_loop, w2)

if __name__ == "__main__":
    anyio.run(main)
```

This demo shows:
1. **Args from postMessage** - `args.highlight` and `args.message` affect rendering
2. **Triggerable things** - widgets wait in `receive()` loop, showing trigger UI
3. **Data updates** - triggered messages get added to `widget.messages` and persisted
4. **Live updates** - embed view sees new messages appear in real-time

## Implementation Steps

- [x] 1. Create `embed.html` - minimal HTML page for embed view
- [x] 2. Create `EmbedApp.tsx` - single-thing viewer with postMessage handling
- [x] 3. Create `embed.tsx` - entry point that renders EmbedApp
- [x] 4. Create `debug-embed.html` - HTML for debug UI
- [x] 5. Create `DebugEmbedApp.tsx` - debug UI component
- [x] 6. Create `debug-embed.tsx` - entry point for debug UI
- [x] 7. Update `FrontendHandler` in `server.py` to compile and serve new entry points
- [x] 8. Add routes for `/embed`, `/embed.js`, `/debug-embed`, `/debug-embed.js`
- [x] 9. Create `examples/embed_demo_server.py` demo
- [x] 10. Test end-to-end: start demo, open debug-embed, send postMessage, verify args
- [x] 11. Update CODE_OVERVIEW.md and LOCAL_CODE_OVERVIEW.md

## Testing Checklist

- [x] `/embed` loads without errors
- [~] postMessage with `key` (base64) works (untested but not that important)
- [x] postMessage with `keyJSON` works
- [x] Args are passed to custom renderer
- [x] Live updates work (tick subscription)
- [x] Streaming updates work
- [x] Triggering works from embed view
- [x] Debug UI can send messages to iframe
- [x] Demo server works end-to-end

## Future Extensions (out of scope)

- VS Code extension that uses this embed view
- Outgoing messages (trigger events, tick changes)
- Multiple things in embed view
- Persistent state for embedded renderers
