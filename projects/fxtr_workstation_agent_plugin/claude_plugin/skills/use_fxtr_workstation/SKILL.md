---
name: use_fxtr_workstation
description: Drive a fxtr workstation session via the workstation_mcp tools. Use when the user wants to run sandboxed Python against a fxtr workstation, manage entities and services in a persistent session, or invoke registered services on the workstation.
---

# Use the fxtr workstation

The plugin gives you a persistent, sandboxed Python environment
that's bridged to a long-lived fxtr workstation. Use it when the
user asks to:

- exercise registered workstation services (call them, retrieve
  results, pass entity refs as arguments)
- read entities from the workstation
- accumulate session state across multiple script invocations

## Setup (once per conversation)

1. ``set_working_dir(<absolute path>)`` — point at a directory
   containing:
   - ``.fxtr_workstation.toml`` — at minimum
     ``workstation_url = "ws://host:port"`` (the URL of a running
     workstation; the active session UUID is added here later
     under ``active_session``).
   - ``preamble.py`` — re-executed in the sandbox before every
     ``run_script`` call. Use it for shared imports and helpers.
   - (optional) ``packages/`` — one **src-layout** Python project
     per subdirectory. Each ``packages/<name>/`` must contain a
     ``pyproject.toml`` and a ``src/<name>/`` package directory;
     the MCP adds ``src/`` to the sandbox's ``sys.path`` so
     ``import <name>`` resolves. Subdirs without a
     ``pyproject.toml`` are ignored. Subdirs that have a
     ``pyproject.toml`` but no ``src/`` are skipped with a warning
     printed at the top of each ``run_script`` response — fix the
     layout (or delete the pyproject) to silence it. This is the
     same layout ``mkWasmService`` builds a ``fxtr.services.<name>``
     from (see "Shipping code to a sandbox spawner" below), so a
     package you can ``import`` here is also ready to spawn.

2. ``connect()`` — open the workstation connections. Idempotent.

3. ``open_session(metadata=…)`` — create a fresh session and
   subscribe. Persists the new session UUID to the toml. If the
   toml already has ``active_session`` set, the session is
   resumed on ``connect`` and ``open_session`` errors; use
   ``close_session`` first to start a clean one.

## Running scripts

``run_script("<python source>")`` executes a sandboxed Python
script. The preamble runs first; the script's globals start
fresh per call. Top-level ``await`` is allowed. The script
can import from the working dir's ``packages/`` subdirs.

A synthetic ``_fxtr_mcp_builtin`` module is installed inside
the sandbox with these closures:

### Reads (no side effects)

- ``async lookup_entity(short_id: str) -> FxtrID`` — resolve a
  known workstation short_id to a FxtrID. Use
  ``EntityRef(fxtr_id, EntityClass).load()`` to fetch the data.
- ``async lookup_entity_by_name(external_name: str) -> FxtrID`` —
  same, but indexed by the entity's stable external_name.
- ``async lookup_service_by_name(external_name: str) -> ServiceRefStub`` —
  resolve a stable service name to a stub suitable for
  ``schedule_invoke``.
- ``def get_service(short_id: str) -> ServiceRefStub`` — wrap a
  known service short_id.
- ``def get_id_for_service(stub) -> str`` — the inverse.
- ``async retrieve_result(slot_key: str)`` — read a slot.
  ``invoke_pending`` → raises ``LookupError`` (not yet
  resolved); ``invoke_error`` → raises ``RuntimeError`` with
  the workstation-side error message; ``invoke_result`` →
  returns the unescaped payload (entity tags become loadable
  ``FxtrID``s); ``exported_entity`` / ``exported_service`` →
  returns the resolved ref; ``callback_open`` → returns a
  ``CallbackRefStub`` (pass to a later ``schedule_invoke``
  arg).
- ``async callback_notification_keys(slot_key: str) -> list[int]`` —
  list the stored notification indices for a callback slot.
- ``async retrieve_callback_notification(slot_key, notif_index)`` —
  read one stored callback payload (same unescape as
  ``retrieve_result`` for ``invoke_result``).

### Writes (queued, flushed at end-of-script)

- ``async export_entity(slot_key: str, entity)`` — pin an entity
  ref into the session under ``slot_key``.
- ``async export_service(slot_key: str, stub: ServiceRefStub)`` —
  pin a service stub into the session.
- ``def forget(slot_key: str)`` — drop a slot from the session.
  For a callback slot (``["callback_open", N]``) this also
  closes the workstation-side callback (auto-emits
  ``CloseCallbackOp``); stored notifications for that slot
  survive — call ``forget_callback_notification`` to clean
  them up.
- ``async schedule_invoke(slot_key, service_stub, action_lambda)``
  — invoke a method on a service. ``action_lambda`` receives a
  recording proxy and must ``return`` the result of exactly one
  method call on it, e.g.
  ``lambda svc: svc.echo("hello")``. Args may be scalars,
  bytes, lists, dicts, ``FxtrID``s/``EntityRef``s (the entity
  is round-tripped to the workstation), ``ServiceRefStub``s, or
  ``CallbackRefStub``s (pass a callback ref to a service so it
  can fire notifications back to you).
  The *target* may also be one of your own callback refs (from
  ``create_callback`` / ``retrieve_result`` of a callback slot)
  — e.g. subscribe the callback to an event-source service in
  the same script:
  ``feed = create_callback("feed")`` then
  ``await schedule_invoke("sub", feed, lambda i: i.subscribe_to(src))``.
  Each event the source publishes afterwards arrives like a
  callback notification.
  The result lands asynchronously in ``slot_key`` — see the
  ``<event …>`` block convention below.
- ``def create_callback(slot_key: str) -> CallbackRefStub`` —
  open a workstation-managed callback at ``slot_key``. The
  returned stub can be passed into a same-script
  ``schedule_invoke`` arg, or retrieved later via
  ``retrieve_result(slot_key)`` (returns a stub carrying the
  slot number). Whenever a service the workstation routes
  calls ``notify(payload)`` on the callback, a
  ``kind="callback"`` ``<event …>`` block fires and the payload
  is stored in session state for retrieval via
  ``retrieve_callback_notification``.
- ``def forget_callback_notification(slot_key, notif_index)`` —
  drop one stored callback notification. Useful for cleaning
  up after consumption; notifications otherwise persist until
  the session closes.
- ``async retrieve_nix_entity(name, *, nix_location="fxtr.entities") -> FxtrID``
  — build + pull a nix-declared DATA entity into the script. The
  workdir flake declares ``fxtr.entities.<name>`` (a layered-store
  entity); this evals + ``nix build``s it on demand and returns a
  bare ``FxtrID`` (wrap with ``EntityRef(fid, T).load()``). Reads
  the **live** workdir (uncommitted edits to tracked files count;
  new files need ``git add``). Raises ``NixEntityBuildFailed`` on a
  build/eval failure (full nix stderr in the message — fetch it
  with ``get_full_exception``).
- ``async current_commit() -> str`` — the workdir's git HEAD sha,
  for building ``NixWasmServiceDefn`` / preset args. **Errors
  (``WorkingTreeDirty``) if the tree is dirty** (including untracked
  files), since nix builds git flakes from the committed tree —
  commit first. See "Shipping code to a sandbox spawner" below.

All write-side ops are batched into a single workstation
mutation issued at end-of-script. If the mutation rejects, the
``run_script`` return value starts with ``Error:``.

## Async results — `<event …>` blocks

When an invoke result, callback fire, or other slot promotion
lands in the session, the MCP surfaces it as an ``<event …>``
block. The same shape covers every async server-side event.

```
<event slot_key="..." kind="invoke_result">summary</event>
```

…where ``kind`` is one of ``invoke_result`` / ``invoke_error`` /
``callback`` / ``self_commit`` (a marker emitted when one of your
own ``run_script`` mutations commits) and ``summary`` is a short
repr of the payload (or the error message). For callbacks the
block also carries a ``notif_index`` attribute identifying the
stored payload:

```
<event slot_key="..." kind="callback" notif_index="47">summary</event>
```

Where these blocks appear depends on what was happening when
the event arrived:

- **If one of your tools was running** (e.g. ``run_script``,
  ``connect``, ``wait_for_slot_notification``) the events land
  inline in that tool's response, *after* the primary result.
  This is the common case for ``run_script``: any invoke
  results that complete during the tool's trailing-delay window
  appear at the end of the tool output, along with a
  ``kind="self_commit"`` marker for your script's own mutation
  so you can see the chronological ordering of foreign events
  vs your commit.
- **If you were idle** the events arrive as a single
  ``notifications/claude/channel`` frame containing one or more
  ``<event …>`` blocks:

  ```
  <channel source="workstation_mcp" kind="events">
  <event slot_key="..." kind="invoke_result">...</event>
  <event slot_key="..." kind="callback" notif_index="47">...</event>
  </channel>
  ```

When you see a notification for a slot you care about, issue a
follow-up ``run_script`` that calls ``retrieve_result(slot_key)``
or ``retrieve_callback_notification(slot_key, notif_index)`` to
fetch the actual payload. ``notif_index`` is the originating
event's ``seq`` — stable per session, so safe to reuse across
calls.

### Waiting inline for a specific slot

``wait_for_slot_notification(slot_key, timeout_seconds)`` blocks
until the named slot receives an event or the timeout elapses.
While waiting, any notifications that arrive (for this slot or
others) buffer and come back in the tool's response. Useful when
you've just scheduled an invoke or created a callback and want
to wait inline rather than poll between drip-fed updates.

The slot is expected to be in a non-terminal state
(``invoke_pending`` / ``callback_open``) at call time. Terminal
slots return success immediately with a hint pointing at
``retrieve_result``; absent slots error.

## Closing

``close_session()`` closes the session via a single-op
``CloseOp`` mutation and clears ``active_session`` in the toml.

## Tips

- Put repeated helper imports / domain types in ``preamble.py``
  so your script bodies stay short.
- For multi-step flows, define helper async functions in your
  ``packages/`` and call them from ``run_script`` one-liners.
  The ``examples/basic_test_demo/agent_workdir/`` example
  follows this pattern.
- ``retrieve_result`` raises ``LookupError`` if the slot is
  still ``invoke_pending`` — always wait for the ``<event …>``
  notification before retrieving rather than polling. Use
  ``wait_for_slot_notification`` if you want to block inline.

## Shipping code to a sandbox spawner

If a ``SandboxSpawnerProto`` service is registered on the
workstation (look up by name via ``lookup_service_by_name``), you
spawn a sandboxed service **by git reference**: declare it in your
workdir's ``flake.nix``, commit, and hand the spawner a
``NixWasmServiceDefn``. The spawner builds your code itself from
the pinned commit — nothing is uploaded. The flow:

1. **Write your service package** at
   ``<workdir>/packages/<name>/`` using **src layout**:

   ```
   packages/my_service/
     pyproject.toml
     src/
       my_service/                     ← importable package dir
         __init__.py                   ← defines `async def run(config)`
   ```

   The workstation MCP's sandbox bridge adds
   ``packages/<name>/src/`` to the sandbox's ``sys.path``, and
   the flake's ``mkWasmService`` builds the same dir into a guest
   site-packages — so one layout works for both ``import``-ing the
   package from ``run_script`` *and* building it for the spawner.

   - Use a **static version** in pyproject (build hooks don't run
     in the guest build).
   - Only pure-Python files end up in the guest (no native
     extensions / binary data).

2. **Declare deps in pyproject.** A typical sandboxed service
   needs ``fxtr-sandbox-rpc-adapter[fxtr2-portable]`` to expose a
   service via fxtr's RPC bridge. Mirror the bundled
   ``packages/hello_service/`` example for the
   ``[tool.uv.sources]`` + entry-point shape:

   ```toml
   [project]
   name = "my-service"
   version = "0.0.0"
   requires-python = ">=3.12"
   dependencies = ["fxtr-sandbox-rpc-adapter[fxtr2-portable]"]
   [project.entry-points."fxtr.service"]
   service = "my_service:run"
   ```

3. **Write your entry point.** ``my_service:run`` must be an
   ``async def run(config) -> ServiceRef[YourProto]``. Inside, you
   mount your impl on ``get_active_scope()`` and return the ref.
   See ``packages/hello_service/`` (this workdir) or
   ``projects/fxtr_sandboxing/examples/rpc_task/`` for the shape.

4. **Declare it in ``flake.nix``** as a CODE slice:

   ```nix
   fxtr.services.my_service = fxtr.lib.fxtr.mkWasmService {
     src = ./packages/my_service;
   };
   ```

5. **Commit, then spawn by reference.** The spawner builds from
   the committed tree, so commit first (and ``git push`` if the
   spawner is remote). ``current_commit()`` pins HEAD and **errors
   on a dirty tree**, so it can't silently ship a stale commit:

   ```python
   defn = NixWasmServiceDefn(
       git_commit=current_commit(),
       flake_attr="fxtr.services.my_service",
   ).store()
   spawner = await lookup_service_by_name("the-spawner")
   await schedule_invoke("running", spawner, lambda s: s.spawn(defn))
   ```

   Wait for the ``running`` slot to land an ``invoke_result``
   event, then ``retrieve_result("running")`` to get a stub you
   can ``connect(config)`` and invoke.

The first spawn of a given commit cold-builds the code via nix
(seconds to minutes); rebuilds of the same commit are cached.
Trust is connection-level — the workstation builds the
git-referenced code of any agent authorized to connect, the same
as it already runs your ``run_script`` code.
