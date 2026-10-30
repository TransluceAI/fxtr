# Stage 7 — scratch notes

Working notes that informed the plan; here for posterity, not part
of the spec.

## Why a buffer is the right primitive

Today's flow is:

```
reader → _EventJob → worker → _apply_state_effect → _maybe_notify → _notify_channel
                                                                      ↓
                                                  session._write_stream.send(frame)
```

The agent sees two things back-to-back:

1. A `notifications/claude/channel` frame (the slot promotion).
2. A `tool_result` frame for whatever tool call just finished.

In Claude Code's UI these are separate rendered items, leading to
the "drip-feed" experience. Worse: if multiple events arrive in
rapid succession (e.g. a subscribe-time replay of `subsequent_events`
spanning many slots), the agent sees one channel frame per event.

The buffer fixes this by:

- **Serializing notifications and tool results through one
  abstraction.** Notifications that arrive while a tool is in
  flight become part of that tool's response. The agent sees
  fewer "messages" and gets coherent batches.
- **Allowing rebuild-time consolidation.** After a reconnect,
  many notifications may queue up; they all flush together rather
  than as N separate channel frames.

## Why AppContext-scoped, not connection-scoped

The buffer is fundamentally a *transport-layer* concern (how do we
deliver messages to the agent's MCP session). It doesn't care about
workstation `/rpc` / `/client` connections — it cares about the
agent's stdio session, which the lifespan owns.

If we put it on the connection:

- Buffer would be created when `connect()` first runs, which is
  late enough that early notifications (if any) couldn't use it.
- A supervisor reconnect cycle's task-group cancellation could
  interrupt an in-flight debounced flush task.
- Buffered messages would survive cycle restarts only if we put
  the field on the connection and not on the cycle.

AppContext-scoped sidesteps all of these. The buffer's flush task
lives on the lifespan TG (set once during lifespan entry) and
isn't cancelled by workstation-side reconnects.

## Race analysis — wait_for_slot_notification

The tool body sequence:

```
with buffer.tool_window() as drain:
    current = connection.peek_mirror_slot(slot_key)   # sync; no await
    # branch on `current`:
    #   None        → error
    #   non-pending → error
    #   pending     → continue
    ev = buffer.watch_slot(slot_key)                  # sync
    with anyio.move_on_after(timeout):
        await ev.wait()
    ...
```

The peek + watch sequence has no `await` between them. anyio is
cooperative single-threaded, so the worker task can't run in
between. By the time we await, the watcher is registered.

If an event for `slot_key` arrives between watch registration and
`ev.wait()`: the event setter runs on the worker task during one
of the upstream `await`s in our tool body, sets the anyio.Event,
then our subsequent `await ev.wait()` returns immediately (event
is already set). No race.

If two `wait_for_slot_notification` tools target the same slot
concurrently: the watcher dict maps `slot_key → list[anyio.Event]`,
firing all of them when an event arrives. Both wake up, both drain
(second drain gets empty buffer since the first cleared it). That's
fine — both agents see something.

## Why drained events come *after* the primary result

For `run_script`, the worker can't process foreign `_EventJob`s
until the script ends because the worker is parked inside
`_do_script_job`. So events that arrive *during* the script land
in the queue and only get processed during the trailing-delay
`anyio.sleep(0.1)`. Chronologically these events happened "while
the script was running" (or just after).

Putting the drained-events block after the primary result reads
chronologically: "I ran this script, and these things happened
during/right after."

For `connect`, the events that appear are from `subsequent_events`
in the subscribe snapshot — i.e. things that happened in the
session while we were disconnected, in seq order. Putting them
after the "connected to X" line reads: "I (re)connected, and here
are the events that occurred while I was gone."

I'll go with "always after" for consistency.

## Why a `<event …>` tag (not `<channel …>`)

Today the agent sees frames like:

```
<channel source="workstation_mcp" slot_key="r" kind="invoke_result">42</channel>
```

The `<channel>` shape comes from Claude Code's channel-notification
renderer, which auto-attaches `source` and wraps the body. When
the buffer's debounced flush sends a channel frame, the agent
sees:

```
<channel source="workstation_mcp" kind="events">
<event slot_key="r" kind="invoke_result">42</event>
<event slot_key="s" kind="invoke_result">"hello"</event>
</channel>
```

And when a tool drains buffered events into its result, the agent
sees the raw `<event …>` blocks inline in the tool response:

```
Script ran. Committed: 1 schedule_invoke.
<event slot_key="r" kind="invoke_result">42</event>
```

Using `<event>` (not `<channel>`) for the inner shape avoids
confusion with the outer transport tag. The `source` attribute is
unnecessary inside the buffer since the agent already knows which
MCP produced the message.

## Self-commit notification timing

`_AckCommitJob` is enqueued by `_apply_script_mutation` *after*
the commit response arrives. By queue invariant, it lands behind
any `_EventJob`s for events with `seq < our_commit_seq`. So when
the worker drains:

```
[EventJob(N), AckCommitJob(N+1), EventJob(N+2), ...]
 ↑               ↑                  ↑
 pre-commit     "Script committed"  post-commit
 events                             events
```

The agent's drained buffer reads:

```
{primary_result}
<event ... kind="invoke_result">pre-commit slot promotion</event>
<event ... kind="self_commit">Script action committed at seq=N+1</event>
<event ... kind="invoke_result">post-commit slot promotion</event>
```

Without the self_commit marker, the agent can't tell which slot
promotions came before vs after their `forget`/`export`/etc. With
it, the ordering is unambiguous.

The self_commit message is only meaningful when interleaved with
other events. If a `run_script` has no concurrent foreign events,
the drained buffer is just `{self_commit}` after `{primary_result}`,
which is slightly redundant (the primary result already says
"Committed: N ops"). I think that's fine — the cost is one extra
short line in the common case.

## What `_AckCommitJob` knows

The job has `seq: int`. We can include that in the notification
content for debugging:

```
<event kind="self_commit" seq="42">Your script's mutation committed at seq=42.</event>
```

Nothing else is needed — the agent's mental model already includes
"my script just ran" from the immediately preceding primary result.

## What happens if the buffer's flush fails

`_notify_channel(text, attrs)` writes to `session._write_stream`.
This can fail if:

- The MCP transport closed.
- The session is broken.

If we drop pending messages on flush failure, we lose them. If we
keep them, the next successful drain/flush surfaces them. Keeping
them is the safer default — the agent will catch up when the
transport recovers (e.g. on the next tool call's drain).

A pathological case: pending messages grow unboundedly because
flushes keep failing. Unlikely in practice (a broken stdio means
the MCP process is going down anyway). Not a stage-7 concern.

## Tool exception paths

If a tool body raises inside `tool_window`:

- Sync ctx-mgr `finally` decrements depth.
- Pending messages stay in the buffer (no drain).
- FastMCP catches the exception, sends an MCP error response.
- Next tool call (or a debounced flush) drains the leftover
  messages.

That's the right behavior — we don't want to lose notifications
because of an unrelated tool error. But the agent might see
notifications "out of order" relative to the tool that triggered
them (the next tool's drain includes them). Acceptable for now;
all existing tools catch their own exceptions and return strings,
so this path is rare.

## Could we use anyio.Event for the watcher?

Yes — `anyio.Event` is one-shot (set once, all waiters fire).
That matches our use case: notify once, all `wait_for_slot_notification`
calls for this slot wake up. We don't need fancier primitives.

Multiple watchers for the same slot_key: store a list of Events,
fire-all-and-clear when the slot promotes.

## Module location for OutgoingBuffer

New file: `fxtr_workstation_mcp/outgoing_buffer.py`. Sits alongside
`channel.py` since it's the transport-side abstraction. The
existing `channel.py` stays around as the low-level "send a raw
channel frame" helper (now used only by the buffer's flush path).

## Render escaping concern

The `<event …>` rendering doesn't escape attribute values or
content. If a slot_key contains a `"` or `<`, the rendering would
be malformed XML-ish text. For attrs, slot_keys / kinds are
identifier-shaped today (the channel contract enforces
`[A-Za-z0-9_]`), so safe. For content (e.g.
`summarize_invoke_payload(payload)`), the payload could be
arbitrary — `repr()` could include `>` or quotes.

I think for now we accept this — it's consistent with the existing
`<channel …>` rendering which also doesn't escape. Future hardening
could escape attrs/content if needed. Not a stage-7 concern.

## Existing _notified_seqs

The supervisor's `_notified_seqs` dedupe stays as-is. It prevents
double-emission to the buffer when an event is replayed (e.g. on
subscribe-time rebuild). The buffer doesn't need its own dedupe —
the supervisor never double-adds.
