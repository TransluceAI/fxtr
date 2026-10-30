# Network Transport Implementation Learnings

Issues discovered during implementation of the network transport feature.

## 1. Thread hang on sandbox error (fixed)

**Problem:** When an eryx sandbox errors during execution, the pytest process hangs indefinitely on thread shutdown. `py-spy dump` shows an `AnyIO worker thread` stuck in `queue.get()`.

**Root cause:** The sandbox runs in a worker thread via `anyio.to_thread.run_sync(sandbox.execute(...), abandon_on_cancel=True)`. The sandbox's `_receive_from_host` callback blocks on `h2s_queue.get()`, waiting for the host to send a message. If the sandbox errors (for any reason), eryx appears to wait for the in-flight callback to return before propagating the error. Since nothing ever sends to the queue, the thread deadlocks.

The same issue applies to `TaskSession.receive()` which blocks on `s2h_queue.get()` — if the sandbox crashes before sending a response, the host-side receive is stuck forever.

**Fix:** Added a `_SHUTDOWN` sentinel object. The `finally` block in `run()` pushes sentinels onto both queues:
```python
h2s_queue.put(_SHUTDOWN)
s2h_queue.put(_SHUTDOWN)
```
The `_receive_from_host` callback and `TaskSession.receive()` check for the sentinel and raise `RuntimeError` to unblock.

**Applies to:** Any use of blocking queues with eryx callbacks. The existing non-transport `run()` had the same latent bug — it would hang if the sandbox errored while waiting for `_receive_from_host`.

## 2. Getting tracebacks out of eryx

**Problem:** When eryx reports `ExecutionError: Unknown error`, there's no Python traceback visible. This makes debugging sandbox errors very difficult.

**Root cause:** Sandbox `print()` output goes to eryx's captured stdout/stderr, not to the host process's stdout. The error traceback is written to the sandbox's stderr, which is only available after `sandbox.execute()` returns — but if the error causes an `ExecutionError`, the stderr might not be surfaced in the exception message.

**Fix:** Use eryx's output streaming callbacks:
```python
sandbox = factory.create_sandbox(
    callbacks=registry,
    on_stdout=lambda chunk: print(f"[SANDBOX] {chunk}", end="", flush=True),
    on_stderr=lambda chunk: print(f"[SANDBOX ERR] {chunk}", end="", flush=True),
)
```
These fire in real-time as the sandbox writes to stdout/stderr, even if the sandbox subsequently errors. This is essential for debugging — the `on_stderr` callback showed the actual traceback that `ExecutionError: Unknown error` was hiding.

**Reference:** https://docs.eryx.run/latest/book/guide/output-streaming.html

## 3. anyio cannot import in eryx's WASM sandbox (worked around)

**Problem:** `import anyio` fails inside the eryx sandbox with:
```
ImportError: cannot import name 'AddressFamily' from 'socket' (unknown location)
```

**Root cause:** anyio imports `socket.AddressFamily`, `socket.SocketKind`, and `socket.socket` from the standard library `socket` module. Eryx's WASM sandbox provides a limited `socket` module that has the constants (`AF_INET`, `SOCK_STREAM`, etc.) as plain ints but is missing the `IntEnum` wrappers (`AddressFamily`, `SocketKind`) and some constants (`AF_UNSPEC`).

anyio also uses `socket.AF_UNSPEC` as a default argument value (not just a type annotation), so it must exist as a real value.

**Impact:** This blocks httpcore's async support. httpcore uses anyio for its async synchronization primitives (`AsyncEvent`, `AsyncLock`, `AsyncShieldCancellation`). When anyio can't import, httpcore sets `anyio = None` and raises `RuntimeError: Running with asyncio requires installation of 'httpcore[asyncio]'` when any async operation is attempted.

**Workaround:** A socket shim injected before any imports in the sandbox. It patches eryx's socket module with the missing names, built from the constants eryx already provides:
```python
import socket as _socket
import enum as _enum
if not hasattr(_socket, 'AF_UNSPEC'):
    _socket.AF_UNSPEC = 0
if not hasattr(_socket, 'AddressFamily'):
    class AddressFamily(_enum.IntEnum):
        AF_UNSPEC = 0
        AF_INET = _socket.AF_INET
        AF_INET6 = _socket.AF_INET6
        AF_UNIX = _socket.AF_UNIX
    _socket.AddressFamily = AddressFamily
if not hasattr(_socket, 'SocketKind'):
    class SocketKind(_enum.IntEnum):
        SOCK_STREAM = _socket.SOCK_STREAM
        SOCK_DGRAM = _socket.SOCK_DGRAM
    _socket.SocketKind = SocketKind
```

This is injected by `runner.py` at the start of every sandbox execution (via `_SOCKET_SHIM`). It's harmless for tasks that don't use transports since the shim only adds missing names.

**Long-term fix:** eryx should add `AddressFamily`, `SocketKind`, and `AF_UNSPEC` to its socket module natively. This would also unblock any task code that wants to use anyio directly.

**Demo:** `examples/eryx_anyio_test.py` demonstrates the problem and the shim fix.

## 4. httpcore/httpx import is guarded, not required (design decision)

**Problem:** The virtual socket transport classes (`_CallbackNetworkStream`, `_CallbackTransport`, etc.) subclass httpcore/httpx types, so they can't be defined if those packages aren't available.

**Design:** `_runner.py` (the single script injected into all sandboxes) imports httpcore/httpx at the top level inside a `try/except ImportError`. If the import succeeds, the transport classes are defined. If not, `_HTTPCORE_AVAILABLE` is set to `False` and `get_transport()` raises a helpful `RuntimeError` at call time.

This means:
- Tasks that don't use transports don't need httpcore/httpx in their dependencies
- Tasks that use transports get a clear error if httpcore is missing
- The same `_runner.py` is injected for all tasks (no conditional code assembly)
