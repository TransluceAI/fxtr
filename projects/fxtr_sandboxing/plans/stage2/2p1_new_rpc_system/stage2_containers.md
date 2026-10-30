# Stage 2 — Containers

Bring `DockerTaskBundle` into the new spawner shape. Goal: same user-facing API as
stage 1, dispatching on bundle type. Containers come up alongside WASM, share the
spawner / `connect` flow end-to-end, and tear down cleanly on host release.

## High-level shape

The host side gets a second per-bundle dispatch arm in `SandboxSpawnerImpl`. The
in-container helper package is renamed (`fxtr_task_runner` →
`fxtr_service_runner`) and reduced to a thin shim around
`fxtr_sandbox_rpc_adapter.main(ctx, entry_point=...)`. The container's
``module:function`` entry point is discovered inside the container — either from
the `fxtr.service` entry-point group (default) or from a CLI argument the
container's `cmd_override` can pass.

While we're touching the in-container shim, we restructure two adjacent things
along clean lines:

1. **Split the JSON ObjectStream into two implementations**, one per
   transport shape:
   - `fxtr_sandbox_types/json_object_stream.py` — `_JSONObjectStream`
     (the existing escape-based EOF impl; kept as-is, used by WASM where
     eryx exposes JSON-typed callbacks instead of a byte stream).
   - `fxtr_sandbox_types/socket_json_stream.py` — `_SocketJSONStream`
     (new; wraps `anyio.SocketStream` directly. Length-prefixed JSON on
     send; native `EndOfStream` propagates from the underlying socket;
     `send_eof()` performs a real UDS half-close. No escape layer.).
2. **Define `SandboxServiceContext` as a `typing.Protocol` in
   `fxtr_sandbox_types/service_context.py`** — the typed contract between
   the runtime and the adapter. Stage 2 shape:
   ```text
   class SandboxServiceContext(Protocol):
       @property
       def main_protocol_stream(self) -> ObjectStream[JSONTypes]: ...
       # TODO(stage 3): network transport connection primitives here.
   ```
   `fxtr_sandbox_rpc_adapter.main(ctx: SandboxServiceContext, *, entry_point)`
   accesses `ctx.main_protocol_stream` and otherwise stays unchanged. Each
   runtime provides its own concrete impl: `_EryxServiceContext` (WASM,
   `main_protocol_stream = _JSONObjectStream(...)`) and
   `_ContainerServiceContext` (container,
   `main_protocol_stream = _SocketJSONStream(...)`).

Network-transport plumbing in `container_runner.py` (route map, multiplexer
accept loop) is retained as-is in stage 2 so stage 3 can reshape it in place
rather than rebuild from scratch.

## Steps

### 1. Rename `fxtr_task_runner` → `fxtr_service_runner`

Aligns with the renamed entry-point group (`fxtr.service`) and the
"sandbox runs a service" framing.

- [x] Rename the package directory `fxtr_task_runner/` → `fxtr_service_runner/`
      and the module subdirectory inside it.
- [x] Update `pyproject.toml` (`name = "fxtr-service-runner"`, `packages = ["fxtr_service_runner"]`).
- [x] Update `fxtr_sandboxing/pyproject.toml` (`fxtr-task-runner` →
      `fxtr-service-runner`, both in `dependencies` and in `[tool.uv.sources]`).
- [x] `uv lock` to refresh lockfile.
- [x] Update all imports: `from fxtr_task_runner.protocol import ...` →
      `from fxtr_service_runner.protocol import ...`. Affected files:
      `fxtr_sandboxing/container_runner.py`, any test that touches the package.
- [x] `tests/test_protocol.py` keeps passing — module name change only.

### 2. Split JSON ObjectStream impls; add `_SocketJSONStream`

Two object-stream impls, one per transport shape. Lives in
`fxtr_sandbox_types`, shared between host and sandbox.

- [x] Add `anyio` as a base dependency of `fxtr_sandbox_types`
      (`fxtr_sandbox_types/pyproject.toml` `dependencies = ["anyio>=4"]`).
      anyio is pure-Python; safe for both host and portable consumers.
      Both extras (`[fxtr2]` / `[fxtr2-portable]`) already pull anyio
      transitively, so this is just making the direct dep explicit.
- [x] Split `fxtr_sandbox_types/object_stream.py` into two files:
      - `fxtr_sandbox_types/json_object_stream.py` — moves
        `_JSONObjectStream` (and the `JSONTypes` type alias) out of
        `object_stream.py`. No behavior change. Module docstring narrows
        to "JSON-callback ObjectStream impl with escape-based EOF; for
        runtimes whose transport is a JSON-typed callback channel, like
        eryx."
      - `fxtr_sandbox_types/socket_json_stream.py` — new file, holds
        `_SocketJSONStream`. Imports `JSONTypes` from
        `.json_object_stream`.
      - Delete the old `object_stream.py`.
- [x] Implement `_SocketJSONStream`:
      ```text
      class _SocketJSONStream:
          def __init__(self, stream: anyio.abc.ByteStream) -> None: ...
          async def send(item: JSONTypes) -> None:
              # json.dumps -> utf-8 -> 4-byte BE length prefix -> stream.send
          async def receive() -> JSONTypes:
              # _read_exact(4) for header, _read_exact(length) for body, json.loads
              # Lets anyio.EndOfStream propagate naturally on peer half-close.
          async def send_eof() -> None:
              # stream.send_eof()  — real UDS half-close
          async def aclose() -> None:
              # stream.aclose()
          # Plus __aiter__ / __anext__ that translate EndOfStream -> StopAsyncIteration.
      ```
      `_read_exact` reads from `stream.receive(n)` in a loop. EOF
      semantics:
      - If `stream.receive` raises `anyio.EndOfStream` *before any
        header bytes have been read*, propagate cleanly — this is the
        graceful-drain path; the RPC reader loop expects `EndOfStream`
        to mean "peer closed cleanly between frames."
      - If `stream.receive` raises `EndOfStream` *mid-header* or
        *mid-body* (some bytes already buffered for this frame),
        translate to `anyio.BrokenResourceError("incomplete frame: got
        N of M bytes")`. This is a peer crash / abrupt drop, not a
        clean shutdown — distinguishing it makes debugging easier and
        keeps `EndOfStream` as a clean-close signal.
- [x] Update `fxtr_sandbox_types/__init__.py` re-exports:
      `JSONTypes` and `_JSONObjectStream` from `.json_object_stream`,
      `_SocketJSONStream` from `.socket_json_stream`. Import-site updates:
      anywhere that does `from fxtr_sandbox_types import _JSONObjectStream`
      keeps working unchanged; deeper imports
      (`from fxtr_sandbox_types.object_stream import ...`) need to be
      retargeted at the new module names. Affected files (sweep):
      `fxtr_sandboxing/spawner.py`, `fxtr_sandbox_rpc_adapter/_main.py`,
      and any test using deep imports.
- [x] Tests: new `tests/test_socket_json_stream.py` (9 tests covering
      round-trip + multi-frame + various JSON types + clean EOF +
      truncated-header / truncated-body BrokenResourceError + aclose
      + async iteration). Uses anyio UDS listener + connect over a
      tmpdir socket path.

### 2.5. Define `SandboxServiceContext` Protocol in `fxtr_sandbox_types`

The runtime↔adapter contract, codified as a `typing.Protocol` so each
runtime's concrete implementation satisfies it structurally.

- [x] New file `fxtr_sandbox_types/service_context.py`:
      ```text
      from typing import Protocol
      from anyio.abc import ObjectStream
      from .json_object_stream import JSONTypes

      class SandboxServiceContext(Protocol):
          """Runtime-provided context handed to fxtr_sandbox_rpc_adapter.main.

          Carries the JSON ObjectStream that backs the host RPC connection.
          Each runtime (eryx WASM, container) provides its own concrete impl.

          Future: stage 3 will add network transport connection primitives
          here — likely an async `connect_transport(token: str)` that
          returns an httpx-compatible transport, with the runtime threading
          the redemption call back to the host-side authorizer registry.
          See plans/stage2/2p1_new_rpc_system/stage3_transports.md.
          """

          @property
          def main_protocol_stream(self) -> ObjectStream[JSONTypes]: ...
      ```
- [x] Re-export `SandboxServiceContext` from
      `fxtr_sandbox_types/__init__.py`.
- [x] No changes to `fxtr_sandbox_types/protocols.py` — that file remains
      reserved for fxtr2 service protocols (`SandboxConnectProto`,
      `SandboxSpawnerProto`). `service_context.py` is a separate file
      because `SandboxServiceContext` is a plain `typing.Protocol`, not a
      fxtr2 `@service_protocol` — different machinery, different
      audiences.

### 3. Adapter API: `main(ctx: SandboxServiceContext, *, entry_point=...)`

Reshape the adapter to consume the new `SandboxServiceContext` protocol
instead of the old `(send, receive)`-callable shape. The body that uses
`ctx.send/ctx.receive` to build a `_JSONObjectStream` goes away — the
runtime hands in the right stream directly via `ctx.main_protocol_stream`.

- [x] `fxtr_sandbox_rpc_adapter._main.main(ctx: SandboxServiceContext, *, entry_point: str)`:
      replace `json_stream = _JSONObjectStream(ctx.send, ctx.receive)` with
      `rpc_message_stream = wrap_json_stream(ctx.main_protocol_stream)`.
      Body is otherwise unchanged (entry-point resolve, `open_rpc_scope`,
      etc.).
- [x] Update WASM's `fxtr_sandboxing/_runner.py`:
      - Replace `_EryxTaskContext` with `_EryxServiceContext`. Same shape
        (defined inline in `_runner.py`), but instead of exposing
        `send` / `receive`, it exposes a `main_protocol_stream` property
        whose value is a single `_JSONObjectStream` instance built from
        the eryx-injected callbacks (`_send_to_host` /
        `_receive_from_host`). The instance is constructed once in
        `__init__` and returned by the property — the adapter caches a
        reference, so identity matters.
      - `await fxtr_sandbox_rpc_adapter.main(_EryxServiceContext(),
        entry_point=_ENTRY_POINT)`.
- [x] Update host-side `fxtr_sandboxing/spawner.py::_spawn_runner_wasm`: no
      structural change. Still builds `_JSONObjectStream(session.send,
      session.receive)` and feeds the result into `wrap_json_stream` —
      the host doesn't go through `SandboxServiceContext`, only the
      sandbox-side runner does.
- [x] No change to `fxtr_sandbox_rpc_adapter._guest_main` (it doesn't see
      the stream; it sees a mounted ServiceRef).
- [x] **Stage-1 regression gate.** All 4 `tests/test_spawner.py` tests
      pass against the new adapter API + `_EryxServiceContext` shape
      (test_spawn_then_connect_greet, test_spawn_once_connect_twice,
      test_teardown_basic, test_add). 40s run.

### 4. Rewrite `fxtr_service_runner` around the new context

Rename `fxtr_service_runner/context.py` → `service_context.py`. Reshape
`TaskContext` → `_ContainerServiceContext`, satisfying
`SandboxServiceContext`.

- [x] `fxtr_service_runner/service_context.py`:
      ```text
      class _ContainerServiceContext:
          """SandboxServiceContext impl backed by an anyio UDS SocketStream.

          Built from anyio.connect_unix(SOCKET_PATH); wraps the resulting
          SocketStream in a single _SocketJSONStream that backs
          main_protocol_stream.
          """

          def __init__(self, socket_stream: anyio.abc.SocketStream) -> None:
              self._main_protocol_stream = _SocketJSONStream(socket_stream)

          @property
          def main_protocol_stream(self) -> ObjectStream[JSONTypes]:
              return self._main_protocol_stream
      ```
      Implements the protocol structurally; no runtime check needed.
- [x] Drop the old `TaskContext` `send` / `receive` / `get_transport` /
      `get_client` / `close` methods. `send`/`receive` are subsumed by
      `stream`. `get_transport` / `get_client` keep the standalone
      module-level forms in `fxtr_service_runner/transport.py` (already
      present); stage 3 will fold them onto `SandboxServiceContext`.
- [x] Leave `fxtr_service_runner/transport.py` untouched in stage 2.
      `get_transport` / `get_client` stay as the existing
      multiplexed-UDS-name-keyed shape (no callers in stage 2; stage 3
      reshapes to token-keyed).
- [x] Rewrite `fxtr_service_runner/__main__.py` as a thin shim:
      - CLI signature: `python -m fxtr_service_runner [ENTRY_POINT]`.
        Optional positional argument is a `module:function` string. If
        absent, fall back to discovering the (single) `fxtr.service` entry
        point from installed packages (rename of the existing `fxtr.task`
        lookup). This keeps the image self-describing by default and lets
        `cmd_override` in the toml pin a specific entry point when the
        image bakes in multiple services.
      - Use `anyio.run(_run, backend="asyncio")` instead of
        `asyncio.run`. The adapter is anyio-native; running anyio at the
        top level keeps cancellation / scope semantics consistent with
        the rest of the stack.
      - `_run(entry_point: str)`:
        1. `socket_stream = await anyio.connect_unix(SOCKET_PATH)`,
        2. `ctx = _ContainerServiceContext(socket_stream)`,
        3. `await fxtr_sandbox_rpc_adapter.main(ctx, entry_point=entry_point)`,
        4. `await socket_stream.aclose()` in `finally` (idempotent if
           the adapter already half-closed it via `send_eof`).
      - Error handling: `RuntimeError("Multiple fxtr.service entry points: ...")` /
        `RuntimeError("No fxtr.service entry point found ...")` keep the
        existing messages, just with the renamed group.
- [x] `pyproject.toml` deps: add `anyio>=4`. Heavy in-container runtime
      deps (`fxtr-sandbox-types[fxtr2-portable]` and
      `fxtr-sandbox-rpc-adapter`) live behind a `[runtime]` extra so the
      host can install fxtr-service-runner without pulling fxtr2-portable
      (which would conflict with the host's fxtr2 install). The
      container's Dockerfile installs ``fxtr-service-runner[runtime]``;
      host-side installs (used only for the protocol helpers) get a slim
      slice of just stdlib + anyio + httpx. Keep `httpx>=0.28` as a base
      dep (used by `transport.py`'s stage-3-bound stubs).
- [x] `fxtr_service_runner/__init__.py`: no public surface needed beyond
      the existing module-level imports.

### 5. Add `_spawn_runner_container` to `SandboxSpawnerImpl`

Mirror the `_spawn_runner_wasm` shape. Convert the `_spawn_runner_*` runners
into methods on `SandboxSpawnerImpl` so they have access to `self.cache_dir` /
`self.insecure_registry` / `self.env_tg` directly.

- [x] Move `_spawn_runner_wasm` body into `SandboxSpawnerImpl._spawn_runner_wasm`
      (method, not module-level function). Update `task_group.start(...)` call
      sites to use the bound method.
- [x] Add `_spawn_runner_container(self, bundle, *, task_status, teardown_event)`:
      ```text
      async with _run_container_internal(bundle, insecure_registry=...) as socket_stream:
          json_stream = _SocketJSONStream(socket_stream)
          rpc_message_stream = wrap_json_stream(json_stream)
          async with open_rpc_scope(
              rpc_message_stream,
              remote_service_type=SandboxConnectProto,
              name="host_rpc",
          ) as rpc_session:
              task_status.started((rpc_session.remote_service, rpc_session.scope))
              await teardown_event.wait()
      ```
      Same shape as `_spawn_runner_wasm`. The CM yields an
      `anyio.SocketStream` (the accepted UDS connection from the container)
      rather than a `TaskSession` — the spawner wraps it in
      `_SocketJSONStream` directly.
- [x] Update `spawn` dispatch: `DockerTaskBundle` branch picks
      `self._spawn_runner_container`; `TaskBundle` branch keeps
      `self._spawn_runner_wasm`.

### 6. Privatize `container_runner.py`'s public CM

- [x] Rename `run_container` → `_run_container_internal`. Keep the
      `@asynccontextmanager` shape; rewrite the docstring to call out that this
      is spawner-internal, no longer user-facing.
- [x] Drop the `transports: list[TransportRoute] | None` parameter from the
      signature. The route_map / multiplexer logic stays in the body but is
      gated behind a `False`-defaulted flag (or, simplest, the param defaults
      to `None` and we leave the rest of the body untouched — stage 3 will
      re-thread it). Idea: keep a hidden `_transports` parameter that the
      spawner doesn't pass, so stage 3 can re-introduce it from the env-level
      registry without re-implementing the multiplexer.
- [x] Add `insecure_registry: bool = False` to the signature; spawner forwards
      `self._insecure_registry` through.
- [x] **Yield an `anyio.SocketStream` instead of a `TaskSession`.** The
      `TaskSession` class in `container_runner.py` becomes unnecessary —
      the spawner wraps the raw socket via `_SocketJSONStream` directly.
      Replace the `accept` block:
      ```text
      # was:
      conn = await anyio.to_thread.run_sync(lambda: server_sock.accept()[0], ...)
      session = TaskSession(conn)
      yield session

      # now:
      socket_stream = await transport_listener.accept()
      # (or, if we keep server_sock, wrap it with anyio.connect_unix-style
      # adapter and accept via anyio's UDS listener API — see step 6.5)
      yield socket_stream
      ```
      Drop the `TaskSession` class (and its `anyio.to_thread.run_sync`
      calls, which only existed because the old `send_message` /
      `receive_message` were sync stdlib-socket-based).
- [x] Forward container stdout/stderr to host stdout/stderr (currently
      `subprocess.PIPE`'d but never consumed). Either:
      - Spawn two anyio threads consuming the pipes and writing to
        `sys.stdout` / `sys.stderr`, OR
      - Pass `stdout=None, stderr=None` (inherit host streams).
      Inherit is simpler and matches WASM's `on_stdout=lambda c: sys.stdout.write(c)`
      pattern. Pick that.
- [x] Pass `FXTR_ENTRY_POINT` env var? No — the container discovers the entry
      point itself (step 4). The host doesn't know it. The toml's
      `[env]` / `cmd_override` provide user-controllable knobs.
- [x] Container teardown: try graceful shutdown first.
      `open_rpc_scope`'s normal exit drains the conn, the host's
      `_SocketJSONStream.send_eof()` does an actual UDS half-close on its
      write side, the container reads through any pending bytes and then
      `anyio.EndOfStream` propagates from the underlying socket; the
      in-container conn drains symmetrically (calls `send_eof()` on its end,
      which the host then sees), `main()` returns, `python -m fxtr_service_runner`
      exits 0, the container process exits 0. Host's
      `_run_container_internal` `finally` block:
      1. Drop the connection (already happened naturally on CM exit).
      2. `container_proc.wait(timeout=GRACEFUL_TEARDOWN_S)` — say 5 seconds.
         If the container exited cleanly, this returns immediately.
      3. On timeout: `container_proc.terminate()` and re-`wait(timeout=5)`.
      4. On further timeout: `container_proc.kill()` + `wait()`.
      Same fallback shape as today, but the "happy path" doesn't require
      terminate.
- [x] Keep all the rest of `container_runner.py` (route_map dict construction,
      `_handle_transport_conn`, `_transport_accept_loop`, the
      `transport_listener` setup, `aiohttp.ClientSession` lifecycle, mount
      tmpdir setup) untouched. None of it gets exercised in
      stage 2 because `transports` is always `None`/empty, but the code stays
      where stage 3 expects it.

### 6.5. Switch the host's task-socket setup to anyio UDS listener

The current `container_runner.py` builds a stdlib `socket.socket(AF_UNIX, ...)`
listener and `accept`s on a thread. Now that we yield an `anyio.SocketStream`
to the spawner, switch to anyio's native UDS listener — same pattern already
used for the transport multiplexer socket.

- [x] Replace `server_sock = socket.socket(...); server_sock.bind(socket_path)
      ; server_sock.listen(1)` with
      `task_listener = await anyio.create_unix_listener(socket_path, mode=0o777)`.
- [x] Replace `conn = await anyio.to_thread.run_sync(lambda:
      server_sock.accept()[0], ...)` with
      `socket_stream = await task_listener.accept()`.
- [x] `task_listener.aclose()` in the `finally` block (replaces
      `server_sock.close()`).

### 7. `insecure_registry` plumbed through environment

- [x] `SandboxSpawnerImpl.__init__(*, cache_dir, env_tg, insecure_registry: bool = False)`.
- [x] `create_sandbox_environment(*, cache_dir, insecure_registry: bool = False)`.
      Forward to spawner.
- [x] Update host-side test plumbing: `tests/test_container_spawner.py` passes
      `insecure_registry=True` because the demo image lives at
      `localhost:5000/...`.

### 8. `container_prepare.py` and `container_config.py` housekeeping

The container side has no `entry_point` field on its bundle (entry-point lives
inside the image, not on the bundle), so `container_prepare.py` mostly stays as-is.

- [x] Confirm `fxtr_container_task.toml` schema unchanged (no new fields needed
      for stage 2). The toml's `[run]` section's `cmd_override` is what users
      pass to override the in-container entry point if they want.
- [x] `tests/test_container_prepare.py` lifted back to `tests/`. One import
      change: `fxtr_sandboxing.entities` → `fxtr_sandbox_types` (entities
      moved out of the host package in stage 1). 9 tests pass.
- [x] `tests/test_container_config.py` lifted back unchanged. 9 tests pass.
- [x] `tests/test_container_rebuild.py` lifted back unchanged. 8 tests pass.

### 9. Container demo: `examples/container_rpc_task/`

Mirror `examples/rpc_task/` so the same-shape `GreeterProto` /
`EntityRef[GreetingConfig]` config exercises the same typed-config path under a
container instead of WASM. Plus a `platform_info()` method on the protocol so
tests can confirm the call really ran inside the container.

- [x] `task_src/` package structure:
      ```
      task_src/
        pyproject.toml          # Declares neither fxtr-sandbox-rpc-adapter
                                #   nor fxtr-service-runner as deps. The image
                                #   layer (Dockerfile) bundles both via wheel
                                #   install. The user-task code doesn't import
                                #   either package any more (the entrypoint
                                #   takes (config, serve) — no RPC ctx surface
                                #   to interact with), so neither dep is
                                #   needed at the package level. See "Decisions"
                                #   below for the asymmetry vs. WASM.
        my_rpc_task/
          __init__.py           # Mirrors examples/rpc_task/rpc_task/__init__.py
                                #   exactly, plus a `platform_info()` service method
                                #   that returns {"platform": platform.platform(),
                                #   "python": platform.python_version()}.
                                #   Same RPC_TASK_NS UUID + define_type calls so
                                #   GreetingConfig / GreeterProto are wire-compatible
                                #   with the WASM example. (Or use a fresh UUID —
                                #   doesn't matter as long as the test's host-side
                                #   GreeterProto definition matches.)
      ```
- [x] `[project.entry-points."fxtr.service"] service = "my_rpc_task:run"`.
- [x] `Dockerfile` based on the existing pattern:
      ```
      FROM python:3.12-slim
      COPY --from=wheels / /tmp/wheels/
      RUN pip install --no-cache-dir --no-deps /tmp/wheels/fxtr2_portable-*.whl \
          && pip install --no-cache-dir --no-deps /tmp/wheels/fxtr_sandbox_types-*.whl \
          && pip install --no-cache-dir --no-deps /tmp/wheels/fxtr_sandbox_rpc_adapter-*.whl \
          && pip install --no-cache-dir /tmp/wheels/fxtr_service_runner-*.whl \
          && pip install --no-cache-dir anyio cbor2 frozendict mashumaro \
          && rm -rf /tmp/wheels/
      COPY task_src/ /app/task_src/
      RUN pip install --no-cache-dir --no-deps /app/task_src/
      CMD ["python", "-m", "fxtr_service_runner"]
      ```
      Notes:
      - Adds `fxtr_sandbox_types` to the wheel install list (new in stage 1).
      - Renames `fxtr_task_runner` → `fxtr_service_runner` everywhere.
      - Stays on `python:3.12-slim`. The package set targets `>=3.12`.
- [x] `rebuild.sh`: build all four wheels (`fxtr2-portable`,
      `fxtr_sandbox_types`, `fxtr_sandbox_rpc_adapter`, `fxtr_service_runner`)
      into the named build context. Mirrors the existing pattern.
- [x] `setup.sh`: same as the old `examples/old_broken/container_rpc_task/setup.sh`
      with the renames; updates `fxtr_container_task.toml` digest after push.
- [x] `fxtr_container_task.toml`: same shape as the old example; image registry
      `localhost:5000/demo-rpc-task`, no source mounts (everything baked in),
      no `cmd_override` (default CMD discovers the entry point).

### 10. Tests: `tests/test_container_spawner.py`

Mirror `tests/test_spawner.py` against the new container demo. Same four happy-path
shapes plus a fifth that verifies we really ran in the container.

- [x] `_runtime_available()` / `_image_available()` skip helpers (lift from the
      old `test_container_runner.py`).
- [x] Local `GreetingConfig` / `GreeterProto` mirrors of the demo's types
      (using the same `RPC_TASK_NS` UUID + `define_type` strings as the demo,
      so the host's classes are wire-compatible with the demo's classes).
      Add a `platform_info() -> dict[str, str]` method to the local
      `GreeterProto` to mirror the demo.
- [x] Tests:
      1. `test_container_spawn_then_connect_greet` — spawn the container bundle,
         connect with a `GreetingConfig("hello", "!")` ref, assert
         `await user.greet("world") == "hello, world!"`.
      2. `test_container_spawn_once_connect_twice` — two `connect` calls with
         different configs on one spawn ref.
      3. `test_container_spawn_then_platform_info` — call `platform_info()`,
         assert `result["python"]` matches the container's Python (or at least
         is *some* version string), and assert `result["platform"]` includes
         "Linux" (the host running the test is macOS, so this proves the call
         crossed the container boundary).
      4. `test_container_teardown_basic` — same shape as the WASM teardown
         test: two back-to-back container spawns in one env, each in its own
         sub-scope. Slow (image pull / start twice). Mark with a custom
         `pytest.mark.slow` marker so users can opt out via
         `pytest -m "not slow"`. Always opt in for CI.
      5. `test_container_add` — sanity check on the no-config-touching method.
- [x] `create_sandbox_environment(cache_dir=..., insecure_registry=True)` for
      every test that talks to `localhost:5000/...`.
- [x] Pass through `tests/old_broken/test_container_runner.py` and
      `test_container_rpc_runner.py` — they stay in `old_broken/` (different
      API; not reviving in stage 2).

### 11. Update CODE_OVERVIEW.md

Reflect:

- Package rename `fxtr_task_runner` → `fxtr_service_runner`.
- Container path now wired through `SandboxSpawnerImpl._spawn_runner_container`.
- `container_runner.py` is internal-only (`_run_container_internal`).
- Stage 3 stubs (transport routing) explicitly noted as "still here, not yet wired".
- `examples/container_rpc_task/` exists as the new container demo (separate from
  `examples/old_broken/container_rpc_task/`).

## Decisions captured here for future reference

- **Entry-point pinning lives inside the container, not on the bundle.** A
  Docker container is opaque to the host: it's an OCI digest, not a bundle of
  Python source the host can introspect. The `module:function` resolution
  happens inside `fxtr_service_runner.__main__` via the `fxtr.service` entry-
  point group. Override path: pass the string as a CLI arg via `cmd_override`
  in `fxtr_container_task.toml`. **WASM is asymmetric here**: WASM bundles
  pin `entry_point` on `TaskBundle` because the host builds the bundle from
  pyproject metadata and threads it into the sandbox at execute time. Both
  paths converge on the same adapter call shape (`main(ctx, entry_point=...)`)
  but the *source* of the string differs.
- **No public `TaskSession` for either runtime.** Spawner-only access. The
  low-level `runner.py` / `container_runner.py` only export internal helpers
  used by the spawner.
- **JSON framing on the container UDS stays the same** as stage 1 / pre-rewrite
  (4-byte big-endian length + UTF-8 JSON body). Stage 3's transport socket
  multiplexing is a *separate* socket at `/run/fxtr/transport.sock`; the task
  socket stays single-purpose.
- **`fxtr_service_runner`'s entry-point is `python -m fxtr_service_runner`**
  as the container's CMD. Image doesn't need to know which user service it's
  running by default; CLI arg / `cmd_override` opt-in to pinning.
- **Anyio at the top of the container.** Switching from `asyncio.run` to
  `anyio.run` keeps the entire stack on one async runtime. anyio uses the
  asyncio backend by default, so behavior is unchanged.
- **`insecure_registry` is env-level, not per-spawn.** It's a property of the
  test/dev environment (whether to talk to `localhost:5000` over HTTP),
  not of any individual bundle. Lives on `create_sandbox_environment` and
  flows into `SandboxSpawnerImpl.__init__`.
- **Stage-3 transport plumbing stays.** The `route_map` / `_handle_transport_conn`
  / `_transport_accept_loop` / `aiohttp.ClientSession` machinery in
  `container_runner.py` is retained but unreachable from the spawner in
  stage 2 (no `transports` arg threaded through). Stage 3 reshapes it
  in-place rather than re-creating it. Same treatment as the eryx
  `_transport_*` callbacks in `runner.py`.
- **Container demo's `task_src` declares neither runtime dep.** The user
  task imports nothing from `fxtr_sandbox_rpc_adapter` or
  `fxtr_service_runner`; the adapter API is now `(config, serve)` from
  the user's perspective. The image bundles both runtime packages via
  the Dockerfile's wheel install; the user-task pyproject can stay clean
  of infrastructure deps. This is asymmetric with `examples/rpc_task`'s
  WASM pyproject, which keeps `fxtr-sandbox-rpc-adapter` declared
  because **`prepare()` walks the dep tree to bundle WASM site-packages**
  — the dep declaration drives bundling. Containers don't have an
  equivalent step (the image is opaque), so the dep declaration would
  be vestigial there. Different installation models, different dep
  declarations.
- **One adapter contract (`SandboxServiceContext` Protocol), two
  ObjectStream impls.** The adapter calls `main(ctx, *, entry_point)`
  where `ctx` satisfies `SandboxServiceContext` (a `typing.Protocol` in
  `fxtr_sandbox_types/service_context.py`). The protocol exposes
  `ctx.main_protocol_stream: ObjectStream[JSONTypes]`; stage 3 will
  extend it with network-transport connection primitives. Each runtime
  provides its own concrete impl, picking the JSON ObjectStream that
  matches its transport:
  - **WASM** (`_EryxServiceContext`) wraps `_JSONObjectStream`
    (escape-based EOF) because eryx exposes JSON-typed callbacks and
    there's no underlying byte stream to half-close.
  - **Container** (`_ContainerServiceContext`) wraps `_SocketJSONStream`
    (anyio half-close, native EOF) because the channel is a real UDS
    byte stream where `send_eof()` and `EndOfStream` are first-class
    primitives.
  Both stream impls live in dedicated files
  (`fxtr_sandbox_types/json_object_stream.py` and
  `socket_json_stream.py`). anyio is a base dep of
  `fxtr_sandbox_types`.
- **Clean drain over forced terminate.** Host releases bridges → conn drain →
  host's `_SocketJSONStream.send_eof()` does a real UDS half-close on its
  write side → container reads remaining bytes, then `anyio.EndOfStream`
  propagates from the underlying socket → in-container conn drains and
  half-closes its end → host reads its `EndOfStream` → `main()` returns
  → `python -m fxtr_service_runner` exits 0 → container exits 0 →
  host's `wait(timeout=GRACEFUL_TEARDOWN_S)` succeeds without needing
  terminate. Terminate / kill remain as fallbacks on timeout.
- **Container stdout/stderr forward to host.** Inherit host streams
  (`stdout=None, stderr=None` in `subprocess.Popen`) so debug output flows
  through naturally. Same UX as WASM's `on_stdout=lambda c: sys.stdout.write(c)`.

## Likely gotchas

- The container needs `fxtr2-portable` *and* `fxtr_sandbox_types` *and*
  `fxtr_sandbox_rpc_adapter` *and* `fxtr_service_runner` installed in its
  Python env; the existing Dockerfile in `old_broken/` only installed three.
  `rebuild.sh` needs the new wheel build for `fxtr_sandbox_types` and the
  rename for `fxtr_task_runner` → `fxtr_service_runner`.
- `_SocketJSONStream`'s `_read_exact` distinguishes between graceful
  EOF (between frames → propagate `EndOfStream`) and mid-frame
  truncation (peer crash / abrupt drop → translate to
  `anyio.BrokenResourceError`). The clean-close case is the drain path
  the RPC layer expects; the broken case is degenerate but should give
  a clearer signal than a confusing `EndOfStream` partway through a
  message. Unit-test both.
- Containers spin up slowly; `test_container_teardown_basic` will be slow.
  Tag with a `slow` marker so users can opt out, but keep it in CI for
  correctness coverage.
- Switching `__main__` to `anyio.run` interacts with anyio's
  `connect_unix` API: needs `anyio>=4` (which we have via host pyproject
  but not via the in-container `fxtr_service_runner` deps). Add `anyio>=4`
  to `fxtr_service_runner/pyproject.toml`'s `dependencies`.
- `anyio.connect_unix` requires the socket to exist before the container's
  main runs. The host listens before starting the container; the container
  may try to connect before the host has accept'd. Anyio's connect blocks
  until the listener accepts — but `socket.connect` racing the host's
  `listen()` is a known flake. The current `server_sock.listen(1)` in
  `container_runner.py` runs synchronously before the container starts, so
  this should be fine, but worth a re-check.

## Open issues / things to revisit

- `tests/old_broken/test_container_*.py` — decide per-test whether to lift
  back into `tests/` (against the new shape) or leave in `old_broken/`. Don't
  block stage 2 on this; do it as part of step 8.
- Pre-existing slips elsewhere in the tree have `requires-python = ">=3.14"`
  / a `>=3.14` default in `prepare.py` / a `>=3.14` example in
  `fxtr_sandbox_types/entities.py`. Not stage-2 work, but worth fixing
  alongside this change so the stage-2 Dockerfile's `python:3.12-slim`
  base stays consistent with the package set.

## Update CODE_OVERVIEW.md

Final step (also bumped after major sub-features per CLAUDE.md guidance).
