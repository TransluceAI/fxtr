# RPC Bridge Implementation — Scratch Notes

## What we're building

Two phases:
1. Switch low-level APIs from string to JSON object messaging
2. Layer the fxtr2 RPC bridge on top, with a new `fxtr_sandbox_rpc_adapter` package

## Phase 1: JSON object messaging

### Current state

Both WASM and Docker runtimes use **string** messages:
- `runner.py`: `TaskSession.send(data: str)`, `receive() -> str`
- `container_runner.py`: `TaskSession.send(data: str)`, `receive() -> str`
- `_runner.py` (WASM guest): `TaskContext.send(data: str)`, `receive() -> str`
- `fxtr_task_runner/context.py` (Docker guest): `TaskContext.send(data: str)`, `receive() -> str`

### What needs to change

**WASM side** (`runner.py` + `_runner.py`):
- Eryx callbacks already support JSON natively (Step 1 proved this). The callbacks currently
  have `str` type annotations but eryx uses `serde_json::Value` ↔ `pythonize` internally.
- Change callback signatures: `_receive_from_host() -> object`, `_send_to_host(data: object)`
- Change queue types: `queue.Queue[object]` instead of `queue.Queue[str | object]`
- Change `TaskSession.send/receive` types accordingly
- Change `_runner.py` TaskContext types accordingly

**Docker side** (`container_runner.py` + `fxtr_task_runner/`):
- The protocol.py uses UTF-8 string framing. For JSON objects, we need to serialize.
- Simplest change: `send_message` takes an object, does `json.dumps` → UTF-8 → length-prefix.
  `receive_message` does the reverse: length-prefix → UTF-8 → `json.loads`.
- Actually, could also use a different framing. But JSON over length-prefixed strings is fine
  and backwards-compatible in spirit.
- Update `fxtr_task_runner/protocol.py`, `context.py`, `container_runner.py`

**Test updates**:
- `test_runner.py` sends strings like `'{"hello": "world"}'` → now sends dicts directly
- `test_container_runner.py` similar

### Type for JSON objects

Use `JSONTypes = None | bool | int | float | str | list | dict` or similar.
Actually, the fxtr2 RPC already defines `JSONTypes` somewhere — let me think about whether
to reuse that or keep it simple. For the low-level API, `object` is probably fine as the
annotation, or we define a type alias. Let's keep it simple and use `object`.

Actually in the fxtr2 rpc code, `JSONTypes` is defined in json_transport.py or similar.
For fxtr_sandboxing, we can define our own simple alias or just use `object`.

## Phase 2: RPC bridge

### Architecture reminder (from plan)

Both host and guest build:
```
JSON callbacks/socket (JSON objects, blocking on host thread / async in sandbox)
  → ObjectStream[JSONTypes] adapter
    → SimpleJSONTransportContext
      → ObjectStream[IRValue[JSONLeaf]] (via simple_json_transport_stream)
        → RemoteFxtrRPCContext (send/receive FxtrRPCValue)
```

### ObjectStream[JSONTypes] adapter

This is the key new piece. Need to wrap the low-level JSON messaging into an anyio ObjectStream.

**WASM guest side**: The callbacks are async in the sandbox. So we need:
```python
class _CallbackObjectStream:
    async def send(self, item):
        await _send_to_host(data=item)
    async def receive(self):
        return await _receive_from_host()
    async def aclose(self):
        pass  # or send a sentinel?
```

**Docker guest side**: The socket is sync. Need async wrappers.
```python
class _SocketObjectStream:
    async def send(self, item):
        send_json_message(self._sock, item)  # sync is fine in container
    async def receive(self):
        return receive_json_message(self._sock)
    async def aclose(self):
        pass
```

**Host side (WASM)**: The queues are blocking. TaskSession wraps with anyio.to_thread.
```python
class _TaskSessionObjectStream:
    async def send(self, item):
        await session.send(item)  # already async via to_thread
    async def receive(self):
        return await session.receive()  # already async
    async def aclose(self):
        pass
```

**Host side (Docker)**: Same pattern, TaskSession already wraps sync socket with anyio.

So the ObjectStream adapter is thin — it's just a class satisfying the ObjectStream protocol
on top of whatever send/receive we already have.

### fxtr_sandbox_rpc_adapter package

Lives in `projects/fxtr_sandboxing/fxtr_sandbox_rpc_adapter/`. Depends on fxtr2 (portable).
Works for both WASM and Docker.

The key function: `upgrade(ctx) -> AsyncContextManager[RemoteFxtrRPCContext]`

But wait — the ctx types are different for WASM vs Docker. In WASM, it's the `TaskContext`
from `_runner.py` (defined inline). In Docker, it's `fxtr_task_runner.context.TaskContext`.

Both have `.send(data)` and `.receive()` with the same async signatures (after Phase 1,
both send/receive JSON objects). So `upgrade()` can accept any object with async
`send(object)` and `receive() -> object` methods — duck typing.

Actually, for the WASM case, the adapter package is imported from site_packages. But the
TaskContext is defined in `_runner.py` which is injected code, not an importable module.
So the adapter would receive the ctx as an argument with a protocol/duck-typed interface.

```python
# In fxtr_sandbox_rpc_adapter:
class RawMessageStream(Protocol):
    async def send(self, data: object) -> None: ...
    async def receive(self) -> object: ...

@asynccontextmanager
async def upgrade(ctx: RawMessageStream) -> AsyncGenerator[RemoteFxtrRPCContext]:
    # Wrap ctx as ObjectStream[JSONTypes]
    stream = _ObjectStreamAdapter(ctx)
    # Open JSON transport
    async with simple_json_transport_stream(stream) as ir_stream:
        # Open fxtr RPC context
        async with RemoteFxtrRPCContext(ir_stream) as rpc:
            yield rpc
```

Wait, but I need to think about what the guest does vs what the host does.

**Guest side** (inside sandbox):
- Has a `TaskContext` with send/receive
- Calls `upgrade(ctx)` to get a `RemoteFxtrRPCContext`
- Receives config from host
- Wraps its service object and sends handle to host
- Waits for host to finish

But the guest needs `LocalFxtrRPCContext` to wrap its own objects! `RemoteFxtrRPCContext`
is for the stream. The guest needs both: RemoteFxtrRPCContext for the stream, and
LocalFxtrRPCContext to wrap local Python objects as handles that get sent over the stream.

Actually wait, let me re-read how RemoteFxtrRPCContext works. Looking at fxtr_rpc_stream.py:
- It takes an `ObjectStream[IRValue[JSONLeaf]]`
- `send()` encodes FxtrRPCValue → IRValue and sends
- `receive()` receives IRValue and decodes → FxtrRPCValue

And handles within those values are already wrapped — when you receive an IRValue containing
an RPCHandle (from the transport), decode_to_fxtr_value wraps it as FxtrRPCHandle.

When you want to send a handle to the remote side, you create it locally (via
LocalFxtrRPCContext.wrap()) and send it through the stream. The transport handles the
export/import.

So the guest needs:
1. `RemoteFxtrRPCContext` wrapping the stream (for send/receive)
2. A way to create local handles — but this is done inside the RemoteFxtrRPCContext? No...

Actually looking more carefully: RemoteFxtrRPCContext extends _FxtrRPCContextBase which
extends LocalCapContext. And it has its own scope. But it doesn't have a `wrap()` method
like LocalFxtrRPCContext does.

Hmm. Let me think about this differently. The JSON transport already handles capability
passing. When the guest creates a local handle (via LocalRPCContext.wrap() at the Cap-IR
level) and sends it through the stream, the transport exports it. The remote side gets an
import handle.

But for fxtr-level entity exchange, we need FxtrDispatchImpl, which lives in
LocalFxtrRPCContext. The guest needs a LocalFxtrRPCContext to wrap objects for fxtr dispatch.

So the pattern would be:
1. Guest opens the transport stack to get `ObjectStream[IRValue[JSONLeaf]]`
2. Guest opens `RemoteFxtrRPCContext` wrapping that stream — for send/receive with entity support
3. Guest opens `LocalFxtrRPCContext` — for wrapping local objects as fxtr handles
4. Guest wraps its service via `local_ctx.wrap(my_service)`
5. Guest sends the handle via `remote_ctx.send(handle)`
6. Host receives the handle and dispatches on it

Wait, but can you send a handle created from one context through another context's stream?
That seems like it could be problematic...

Let me re-read how the test_fxtr_rpc_stream.py works.

Actually I think the way it works is simpler than I'm making it. Looking at the
object_stream.py layer: both sides have a SimpleJSONTransportContext. The transport context
takes a `main_interface` handle — that's the local object you're exporting. The remote side
gets `ctx.remote_main` which is a handle to your main interface.

So for the RPC bridge:
- Host creates a local handle (via LocalFxtrRPCContext.wrap()) and passes it as main_interface
  to the transport context. Or maybe the host's main interface is more minimal.
- Guest does the same on its end.

Actually, looking at object_stream.py more carefully:
- Both sides export a `_StreamMainInterface` with `send()` and `send_eof()` methods
- This creates a bidirectional stream of IRValues
- Then RemoteFxtrRPCContext wraps that stream

So the full stack for the guest:
```
callbacks → ObjectStream[JSONTypes] → simple_json_transport_stream() →
    ObjectStream[IRValue] → RemoteFxtrRPCContext → send/receive FxtrRPCValue
```

And handles inside FxtrRPCValues are transported by the underlying JSON transport. The
RemoteFxtrRPCContext doesn't need to create handles — it just passes them through. The
handles are created by LocalFxtrRPCContext on whichever side wants to export objects.

But wait — RemoteFxtrRPCContext extends LocalCapContext. And looking at the test file
test_fxtr_rpc_stream.py, does the guest use a LocalFxtrRPCContext?

Hmm, I think the answer is: the guest creates a LocalFxtrRPCContext which provides the
`wrap()` method. It wraps objects as FxtrRPCHandles. Then those handles are sent through
the RemoteFxtrRPCContext (which is just a stream wrapper). The transport underneath
handles the capability passing.

So the guest upgrade() returns something that gives access to both the stream
(for send/receive) and a local context (for wrapping). Let me look at the plan again.

From the plan:
```python
async with upgrade(ctx) as rpc:
    config = await rpc.receive()   # host's config
    my_service = MyService(...)
    handle = rpc.context.wrap(my_service)  # LocalFxtrRPCContext.wrap()
    await rpc.send(handle)
    await rpc.wait_until_done()
```

So `rpc` is some combined object that has:
- `.receive()` / `.send()` from RemoteFxtrRPCContext
- `.context` which is a LocalFxtrRPCContext for wrapping

This makes sense. The upgrade() function sets up both contexts.

### Host side: rpc_runner.py

```python
async with run_rpc(bundle, cache_dir, transports=..., config={...}) as rpc_session:
    # rpc_session gives access to the guest's handle
    handle = rpc_session.handle  # FxtrRPCHandle to guest's service
    result = await handle.dispatch_and_wait(["process"], FxtrCall([data]))
```

Internally, run_rpc():
1. Opens run() to get TaskSession (JSON objects after Phase 1)
2. Wraps as ObjectStream[JSONTypes]
3. Opens simple_json_transport_stream → ObjectStream[IRValue]
4. Opens RemoteFxtrRPCContext + LocalFxtrRPCContext
5. Sends config (with any host capabilities)
6. Receives guest's main handle
7. Yields some session object with the handle

### Shims needed for WASM

The guest-side code (fxtr_sandbox_rpc_adapter) runs inside eryx. It needs:
1. Socket shim (for anyio import)
2. Loop shim (for anyio task groups, call_later, etc.)

These are currently injected by runner.py before the _runner.py code. For the RPC bridge,
the same shims are needed. The loop shim needs to be the full version from step 2
(with _host_sleep) because the transport uses task groups and potentially timeouts.

### Packaging

For a WASM task that uses RPC:
- Task depends on `fxtr-sandbox-rpc-adapter`
- `fxtr-sandbox-rpc-adapter` depends on `fxtr2-portable`
- `fxtr2-portable` depends on anyio, cbor2, frozendict, mashumaro

All of these flow through prepare.py's dependency resolution. The prepare.py change needed:
- Accept non-editable path dependencies (line 110-114 currently rejects them)
- Build them into PySourcePackage the same way as editable ones

The task's pyproject.toml would look like:
```toml
[project]
name = "my-rpc-task"
dependencies = [
    "fxtr-sandbox-rpc-adapter @ https://fxtr.transluce.org/unreachable/...",
]

[tool.uv.sources]
fxtr-sandbox-rpc-adapter = { path = "../../fxtr_sandbox_rpc_adapter" }

[tool.uv]
no-binary-package = ["cbor2"]
```

Wait, but fxtr2-portable is a transitive dep. The `no-binary-package` needs to be in the
task's pyproject.toml or in the prepare.py invocation. uv respects `[tool.uv]` in the
project's pyproject.toml for resolution, so `no-binary-package = ["cbor2"]` there should
work.

Actually wait — if we're going through the existing prepare.py flow, the packages end up as
PySourcePackage (for path deps) or PyWheelPackage (for remote deps). cbor2 would be a
remote dep → PyWheelPackage. But PyWheelPackage stores wheel URL + sha256. If we force
no-binary, uv would resolve to an sdist, not a wheel. Hmm.

Let me think... `uv export --format pylock.toml` outputs the resolved packages. With
`no-binary-package = ["cbor2"]`, would uv resolve cbor2 to an sdist? Actually, pylock.toml
can include sdists too. But our _build_packages only handles wheels and directories.

Hmm, this is getting complicated. Maybe the simpler approach is:
1. Build fxtr2-portable as a wheel (which only has Python files)
2. fxtr2-portable depends on cbor2
3. cbor2 gets resolved as a wheel (with C extension)
4. At install time in the sandbox, we strip .so files

Or:
1. fxtr-sandbox-rpc-adapter depends on fxtr2-portable (path dep → PySourcePackage)
2. fxtr2-portable depends on cbor2 (remote → PyWheelPackage)
3. cbor2's pure-python wheel is py3-none-any... but cbor2 publishes platform-specific wheels
   with C extensions AND a pure-python fallback wheel

Actually, does cbor2 publish a py3-none-any wheel? Let me think... cbor2 has a C extension
for speed but falls back to pure Python. The wheels on PyPI include both platform-specific
(with .so) and a universal wheel (without). Actually I'm not sure — some packages only
publish platform wheels when they have C extensions.

Actually I just checked: cbor2 publishes both `cbor2-5.6.5-cpython-...-linux.whl` (with C
ext) and a source dist. But _find_pure_python_wheel looks for `*-none-any.whl`. If cbor2
doesn't have a none-any wheel, it would fail.

Actually cbor2 does publish a universal wheel! Looking at PyPI, cbor2 5.6.5 has:
- cbor2-5.6.5.tar.gz (sdist)
- cbor2-5.6.5-cp3*-linux.whl, -macos.whl, -win.whl (platform wheels)

I don't think it publishes a -none-any wheel. So our _find_pure_python_wheel would fail.

So we have a few options:
1. Extend prepare.py to handle sdists (build from source) — complex
2. Extend prepare.py to handle platform wheels and strip .so at install time — hacky
3. Build cbor2 pure-python ourselves as part of the build process
4. Fork/vendor cbor2 pure-python

Actually, the simplest approach might be: in the prepare step, for packages where we can't
find a pure-python wheel, download the sdist, build it with an env var to skip C extensions,
and package the result.

Or even simpler: cbor2 is a transitive dep of fxtr2-portable. If fxtr2-portable is a path
dep → PySourcePackage, then its own deps (cbor2, etc.) flow through as either wheels or path
deps. If cbor2 ends up as a remote dep, we need a pure-python wheel.

Hmm, let me reconsider. What if fxtr2-portable just vendors cbor2's pure-python code? Or
what if we configure the no-binary and then handle sdist in prepare.py?

Actually, I think the cleanest thing is:
- Add sdist handling to prepare.py: if a package has no pure-python wheel but has an sdist,
  download it, build a wheel with `CBOR2_BUILD_C_EXTENSION=0` (or equivalent), and use that.

But this is getting complex. Maybe for now, the pragmatic approach is:
- Pre-build a cbor2 pure-python wheel as part of the portable build process
- Include it as a vendored wheel or configure it as a local dependency

Actually, let me re-read how step 3 handles this. Step 3 uses `uv pip install --target`
which handles everything, and then just strips .so files. The prepare.py flow is different
— it resolves to pylock.toml and classifies packages.

I think the user's suggestion of `no-binary-package = ["cbor2"]` is the right direction.
With that config, uv would resolve cbor2 from sdist. The pylock.toml would list cbor2 with
an sdist/archive entry instead of wheel entries. We'd need prepare.py to handle archives
(sdists): download, build wheel (with env var to skip C ext), unpack.

Alternatively: what if `no-binary-package` just makes uv pick the pure-python fallback?
For cbor2 specifically, if there's pure-python code in the sdist... Let me check.

RESOLVED: cbor2 publishes a py3-none-any wheel on PyPI. The pylock.toml from `uv export`
includes ALL available wheels (both platform-specific and pure-python). The existing
`_find_pure_python_wheel()` in prepare.py already picks the `*-none-any.whl`. No special
configuration needed — tested and confirmed.

### _runner.py changes

Currently _runner.py is a single script injected into the sandbox. For the RPC bridge:
- Still need the socket shim
- Now also need the loop shim (from step 2)
- Still need transport callbacks wiring
- The TaskContext changes: now sends/receives JSON objects
- Need to add: the upgrade path from raw callbacks to RPC

But with fxtr_sandbox_rpc_adapter as a separate importable package, the _runner.py stays
thin. It just:
1. Applies shims
2. Creates TaskContext (JSON objects)
3. Resolves and calls the entry point with the context

The entry point (task code) then does:
```python
from fxtr_sandbox_rpc_adapter import upgrade
async def run(ctx):
    async with upgrade(ctx) as rpc:
        ...
```

### Summary of changes needed

**Phase 1: JSON messaging**
- [ ] runner.py: callbacks + queues + TaskSession → JSON objects
- [ ] _runner.py: TaskContext → JSON objects
- [ ] container_runner.py: TaskSession → JSON objects
- [ ] fxtr_task_runner/protocol.py: send/receive JSON objects
- [ ] fxtr_task_runner/context.py: TaskContext → JSON objects
- [ ] Update tests

**Phase 2: RPC bridge**
- [ ] Update runner.py to include loop shim (from step 2)
- [ ] Extend prepare.py: accept non-editable path deps
- [ ] Extend prepare.py: handle sdist/archive deps (for cbor2)
- [ ] Add `no-binary-package = ["cbor2"]` to relevant config
- [ ] Create fxtr_sandbox_rpc_adapter package
  - ObjectStream adapter (wraps send/receive as anyio ObjectStream)
  - upgrade() function
  - Works for both WASM and Docker
- [ ] Create rpc_runner.py (host-side WASM RPC launcher)
- [ ] Create container_rpc_runner.py or extend container_runner.py (host-side Docker RPC)
- [ ] Create example RPC task
- [ ] Tests
- [ ] Update CODE_OVERVIEW.md
