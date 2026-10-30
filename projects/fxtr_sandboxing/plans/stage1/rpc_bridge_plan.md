# RPC Bridge Plan

## Goal

Layer fxtr2's RPC system on top of the existing sandbox execution infrastructure, so that
sandboxed tasks communicate with the host via `FxtrRPCHandle` calls rather than raw string
messages. This enables structured, capability-passing communication — including entity exchange
— between the host and sandboxed code.

## Design overview

### Architecture (bottom-up)

Both host and guest build the same stack of adapters:

```
eryx callbacks (JSON objects, blocking)
  → ObjectStream[JSONTypes] adapter (async, anyio)
    → JSON transport (SimpleJSONTransportContext or JSONTransportContext)
      → ObjectStream[IRValue[JSONLeaf]]  (via simple_json_transport_stream / json_transport_stream)
        → RemoteFxtrRPCContext  (send/receive FxtrRPCValue, scope-aware entity exchange)
```

The host drives the bottom of this stack by bridging the blocking eryx callback queues into
an async `ObjectStream[JSONTypes]`. The guest drives its end by wrapping the eryx callbacks
(which appear as globals in the sandbox) into the same stack.

### Stream protocol

Once both sides have a `RemoteFxtrRPCContext`, the convention is:

1. **Host sends a config message** — a dict with transport names and any other setup info.
   The host may include `FxtrRPCHandle`s in this config if it wants to expose capabilities
   to the guest (e.g., a handle for spawning sub-sandboxes in the future).
2. **Guest sends its main handle** — an `FxtrRPCHandle` wrapping the guest's service object.
3. **Host dispatches on the handle** — standard fxtr RPC calls, with entity exchange via the
   bridge's arena mechanism.

The guest stays alive (blocking on `rpc.receive()` or a done signal) until the host closes
the stream or the context exits.

### Transport selection

Both `SimpleJSONTransportContext` and `JSONTransportContext` are supported, selected by a
parameter at launch time. The guest-side code is identical either way (it just instantiates
the chosen context). Default to the simple transport initially; the full transport adds
pipelining but also more complexity and potential bugs.

### Low-level API changes

The existing `runner.py` is updated so that:

- **Callbacks pass JSON objects** instead of strings. Eryx callbacks natively support full
  JSON types (`dict`, `list`, `int`, `float`, `bool`, `None`), so no encoding overhead.
- **`TaskSession`** is updated to send/receive JSON objects (not strings). This is a breaking
  change to the existing API, but the existing tests can be updated. JSON is strictly more
  capable.
- The low-level API remains available for callers who don't want the RPC layer.

### High-level API (new module)

A new module (e.g., `rpc_runner.py`) wraps the low-level API:

```python
# Host side
async with run_rpc(bundle, cache_dir, transports=..., config={...}) as handle:
    # handle is an FxtrRPCHandle wrapping the guest's exposed object
    result = await handle.dispatch_and_wait(["process"], FxtrCall([data]))
```

Internally, `run_rpc()`:
1. Calls the low-level `run()` to get a `TaskSession` (JSON object stream)
2. Wraps the session as `ObjectStream[JSONTypes]`
3. Opens a JSON transport context over it
4. Opens `simple_json_transport_stream` / `json_transport_stream` to get `ObjectStream[IRValue]`
5. Opens `RemoteFxtrRPCContext` over that
6. Sends the config message
7. Receives the guest's main handle
8. Yields the handle

### Guest-side helper package: `fxtr_sandbox_rpc_adapter`

A new small package (lives in `projects/fxtr_sandboxing/fxtr_sandbox_rpc_adapter/`) that the
guest task depends on. It depends on `fxtr2` and provides the bridge helpers.

The guest API:

```python
# In the guest's entry point
from fxtr_sandbox_rpc_adapter import upgrade

async def run(ctx):
    async with upgrade(ctx) as rpc:
        config = await rpc.receive()   # host's config (with transport names, etc.)
        # Set up transports from config if needed
        my_service = MyService(...)
        # Wrap and send main handle
        handle = rpc.context.wrap(my_service)  # LocalFxtrRPCContext.wrap()
        await rpc.send(handle)
        # Block until host is done
        await rpc.wait_until_done()
```

`upgrade(ctx)` does the guest-side equivalent of what `run_rpc()` does on the host:
wraps the raw callbacks → ObjectStream → JSON transport → FxtrRPC stream.

### Network transports

Transports remain a separate mechanism (not tunneled through RPC). The host configures
`TransportRoute`s and registers the eryx callbacks as before. The host sends transport
*names* in the config object; the guest uses those names with `ctx.get_transport(name)` /
`ctx.get_client(name)` (existing mechanism, unchanged).

### fxtr2 inside the sandbox

The guest needs a working fxtr2 installation inside the sandbox. This means:

- The user's task must explicitly depend on fxtr2 (via pyproject.toml)
- fxtr2 and its dependencies (mashumaro, cbor2, etc.) must be importable in eryx/WASM
- If some fxtr2 dependencies can't run in WASM, we may need to split fxtr2 into a portable
  core (serialization, entities, scopes, RPC) and non-portable extras (SQLite, git, etc.)

This is the biggest risk and is addressed in Phase 1.

## Derisking plan (Phase 1)

Before implementing the full bridge, validate that the critical dependencies work inside
eryx. Each step builds on the previous one. Work in `examples/rpc_bridge_derisking/`.

### Step 1: JSON callbacks — DONE

**Verified:** Eryx callbacks can pass dicts, lists, nested JSON — not just strings.

Callbacks use `serde_json::Value` ↔ `pythonize` and support full JSON round-trips.
See `examples/rpc_bridge_derisking/step1_json_callbacks.py`.

### Step 2: anyio task groups in eryx — DONE

**Verified:** `anyio.create_task_group()` works inside the sandbox, including concurrent
tasks, memory object streams, and cancellation with real delays.

Eryx's minimal event loop needed patching via two shims:

- **Socket shim** (existing): patches `AF_UNSPEC`, `AddressFamily`, `SocketKind` into
  eryx's `socket` module for anyio import compatibility.
- **Loop shim** (new): patches `get_task_factory`, `call_soon`, `time`, `call_later`,
  `call_at` on eryx's `_EryxLoop`.

The `call_later`/`call_at` implementation uses a host-side sleep callback
(`_host_sleep`) backed by `threading.Event.wait()` for interruptibility. A cleanup
protocol (`_host_cleanup_timers` + `_cleanup_timers`) ensures all in-flight sleep
subtasks are resolved before the sandbox exits, avoiding the WASM "resource has
children" error on `waitable_set_drop`.

See `examples/rpc_bridge_derisking/step2_anyio_task_groups.py` and
`examples/rpc_bridge_derisking/anyio_findings.md` for full details.

### Step 3: fxtr2 importable in eryx — DONE

**Verified:** All 15 fxtr2 modules needed for the RPC bridge import and work inside eryx.
Entity store/load with scopes, content-addressing, child/ref relationships, and scope
lifetime cleanup all work correctly. No changes to fxtr2 source were needed.

Key findings:
- cbor2's C extension must be removed (falls back to pure Python)
- A portable fxtr2 build exists at `v2/portable/pyproject.toml` (build-time split of
  the core RPC, data, and conversion modules with only 4 deps: anyio, cbor2, frozendict,
  mashumaro)

See `examples/rpc_bridge_derisking/step3_fxtr2_imports.py` and
`examples/rpc_bridge_derisking/fxtr2_import_findings.md` for full details.

### Step 4: End-to-end JSON transport in eryx

**Verify:** A `SimpleJSONTransportContext` can run inside eryx, exchanging messages with
the host through eryx callbacks.

This tests the full async stack: callbacks → ObjectStream → JSON transport → handle
dispatch. Write a minimal test where the host sends a call to the guest's handle and
gets a response back, all through the transport running inside eryx.

### Step 5: Full fxtr RPC bridge in eryx

**Verify:** `RemoteFxtrRPCContext` works inside eryx with entity exchange.

Extend step 4 to use the fxtr bridge layer. Test that the guest can receive `FxtrID`s
from the host, load entity data, and send `FxtrID`s back.

## Implementation plan (Phase 2)

_To be detailed after derisking._ High-level outline:

- [ ] Update low-level API: change callbacks and `TaskSession` to use JSON objects
- [ ] Implement host-side `ObjectStream[JSONTypes]` adapter over `TaskSession`
- [ ] Create `fxtr_sandbox_rpc_adapter` package with `upgrade()` helper
- [ ] Implement `rpc_runner.py` with `run_rpc()` on the host side
- [ ] Update `_runner.py` to support the RPC bridge entry point
- [ ] Tests for the full stack
- [ ] Update CODE_OVERVIEW.md

## Future work

- **`run` as a handle**: Turn the sandbox launcher itself into an `FxtrRPCHandle`, so that
  a sandbox can receive the capability to spawn sub-sandboxes. The sub-sandboxes live on the
  host but are bridged via RPC, so the first guest just sees nested capability objects.
- **Docker RPC bridge**: Layer `fxtr_sandbox_rpc_adapter` with `fxtr_task_runner` to bring
  RPC bridge support to Docker-based tasks too.
