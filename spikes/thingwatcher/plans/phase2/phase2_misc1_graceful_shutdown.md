# Phase 2 Misc: Graceful Shutdown for WebSocket Subscriptions

## Problem

When running thingwatcher with the HTTP server (e.g., `demo_pause_trigger.py`), pressing Ctrl-C initiates a graceful shutdown. However, if there are active GraphQL WebSocket subscriptions (e.g., a client subscribed to `newTick`), the shutdown hangs indefinitely.

The server never fully stops because hypercorn's graceful shutdown waits for all open connections to close, but the WebSocket connections backing the subscriptions never close.

## Cause

The issue is in how subscriptions are implemented. Looking at `new_tick_source()` in `server.py`:

```python
@subscription.source("newTick")
async def new_tick_source(_: Any, info: GraphQLResolveInfo) -> Any:
    watcher: Watcher = info.context["watcher"]

    send, receive = anyio.create_memory_object_stream[dict](max_buffer_size=100)

    def on_tick(tick_data: dict[str, Any]) -> None:
        try:
            send.send_nowait(tick_data)
        except anyio.WouldBlock:
            pass

    watcher._add_tick_callback(on_tick)
    try:
        async for tick_data in receive:  # <-- Blocks forever during shutdown
            yield {...}
    finally:
        watcher._remove_tick_callback(on_tick)
        await send.aclose()
```

The subscription creates its own memory stream and registers a callback with the watcher. The problem is:

1. When shutdown begins, `graceful_shutdown_event.set()` signals hypercorn to stop
2. Hypercorn stops accepting new connections and waits for existing ones to close
3. The subscription generator is blocked on `async for tick_data in receive:`
4. Nothing ever signals this stream to close - it just waits for data that will never come
5. The WebSocket connection stays open, hypercorn waits indefinitely

The watcher only stores callback functions, not the streams themselves, so it has no way to signal subscriptions to stop.

## Solution

Change the watcher to store the actual `MemoryObjectSendStream` objects instead of callback functions. This gives the watcher direct control over subscription lifecycles:

1. Subscriptions register their send stream with the watcher
2. The watcher sends tick data directly to all registered streams
3. During shutdown, the watcher closes all registered streams
4. Closing a send stream causes the corresponding receive stream to complete iteration
5. Subscription generators exit cleanly, WebSocket connections close
6. Hypercorn sees all connections closed and completes graceful shutdown

## Implementation Plan

### Step 1: Update watcher stream management

In `watcher.py`, replace the callback-based system with a stream-based one:

**Changes to `__init__`:**
- Replace `_tick_callbacks: list[Callable]` with `_tick_streams: list[MemoryObjectSendStream]`
- Keep the lock (rename to `_tick_streams_lock`)

**Replace `_add_tick_callback` with `_add_tick_stream`:**
```python
def _add_tick_stream(self, stream: MemoryObjectSendStream[dict[str, Any]]) -> None:
    """Register a stream to receive tick notifications."""
    with self._tick_streams_lock:
        self._tick_streams.append(stream)
```

**Replace `_remove_tick_callback` with `_remove_tick_stream`:**
```python
def _remove_tick_stream(self, stream: MemoryObjectSendStream[dict[str, Any]]) -> None:
    """Unregister a tick stream."""
    with self._tick_streams_lock:
        if stream in self._tick_streams:
            self._tick_streams.remove(stream)
```

**Update `_notify_tick_subscribers`:**
```python
def _notify_tick_subscribers(self, tick_data: dict[str, Any]) -> None:
    """Send tick data to all registered streams."""
    with self._tick_streams_lock:
        streams = list(self._tick_streams)
    for stream in streams:
        try:
            stream.send_nowait(tick_data)
        except anyio.WouldBlock:
            pass  # Buffer full, drop this tick for this subscriber
        except anyio.ClosedResourceError:
            logger.warning("Tick stream closed unexpectedly - subscriber may have leaked")
```

**Add `_close_all_tick_streams`:**
```python
async def _close_all_tick_streams(self) -> None:
    """Close all tick streams during shutdown."""
    with self._tick_streams_lock:
        streams = list(self._tick_streams)
        self._tick_streams.clear()
    for stream in streams:
        await stream.aclose()
```

### Step 2: Update shutdown sequence

In `watching_and_serving()`, close tick streams before signaling hypercorn:

```python
try:
    yield
finally:
    # Close all subscription streams first - this causes subscription
    # generators to exit and WebSocket connections to close
    await self._close_all_tick_streams()

    # Now signal hypercorn to stop (connections should already be closing)
    graceful_shutdown_event.set()

    # Close the write stream
    await self._write_stream.aclose()
```

### Step 3: Update subscription in server.py

Simplify `new_tick_source` to register the stream directly:

```python
@subscription.source("newTick")
async def new_tick_source(_: Any, info: GraphQLResolveInfo) -> Any:
    watcher: Watcher = info.context["watcher"]

    send, receive = anyio.create_memory_object_stream[dict](max_buffer_size=100)

    watcher._add_tick_stream(send)
    try:
        async for tick_data in receive:
            yield {
                "index": tick_data["tick"],
                "timestamp": tick_data["timestamp"],
                "level": tick_data["level"],
                "type": tick_data["type"],
                "desc": tick_data["data"],
                "_watcher": watcher,
            }
    finally:
        watcher._remove_tick_stream(send)
        # Note: don't need to aclose(send) here - either:
        # 1. Client disconnected (stream still open, remove is enough)
        # 2. Shutdown closed it (already closed)
```

### Step 4: Update cleanup in watching_and_serving finally block

Also clear `_tick_streams` in the final cleanup (after task group exits):

```python
finally:
    if self._db:
        await self._db.close()
    self._current_things.clear()
    self._tick_streams.clear()  # Was: self._tick_callbacks.clear()
    reset_current_watcher(token)
```

### Step 5: Test

1. Run `demo_pause_trigger.py`
2. Open a GraphQL client with a `newTick` subscription
3. Press Ctrl-C
4. Verify server shuts down cleanly without hanging

## Edge Cases

- **Client disconnects before shutdown**: The subscription's `finally` block calls `_remove_tick_stream`, cleaning up. The stream is still open but removed from the list.

- **Multiple subscriptions**: Each subscription has its own stream. All are closed during shutdown.

- **Buffer full**: `send_nowait` raises `WouldBlock` if buffer is full - we catch and ignore this (same as current behavior).

- **Stream already closed**: If we try to send to a closed stream, `ClosedResourceError` is raised. We log a warning because this indicates a subscriber didn't properly unregister its stream before it was closed - likely a bug.

## Update CODE_OVERVIEW.md

No structural changes needed - this is an internal implementation detail of the subscription mechanism.
