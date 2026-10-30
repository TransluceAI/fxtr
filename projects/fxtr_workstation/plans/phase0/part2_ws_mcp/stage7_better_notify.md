# Stage 7 — consolidated notifications + `wait_for_slot_notification`

Goal: make notification delivery a coherent stream rather than a
drip-feed of separate `notifications/claude/channel` frames.
Notifications produced *during* a tool call land in that tool's
response; notifications produced *between* tool calls are debounced
into a single channel frame; the agent gets a new tool —
`wait_for_slot_notification` — to block explicitly on a slot
instead of polling between drip-fed updates.

## Motivation

Today, every slot promotion writes one raw `notifications/claude/channel`
frame inline from `_apply_state_effect → _maybe_notify → _notify_channel`.
The agent's view becomes:

```
<tool_result for run_script>Script ran. Committed: ...</tool_result>
<channel ... slot_key="r" kind="invoke_result">42</channel>
<channel ... slot_key="s" kind="invoke_result">"hello"</channel>
```

— a stream of small frames the agent has to react to one at a time.
Subscribe-time replay can produce many such frames in rapid
succession. We want consolidation: events bundled into the
tool result that triggered them, or batched into one channel frame
when the agent is idle.

## Scope

- New module `outgoing_buffer.py` implementing `OutgoingBuffer`,
  owned by `AppContext` (lifespan-scoped).
- Rewire `WorkstationConnection`'s `notify_channel` callable to
  `buffer.add` (signature unchanged). Buffer owns the actual
  transport delivery via the existing `make_channel_notifier`.
- Update each connection-dependent MCP tool (`connect`,
  `open_session`, `close_session`, `run_script`) to wrap its body
  in `buffer.tool_window()` and append the drained text to its
  result. `run_script` and `connect` additionally
  `await anyio.sleep(0.1)` before draining so just-in-time events
  make it into the response. `set_working_dir` is skipped (no
  connection exists during it).
- New `<event slot_key="..." kind="...">content</event>` render
  shape; replaces direct `<channel>`-flavored writes.
- New MCP tool `wait_for_slot_notification(slot_key, timeout_seconds)`.
- Emit a small `kind="self_commit"` notification from
  `_do_ack_commit_job` so the agent can locate their own
  mutation's seq in the event stream.

Out of scope:

- Changing the dedupe model (`_notified_seqs` stays).
- HTML-escaping content/attrs.
- Changing the channel-capability declaration or the underlying
  raw-frame helper in `channel.py`.

## Design

### `OutgoingBuffer`

Single class in `outgoing_buffer.py`. Roughly:

```
class OutgoingBuffer:
    _pending: list[Message]            # (content, attrs); ordered
    _tool_active_depth: int            # count of open tool_windows
    _flush_scheduled: bool             # at-most-one debounced flush
    _slot_watchers: dict[str, list[anyio.Event]]
    _notify_channel: ChannelNotifier   # underlying transport
    _task_group: anyio.abc.TaskGroup   # for start_soon
    _debounce_seconds: float = 0.1

    def add(content, attrs) -> None:           # sync — no awaits inside
        append to _pending
        for any slot_watchers matching attrs["slot_key"]: set + clear
        if _tool_active_depth > 0: return            # tool will drain
        if _flush_scheduled: return                  # already pending
        _flush_scheduled = True
        try: _task_group.start_soon(_debounced_flush)
        except RuntimeError: log + leave _flush_scheduled True  # tg cancelled, lifespan dying

    async def _debounced_flush() -> None:
        try: await anyio.sleep(_debounce_seconds)
        finally: pass
        if _tool_active_depth > 0:    # tool started during sleep
            _flush_scheduled = False
            return                    # let the tool drain at exit
        text = drain_to_text()
        _flush_scheduled = False
        if text:
            try: await _notify_channel(text, {"kind": "events"})
            except Exception: log + swallow; keep nothing pending
                              (pending was already cleared, see below)

    @contextmanager
    def tool_window() -> Iterator[Callable[[], str]]:
        _tool_active_depth += 1
        try: yield drain_to_text
        finally: _tool_active_depth -= 1

    def watch_slot(slot_key) -> anyio.Event
    def unwatch_slot(slot_key, ev) -> None
```

Render at drain time: each `Message(content, attrs)` becomes
`<event {sorted_attr_str}>{content}</event>`. Multiple events
newline-joined.

Signature notes:

- `add(content, attrs)` is **sync** — it has no awaits internally,
  so making it sync is more honest. To keep
  `WorkstationConnection`'s `_notify_channel` call sites unchanged
  (they currently `await self._notify_channel(...)`), we wrap it
  in a small async closure where we hand the callable to the
  connection: `async def _adapter(c, a): buffer.add(c, a)`.
- `tool_window()` is a **sync** ctx-mgr — depth bookkeeping needs
  no awaits and using a sync cm gives clean exception semantics.
- The trailing `anyio.sleep(0.1)` is the *caller's* responsibility
  — `tool_window` doesn't add a sleep itself, since only some
  tools need it.

### Flush failure handling

On `_notify_channel` raise inside the debounced flush:

- Log via `_log.exception(...)`.
- Drop the messages we tried to send (don't re-queue). The pending
  list was already cleared by `drain_to_text()`. Rationale: if the
  transport just failed, re-queuing risks unbounded growth on a
  permanently-broken session. The next tool drain is a fresh
  start.

### AppContext integration

`AppContext` gains:

```
buffer: OutgoingBuffer | None = None   # set in lifespan body
```

`lifespan` constructs it:

```
async with anyio.create_task_group() as tg:
    app_ctx = AppContext(task_group=tg)
    app_ctx.buffer = OutgoingBuffer(
        notify_channel=make_channel_notifier(lambda: app_ctx.latest_session),
        task_group=tg,
    )
    try: yield app_ctx
    finally: ...   # unchanged
```

`_ensure_connected` passes a thin async adapter
(`async def _adapter(c, a): app_ctx.buffer.add(c, a)`) as the
`notify_channel` argument to `WorkstationConnection`, instead of
the raw channel notifier. The connection's supervisor still calls
`await self._notify_channel(content, attrs)` exactly as today; no
supervisor-side changes other than the self_commit emit.

### Tool wrapping

Each tool body becomes:

```
async def run_script(script, ctx):
    app_ctx = _app_ctx(ctx)
    with app_ctx.buffer.tool_window() as drain:
        try:
            result = await _run_script_inner(...)
        except Exception as e:
            result = f"Error: {e}"
        await anyio.sleep(0.1)        # only for run_script + connect
    return _combine(result, drain())
```

Where `_combine(primary, drained)` joins them with a newline if
`drained` is non-empty (and omits the trailing newline otherwise).

The post-delay (`anyio.sleep(0.1)`) is applied to **`run_script`
and `connect` only**. Rationale:

- `run_script`: a fast invoke (e.g. `delayed-echo` with `delay=0`)
  can land its result event within milliseconds of the mutation
  commit. The sleep gives the worker time to drain that event.
- `connect`: subscribe-time replay of `subsequent_events` lands
  events as `_EventJob`s that the worker processes after
  `wait_ready` returns. The sleep gives them time to be drained
  into the buffer.
- Other tools (`open_session`, `close_session`): don't trigger
  async server-side work whose results can land in this window.
- `set_working_dir` is **not wrapped** at all — it runs before
  any connection exists, so the buffer is unconditionally empty
  during it and there's no reason to incur even the no-op
  depth-bookkeeping.

`tool_window` exit always runs (sync ctx-mgr `finally`), so even
if the inner body raises before our explicit handling, depth
decrements correctly. Pending messages stay in the buffer if the
tool didn't drain — the next tool call or debounced flush picks
them up.

### Render format

```
<event slot_key="r" kind="invoke_result">repr(payload)</event>
```

- Attributes alphabetized for determinism.
- No `source` attribute on the `<event>` itself — when drained
  into a tool result the agent sees it inline next to the tool
  they just called, so the MCP is unambiguous from context.
  When drained into a debounced channel frame the outer
  `<channel source="workstation_mcp" …>` carries the source.
- No HTML-escaping (consistent with today's `<channel …>`).
- Multiple events separated by `\n`.

For debounced flushes the rendered events go into a channel frame
as the `content`, with `meta={"kind": "events"}`. The agent sees:

```
<channel source="workstation_mcp" kind="events">
<event ...>...</event>
<event ...>...</event>
</channel>
```

For tool drains, raw `<event …>` blocks are inline in the tool
result text, no outer wrapping.

### `wait_for_slot_notification`

```
async def wait_for_slot_notification(
    slot_key: str,
    timeout_seconds: float,
    ctx: Context,
) -> str:
```

Required args. `timeout_seconds` must be a positive float (no
default, no None — agent must commit to a bound).

Body sketch:

```
app_ctx = _app_ctx(ctx)
with app_ctx.buffer.tool_window() as drain:
    if app_ctx.connection is None:  return "Error: not connected"
    current = app_ctx.connection.peek_slot_key(slot_key)
    if current is None:
        return f"Error: slot {slot_key!r} is not present in session state."
    if not _is_pending(current):
        return f"Error: slot {slot_key!r} is already terminal: {current!r}"
    ev = app_ctx.buffer.watch_slot(slot_key)
    try:
        timed_out = True
        with anyio.move_on_after(timeout_seconds) as scope:
            await ev.wait()
            timed_out = False
    finally:
        app_ctx.buffer.unwatch_slot(slot_key, ev)
    drained = drain()
    if timed_out:
        return (
            f"{drained}\nTimed out after {timeout_seconds}s waiting "
            f"for slot {slot_key!r}."
            if drained
            else f"Timed out after {timeout_seconds}s waiting for "
            f"slot {slot_key!r}."
        )
    return drained
```

`connection.peek_slot_key(slot_key)` is a new sync accessor on
`WorkstationConnection`: returns `self._mirror.data_by_key.get(slot_key)`
(or `None`). Safe because anyio is single-threaded and the call
is sync — the worker can't be mutating the mirror mid-read from
the tool's perspective.

`_is_pending(value)` returns `True` if `value[0]` is any tag
denoting "future notifications expected": `invoke_pending`,
`load_pending`, `callback_open`. Phrased as a positive
allowlist of "still active" tags rather than a denylist of
"terminal" tags so new pending/open kinds don't silently fall
through to the terminal-error branch when added in later
stages.

### Self-commit notification

`_do_ack_commit_job` gains a `buffer.add` call. The job's `seq`
is the workstation-acked seq for our mutation:

```
async def _do_ack_commit_job(self, job):
    self._last_processed_seq = max(self._last_processed_seq, job.seq)
    await self._notify_channel(
        f"Your script's mutation committed at seq={job.seq}.",
        {"kind": "self_commit", "seq": str(job.seq)},
    )
```

(`self._notify_channel` is the buffer's `add` at runtime.)

Rationale: gives the agent a temporal landmark in the drained
event stream so they can tell which slot promotions came before
vs after their own mutation. See scratch doc for the worked
example.

### CODE_OVERVIEW updates

New module bullet (`outgoing_buffer.py`). New "Outgoing message
buffer" subsection under "Mental model" describing tool_window /
debounced flush / watcher mechanics. Update the existing
"Channel notifications are deduped by seq" item to clarify that
`_notify_channel` now points at the buffer's `add` rather than
the raw channel writer.

## Files touched

- `fxtr_workstation_mcp/outgoing_buffer.py` — new module.
- `fxtr_workstation_mcp/app_context.py` — add `buffer` field.
- `fxtr_workstation_mcp/server.py` — construct buffer in lifespan;
  wrap every tool body in `tool_window`; pass `buffer.add` to
  `WorkstationConnection`; add `wait_for_slot_notification` tool;
  add 0.1s trail-sleep in `run_script` + `connect`.
- `fxtr_workstation_mcp/channel.py` — unchanged (its
  `make_channel_notifier` is now consumed by `OutgoingBuffer`).
- `fxtr_workstation_mcp/ws_connection.py` — emit self_commit
  notification from `_do_ack_commit_job`; add `peek_slot` accessor.
- `CODE_OVERVIEW.md` (under `fxtr_workstation_agent_plugin/`) —
  document the buffer + the new tool + the new event shape.
- `claude_plugin/skills/use_fxtr_workstation/SKILL.md` —
  document the new `<event …>` shape the agent should expect and
  the new `wait_for_slot_notification` tool. (Existing instructions
  reference the `<channel …>` shape; update to point at the new
  inline `<event …>` form for tool results.)
- `fxtr_workstation_mcp/tests/` — new test cases (see below).

## Implementation steps

1. [x] **Write `OutgoingBuffer` module.** Add `Message` dataclass,
   `OutgoingBuffer` class with all methods (`add`, `tool_window`,
   `watch_slot`/`unwatch_slot`, `_debounced_flush`, render
   helper). Module-level `_render_message(m)` builds the
   `<event …>` string with alphabetized attrs.
   Update CODE_OVERVIEW.md.
2. [x] **Wire into AppContext + lifespan.** Add `buffer` field;
   construct in `lifespan` before `yield`. Adjust
   `_ensure_connected` to pass `app_ctx.buffer.add` as the
   `notify_channel`.
3. [x] **Wrap relevant tool bodies in `tool_window`.** For
   `connect`, `open_session`, `close_session`, and `run_script`,
   restructure the body so the primary result is computed inside
   the with-block and `drain()` is called at the end. Add
   `anyio.sleep(0.1)` inside the with-block for `run_script` and
   `connect` only. Introduce a small `_combine(primary, drained)`
   helper for joining. `set_working_dir` is intentionally not
   wrapped (no connection exists during it, so the buffer is
   always empty).
4. [x] **Add the self_commit notification.** Inside
   `_do_ack_commit_job`, emit one `buffer.add` after advancing
   `_last_processed_seq`. (Goes through the supervisor's
   `_notify_channel` callable, which is now the buffer's `add`.)
   Update CODE_OVERVIEW.md.
5. [x] **Add `peek_slot_key` accessor on `WorkstationConnection`.**
   Sync method returning `self._mirror.data_by_key.get(slot_key)`.
6. [x] **Implement `wait_for_slot_notification` tool.** New
   `@mcp.tool()` in `server.py`. Validates `timeout_seconds > 0`;
   error returns for unknown / already-terminal slots; uses
   `buffer.watch_slot` + `anyio.move_on_after`. Update
   CODE_OVERVIEW.md.
7. [x] **Update SKILL.md.** Replace `<channel …>` example with new
   `<event …>` shape. Add `wait_for_slot_notification` to the
   tool list.
8. [x] **Tests.** See below; each as its own test function.
9. [x] **Final CODE_OVERVIEW.md pass.** Make sure the "Mental model"
   section reflects the new flow end-to-end; cross-check the file
   listing.

## Testing

New tests live next to existing ones in
`fxtr_workstation_mcp/tests/`. They use the existing `_FakeContext`
+ in-process `WorkstationServer` pattern.

**Event-injection mechanism.** Tests drive events into the
session via the MCP's callback support (stage 6): a script
creates a callback, then the test fires it on demand. A
`CallbackEvent` lands in the MCP's reader → worker queue, same
shape as any other non-state event. This is simpler and less
timing-sensitive than spinning up a second foreign-mutator
connection, and it avoids the cross-mutator-seq-interleaving
the foreign-mutator approach needed.

Focus areas:

- **In-tool buffering.** Run a script that creates a callback,
  fires it during the same script body, then exits. Verify the
  tool's response string contains a `<event …>` block for the
  callback fire and that no channel notification was sent over
  the wire during the tool call.
- **Debounced flush when idle.** Between tool calls, fire a
  previously-created callback; verify a single channel
  notification arrives ~0.1s later containing the rendered
  `<event …>` block.
- **Multiple events per debounced flush.** Fire two callbacks
  in quick succession; verify they're rendered in one channel
  frame with two `<event …>` blocks.
- **Tool starts during debounce window.** Trigger a callback to
  start a flush, then enter a tool window within ~50ms; verify
  the tool drains the pending notifications (no channel frame
  sent).
- **wait_for_slot_notification — pending slot, resolved
  in-flight.** Schedule an invoke (or open a callback), call
  `wait_for_slot_notification` in a separate task with
  `timeout_seconds=2.0`; the result/callback arrives; verify the
  tool returns the rendered event string within the timeout.
- **wait_for_slot_notification — timeout.** Wait on a slot that
  won't be promoted; verify the tool returns the timeout error
  string after `timeout_seconds`.
- **wait_for_slot_notification — already terminal.** Schedule
  an invoke, wait for result, then call
  `wait_for_slot_notification` for the same slot; verify error
  return `"slot ... already terminal"`.
- **wait_for_slot_notification — unknown slot.** Call with a
  slot_key that's not in the session's data_by_key; verify
  error return.
- **Self-commit notification appears.** `run_script` that does
  any mutation; verify the drained tool result contains a
  `<event kind="self_commit" ...>` line.
- **Render format.** Direct unit test on `_render_message`:
  attributes alphabetized, no escaping, exact tag shape.

Skipped on purpose: self-commit *ordering* (interleaving the
self_commit notification between pre- and post-commit foreign
events). It's the most timing-sensitive case and the ordering is
already a structural property of the queue invariants — covered
by the existing `_AckCommitJob` queue-position docs/tests. Worth
revisiting only if we see misordering in practice.

Update CODE_OVERVIEW.md.

## Open questions / things to revisit later

- **Buffer growth on a permanently-broken transport.** If
  `_notify_channel` keeps raising on the debounced flush path,
  we currently drop the failed batch. If the same flush keeps
  failing, the agent silently loses notifications. Acceptable
  for now (a broken stdio means the MCP is going down), but
  worth revisiting if we see this in practice.
- **Escaping in `<event …>`.** Slot keys are identifier-shaped
  by contract; content (especially `summarize_invoke_payload`)
  is `repr()`-flavored and could contain `>` or `"`. Today we
  pass through. If the agent's renderer gets confused, escape
  attrs/content (XML-style) in `_render_message`.
- **Trailing-delay tuning.** 0.1s is a guess. Might be too short
  for slow workstations (event arrives at 0.11s, agent misses
  it; debounced flush catches it 0.2s later, but as a separate
  message). Might be too long for fast iterations. Revisit after
  agent-driven testing.
- **Should we surface the agent's own slot_key when emitting
  the self_commit notification?** The current message doesn't
  mention which slots were affected by the commit. Could
  include `slot_keys=k1,k2` as an attr. Defer — wait until
  agent feedback says it's needed.
- **Update CODE_OVERVIEW.md** (final step, per the project
  convention).
