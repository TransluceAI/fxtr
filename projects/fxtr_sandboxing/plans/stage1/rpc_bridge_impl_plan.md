# RPC Bridge Implementation Plan

Implements the RPC bridge described in `rpc_bridge_plan.md`. Two phases: (1) switch the
low-level messaging from strings to JSON objects, (2) layer the fxtr2 RPC bridge on top.

## Phase 1: JSON Object Messaging

Switch both WASM and Docker runtimes from string messages to JSON objects. This is a
prerequisite for the RPC bridge (the JSON transport sends dicts/lists, not strings).

### Shutdown sentinel

Introduce a `_ShutdownSentinel` type to replace the current bare `object()` sentinel. This
gives us cleaner type annotations:

```python
class _ShutdownSentinel:
    """Sentinel value used to unblock queues during shutdown."""
    pass

_SHUTDOWN = _ShutdownSentinel()
```

Then queue types become `queue.Queue[JSONTypes | _ShutdownSentinel]` instead of the current
`queue.Queue[str | object]`.

### JSON type alias

Define `JSONTypes = None | bool | int | float | str | list[Any] | dict[str, Any]` for use
in the messaging APIs.

### 1a. WASM runtime (`runner.py` + `_runner.py`)

Eryx callbacks already support full JSON natively (`serde_json::Value` ↔ `pythonize`, proven
in derisking step 1). The current code just happens to type-annotate them as `str`.

**`runner.py` changes:**
- Add `_ShutdownSentinel`, `_SHUTDOWN`, and `JSONTypes` definitions
- Queue types: `queue.Queue[JSONTypes | _ShutdownSentinel]`
- Callback signatures: `_receive_from_host() -> object`, `_send_to_host(data: object) -> None`
- `TaskSession.send(data: JSONTypes)`, `TaskSession.receive() -> JSONTypes`
- Check for `_ShutdownSentinel` via `isinstance` instead of `is _SHUTDOWN`

**`_runner.py` changes:**
- `TaskContext.send(data: object)`, `TaskContext.receive() -> object`

No serialization needed — eryx passes JSON objects through callbacks directly.

### 1b. Docker runtime (`container_runner.py` + `fxtr_task_runner/`)

The Docker side uses length-prefixed UTF-8 over a Unix socket. Currently sends raw strings.
Change to serialize JSON objects.

**`fxtr_task_runner/protocol.py`:**
- `send_message(sock, message: object)` — `json.dumps(message)` → UTF-8 → length-prefix → send
- `receive_message(sock) -> object` — recv → length-prefix → UTF-8 → `json.loads`

This is a one-line change in each function (add `json.dumps`/`json.loads`).

**`fxtr_task_runner/context.py`:**
- `TaskContext.send(data: object)`, `TaskContext.receive() -> object`

**`container_runner.py`:**
- Add `_ShutdownSentinel` and `JSONTypes` (or import from a shared location)
- `TaskSession.send(data: JSONTypes)`, `TaskSession.receive() -> JSONTypes`

### 1c. Update tests and examples

- `test_runner.py`: send dicts directly instead of JSON-encoded strings
- `test_container_runner.py`: same
- Example tasks: remove manual `json.loads`/`json.dumps` where applicable

### Implementation order

- [x] Update `runner.py`: `_ShutdownSentinel`, `JSONTypes`, callbacks + queues + TaskSession
- [x] Update `_runner.py` TaskContext (type annotations)
- [x] Update `fxtr_task_runner/protocol.py` (add json.dumps/loads)
- [x] Update `fxtr_task_runner/context.py` (type annotations)
- [x] Update `container_runner.py` TaskSession
- [x] Update `test_runner.py`
- [x] Update `test_container_runner.py`
- [x] Update example tasks (hello_task, yaml_task, http_task, lib_task, container_task)
- [x] Update `test_transport.py` (eryx e2e tests)
- [x] Update `test_protocol.py` (JSON round-trip tests)

---

## Phase 2: RPC Bridge

### Architecture

Both host and guest build the same adapter stack:

```
JSON object messaging (Phase 1)
  → ObjectStream[JSONTypes] adapter
    → SimpleJSONTransportContext (via simple_json_transport_stream)
      → ObjectStream[IRValue[JSONLeaf]]
        → RemoteFxtrRPCContext (send/receive FxtrRPCValue with entity exchange)
```

The host drives the bottom via `TaskSession` (async, backed by queues or sockets). The guest
drives its end via `TaskContext` (async callbacks in WASM, async socket ops in Docker).

### Stream protocol

Once both sides have a `RemoteFxtrRPCContext`:

1. **Host sends config** — a dict with transport names and any setup info. May include
   `FxtrRPCHandle`s for capabilities the host wants to expose.
2. **Guest sends its main handle** — an `FxtrRPCHandle` wrapping the guest's service object.
3. **Host dispatches on the handle** — standard fxtr RPC calls with entity exchange.

### Guest shutdown

When the host is done, `run_rpc()` explicitly calls `send_eof()` on the IR object stream
before tearing down the context stack. This propagates as `EndOfStream` to the guest's
`RemoteFxtrRPCContext.receive()`, giving the guest a clean shutdown signal (not a broken
resource error). The guest detects this and exits its entry point; the `upgrade()` context
manager handles cleanup.

### ObjectStream adapter

A thin adapter that wraps any object with async `send(object)` / `receive() -> object` into
an `anyio.abc.ObjectStream`. Used by both the host and guest sides.

```python
class _JSONObjectStream:
    """Wraps raw JSON send/receive into an anyio ObjectStream."""
    def __init__(self, sender, receiver):
        self._send = sender    # async callable: object -> None
        self._receive = receiver  # async callable: () -> object
    async def send(self, item): await self._send(item)
    async def receive(self): return await self._receive()
    async def aclose(self): pass
    async def send_eof(self): pass  # transport handles shutdown
```

This lives in `fxtr_sandbox_rpc_adapter` and is used on the guest side. The host side uses
the same pattern but constructs it from the `TaskSession`.

### `fxtr_sandbox_rpc_adapter` package

New package at `projects/fxtr_sandboxing/fxtr_sandbox_rpc_adapter/`. Depends on `fxtr2`
(portable). Provides the guest-side RPC bridge for both WASM and Docker tasks.

**Public API:**

```python
@asynccontextmanager
async def upgrade(ctx) -> AsyncGenerator[SandboxRPCSession]:
    """Upgrade a raw TaskContext into an RPC session.

    Works for both WASM (eryx) and Docker task contexts — any object with
    async send(object) and receive() -> object methods.
    """
    stream = _JSONObjectStream(ctx.send, ctx.receive)
    async with simple_json_transport_stream(stream) as ir_stream:
        async with RemoteFxtrRPCContext(ir_stream) as remote_ctx:
            async with LocalFxtrRPCContext() as local_ctx:
                yield SandboxRPCSession(remote_ctx, local_ctx)
```

**`SandboxRPCSession`:**

```python
class SandboxRPCSession:
    """Combined RPC session for guest-side code."""
    def __init__(self, remote, local):
        self._remote = remote
        self._local = local

    async def send(self, value): await self._remote.send(value)
    async def receive(self): return await self._remote.receive()

    def wrap(self, obj) -> FxtrRPCHandle:
        """Wrap a local Python object as an FxtrRPCHandle for the host."""
        return self._local.wrap(obj)
```

**Package structure:**

```
fxtr_sandbox_rpc_adapter/
  pyproject.toml          # depends on fxtr2-portable
  fxtr_sandbox_rpc_adapter/
    __init__.py           # exports upgrade, SandboxRPCSession
    _stream_adapter.py    # _JSONObjectStream
```

**pyproject.toml:**

```toml
[project]
name = "fxtr-sandbox-rpc-adapter"
dependencies = [
    "fxtr2-portable @ https://fxtr.transluce.org/unreachable/..."
]

[tool.uv.sources]
fxtr2-portable = { path = "../../v2/portable" }
```

### Host-side: `rpc_runner.py`

New module in `fxtr_sandboxing/`. Wraps the low-level `run()` with the RPC stack.

```python
@asynccontextmanager
async def run_rpc(
    bundle: TaskBundle,
    cache_dir: Path,
    *,
    transports: list[TransportRoute] | None = None,
    config: dict | None = None,
) -> AsyncIterator[RPCSession]:
    """Run a TaskBundle with RPC bridge.

    Yields an RPCSession with the guest's main FxtrRPCHandle.
    """
    async with run(bundle, cache_dir, transports=transports) as session:
        # Wrap TaskSession as ObjectStream
        stream = _JSONObjectStream(session.send, session.receive)
        async with simple_json_transport_stream(stream) as ir_stream:
            async with RemoteFxtrRPCContext(ir_stream) as remote_ctx:
                async with LocalFxtrRPCContext() as local_ctx:
                    # Send config
                    await remote_ctx.send(config or {})
                    # Receive guest's main handle
                    main_handle = await remote_ctx.receive()
                    try:
                        yield RPCSession(main_handle, remote_ctx, local_ctx)
                    finally:
                        # Signal clean shutdown to guest before tearing down
                        await ir_stream.send_eof()
```

**`RPCSession`:** Exposes the guest's main handle plus send/receive for additional
communication.

A similar `run_container_rpc()` wraps `run_container()` with the same stack.

### Loop shim for WASM

The JSON transport uses anyio task groups, and `simple_json_transport_stream` needs
`call_later`/`call_at` for timeout handling. The existing `_SOCKET_SHIM` in `runner.py` is
not enough — we need the full loop shim from derisking step 2 (with `_host_sleep` callbacks).

**`runner.py` changes:**
- Add `_LOOP_SHIM` constant (from step 2) alongside existing `_SOCKET_SHIM`
- Register `_host_sleep` and `_host_cleanup_timers` callbacks
- Track `active_sleep_events` (dict of `threading.Event`)
- Inject `_SOCKET_SHIM + _LOOP_SHIM` before runner code
- Add cleanup: call `_host_cleanup_timers` before sandbox exit

The loop shim patches eryx's `_EryxLoop` with:
- `get_task_factory() → None`
- `call_soon()` with context fallback
- `time()` → `time.monotonic()`
- `call_later()` / `call_at()` via `_host_sleep` callback (interruptible `threading.Event.wait`)
- `_cleanup_timers()` for clean shutdown

### Packaging changes

**`prepare.py` — accept non-editable path deps:**

Currently line 110-114 rejects non-editable directory dependencies. Change to build them
the same way as editable ones (build wheel → unpack → `PySourcePackage`).

```python
if pkg.directory is not None:
    # Local package (editable or path dep)
    pkg_path = (task_dir / pkg.directory.path).resolve()
    source_pkg = _build_source_package(pkg_path)
    packages[name] = source_pkg.store()
```

**cbor2 pure-python:**

cbor2 publishes a `py3-none-any` wheel on PyPI alongside the platform-specific C-extension
wheels. The pylock.toml output from `uv export` includes all available wheels. The existing
`_find_pure_python_wheel()` function in `prepare.py` already selects the `*-none-any.whl`
entry. No special configuration or `no-binary-package` setting is needed — it just works.

### `_runner.py` changes

With the RPC bridge as a separate package (`fxtr_sandbox_rpc_adapter`), `_runner.py` stays
mostly the same. The main changes:

- Send/receive JSON objects instead of strings (Phase 1)
- Loop shim is injected before the runner code (by `runner.py`)

The task code itself handles the upgrade:

```python
# In the task's entry point
from fxtr_sandbox_rpc_adapter import upgrade

async def run(ctx):
    async with upgrade(ctx) as rpc:
        config = await rpc.receive()
        my_service = MyService()
        await rpc.send(rpc.wrap(my_service))
        # Block until host closes — clean exit on stream end
        try:
            await rpc.receive()
        except EndOfStream:
            pass
```

### Example RPC task

Create `examples/rpc_task/` as a minimal example:

```
examples/rpc_task/
  pyproject.toml     # depends on fxtr-sandbox-rpc-adapter
  rpc_task/
    __init__.py      # entry point using upgrade()
```

The task exposes a simple service (e.g., `echo(text)` or `process(text)` returning analysis),
and the host dispatches on it via `run_rpc()`.

### Implementation order

- [x] Create `fxtr_sandbox_rpc_adapter/` package skeleton
- [x] Implement `_JSONObjectStream` adapter (needs `__aiter__`/`__anext__` since the
  transport uses `async for` on the stream)
- [x] Implement `upgrade()` and `SandboxRPCSession`
- [x] Update `runner.py`: add loop shim + host sleep callbacks
- [x] Update `prepare.py`: accept non-editable path deps
- [x] Implement `rpc_runner.py` with unified `run_rpc()` dispatching on bundle type
  (`TaskBundle` -> `run()`, `DockerTaskBundle` -> `run_container()`)
- [x] Create example RPC task (`examples/rpc_task/`)
- [x] Change `v2/portable/pyproject.toml` to pull mashumaro from an index (not git)
  — prepare.py doesn't support VCS deps, and the index has a py3-none-any wheel
- [x] Write end-to-end RPC tests (echo, reverse, add — all passing through eryx)
- [x] Create `examples/container_rpc_task/` (Dockerfile, rebuild.sh, setup.sh,
  config, `task_src/my_rpc_task`). Dockerfile installs prebuilt wheels for
  `fxtr2-portable`, `fxtr-sandbox-rpc-adapter`, and `fxtr-task-runner`.
- [x] Update `rebuild.sh` to prebuild wheels on the host via `uv build --wheel`
  into `.wheels/` (gitignored) and pass that as a named buildkit context. The
  portable package uses hatchling `force-include` paths that reach outside its
  own directory, so it can't be built inside the container.
- [x] Fix `fxtr_task_runner/context.py` to use non-blocking socket I/O
  (`loop.sock_recv` / `loop.sock_sendall`). The old blocking `send_message` /
  `receive_message` calls deadlocked the RPC transport's read loop against
  user code.
- [x] Write container-side RPC tests (`test_container_rpc_runner.py`) — all 5
  pass end-to-end (echo, reverse, add, platform_info, multiple_calls).
- [x] Update `CODE_OVERVIEW.md`

---

## Open questions

1. **Transport selection**: Default to `SimpleJSONTransportContext`. The full
   `JSONTransportContext` adds pipelining but more complexity. Can revisit later.
