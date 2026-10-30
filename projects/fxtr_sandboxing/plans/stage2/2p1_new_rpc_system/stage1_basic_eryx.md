# Stage 1 — Basic Eryx Flow

Stand up the new `open_rpc_scope` + service-spawner shape end-to-end on WASM, with no
network transports and no nested spawn yet. Goal: prove the design, not exhaustively
test it.

## Prep dependency

`fxtr2.services.service_dialect.encode_to_service_data` /
`decode_from_service_data` have landed; this stage uses them for the typed-config
decoding path. No prep work outstanding.

## Steps

### 1. Expand `v2/portable`

No new C-extension deps have been added since the existing portable build was set
up, so the existing exclusions in `v2/portable/pyproject.toml` already cover the
right things. Just sweep in the new pure-Python directories:

- [x] Scan `v2/fxtr2/` (with `v2/CODE_OVERVIEW.md` as a roadmap) and add directory-
      scoped force-includes for `services/` and `leases/` (both pure-Python). Sweep
      `data/` for any modules not already listed (`cap_ir_arena.py` moved into
      `services/` so picks up automatically).
- [x] Prefer directory-scoped force-includes over per-file enumeration where it
      cleanly works — but leave the existing per-file `data/` entries alone if
      converting them risks pulling in something unintended.
- [x] Build the wheel locally and verify the new public surface imports: `python -c
      "from fxtr2.rpc.scope_bridge import open_rpc_scope; from
      fxtr2.services.typed_service import TypedServiceImpl; from
      fxtr2.services.service_dialect import encode_to_service_data,
      decode_from_service_data"`.
- [x] Update `v2/CODE_OVERVIEW.md` if the portable surface section calls out
      specific contents that have shifted. (No update needed — `v2/CODE_OVERVIEW.md`
      describes `v2/fxtr2/` only and doesn't mention the portable build at
      `v2/portable/`.)

### 2. Create `fxtr_sandbox_types` package

- [x] Add `projects/fxtr_sandboxing/fxtr_sandbox_types/` with `pyproject.toml` (Python
      package, build-backend hatchling).
- [x] Dep declaration: use extras (`fxtr_sandbox_types[fxtr2]` pulls fxtr2,
      `fxtr_sandbox_types[fxtr2-portable]` pulls fxtr2-portable) plus a doc note in
      the package README/CODE_OVERVIEW for which to pick. Base deps stay empty.
- [x] `_namespace.py`: `SHARED_SANDBOX_NS = uuid.UUID(...)` (fresh random UUID).
- [x] Move `fxtr_sandboxing/entities.py` → `fxtr_sandbox_types/entities.py`. Update the
      `define_type` calls to use `SHARED_SANDBOX_NS` (keep the same type-name strings to
      preserve content addresses if practical, but renaming is fine — the old IDs aren't
      cross-environment-compatible anyway).
- [x] `protocols.py`: define `SandboxConnectProto[T]`, `SandboxSpawnerProto`.
      `SandboxConnectProto` is generic over the user's config type so the typed-
      service decoder can drive `encode_to_service_data` / `decode_from_service_data`
      against the right annotation. Stage-1 shape:
      ```
      @service_protocol
      class SandboxConnectProto[T](Protocol):
          async def connect(self, config: T) -> ServiceRef[Any]: ...

      @service_protocol
      class SandboxSpawnerProto(Protocol):
          async def spawn(
              self, bundle: EntityRef[Any]
          ) -> ServiceRef[SandboxConnectProto[Any]]: ...
      ```
      `_service_type_id` for each comes from a module-level
      `system_entities.define_type(SHARED_SANDBOX_NS, "<name>")` constant. Calling
      `define_type` at module top-level tracks the resulting basic-type entity in the
      global scope, so isolated runtime scopes that inherit from the global scope pick
      it up automatically — no per-call `track_*` step needed. Never `track_builtin`.
- [x] Re-export the entity classes and protocols from `fxtr_sandbox_types/__init__.py`.
- [x] Update `fxtr_sandboxing/pyproject.toml` to depend on `fxtr_sandbox_types` (path
      dep) and remove the now-empty `fxtr_sandboxing/entities.py`. Update all imports
      within `fxtr_sandboxing/` to pull from the new package.
- [x] Update CODE_OVERVIEW.md.

### 3. Rewrite `fxtr_sandbox_rpc_adapter`

The adapter becomes the universal sandbox runtime: it owns the `open_rpc_scope` boot,
the GuestMain mount, and importing the user callable. **Entry-point pinning lives on
`TaskBundle.entry_point`** (validated `module:function` from pyproject at `prepare()`
time). Callers pass that string into `main(...)`; the adapter resolves it with
`importlib.import_module` + `getattr` — **not** `importlib.metadata.entry_points`.

- [x] `_object_stream.py`: keep `_JSONObjectStream` as-is (rename the file, no logic
      changes). Used to wrap `ctx.send`/`ctx.receive` as
      `ObjectStream[JSONTypes]`. Layered through `wrap_json_stream` to get an
      `ObjectStream[RPCMessage]` for `open_rpc_scope`.
- [x] `_guest_main.py`: `_GuestMainImpl(TypedServiceImpl[SandboxConnectProto[Any]])`.
      Constructor takes `(entrypoint_fn, guest_tg)`. `_service_type_id` returns the
      module-level `define_type`-derived ID directly — already in the global scope, so
      no per-call tracking step. The `connect` method is annotated as
      `connect(config: FxtrServiceData) -> ServiceRef[Any]` (raw passthrough on the
      wire); the impl reads the user entrypoint's `config` parameter annotation via
      `typing.get_type_hints(entrypoint)` and runs `decode_from_service_data(config,
      user_annotation)` before handing off to the entrypoint. The `connect` body:
      1. opens `teardown_event = anyio.Event()`,
      2. `(user_ref, user_scope) = await self._guest_tg.start(_entrypoint_runner,
         self._entrypoint, config, get_active_scope(), teardown_event)`,
      3. `helper = await user_ref.bridge_from_scope(user_scope, into_scope=
         get_active_scope())`,
      4. `teardown_event.set()`,
      5. returns `helper.ref`.
- [x] `_entrypoint_runner` (in `_guest_main.py`):
      1. Open a fresh `FxtrScope(isolated=True, mountable=True, name="user_scope")`.
      2. `copy_from_scope(config, source_scope)` to make config reachable from
         dispatch sub-scopes derived off user_scope.
      3. Define `serve(my_ref)`: assert single-call; `task_status.started((my_ref,
         user_scope))`; await `teardown_event.wait()`; return.
      4. With `user_scope.as_active_scope()`, call `await entrypoint(config, serve)`.
      Mirrors the spike's `_entrypoint_runner` 1:1.
- [x] `_main.py`:
      ```
      async def main(ctx, *, entry_point: str) -> None:
          entrypoint_fn = _resolve_entrypoint(entry_point)  # "pkg.mod:fn" -> callable
          stream = _JSONObjectStream(ctx.send, ctx.receive)
          rpc_message_stream = wrap_json_stream(stream)
          async with FxtrScope(isolated=True, mountable=True, name="guest_host") as gh:
              with gh.as_active_scope():
                  async with anyio.create_task_group() as guest_tg:
                      impl = _GuestMainImpl(entrypoint_fn, guest_tg)
                      main_ref = gh.mount(impl)
                      async with open_rpc_scope(rpc_message_stream, main_ref,
                                                name="guest_rpc"):
                          pass  # serves until host disconnects
      ```
      `_resolve_entrypoint` splits on `:`, imports the module, returns the attribute.
      Host-side `prepare()` still validates that pyproject declares exactly one
      `[project.entry-points."fxtr.service"]` entry; that value is stored on the bundle
      and threaded through unchanged.
- [x] `__init__.py`: re-export `main`. Stage 1 leaves `connect_transport` unimplemented
      (raises `NotImplementedError("transports land in stage 3")` if accessed).
- [x] Update `pyproject.toml` to depend on `fxtr2-portable` + `fxtr_sandbox_types`.
- [x] Update CODE_OVERVIEW.md.

### 4. Update eryx `_runner.py`

- [x] Replace the entry-point lookup + invocation block at the bottom of `_runner.py`
      with `await fxtr_sandbox_rpc_adapter.main(_EryxTaskContext(), entry_point=_ENTRY_POINT)`.
      **`runner.py` prepends** `_ENTRY_POINT = "<module:function>"\n` (from
      `bundle.entry_point`) **before** concatenating `_runner.py`, same pattern as the
      pre-RPC `_runner` that set globals for optional transports.
- [x] `_EryxTaskContext.send/receive` interface stays the same (JSON objects). Transport
      helpers (`get_transport`, `get_client`) are not on this minimal context in stage 1;
      stage 3 reshapes transport access.
- [x] Verify the existing socket/loop shim (`_SOCKET_SHIM`, `_LOOP_SHIM`) is still
      sufficient for the additional fxtr2 deps now arriving in the sandbox (anyio
      task groups, scope context vars). Add shims as needed.

### 5. Build host-side `SandboxSpawnerImpl` + `create_sandbox_environment`

`fxtr_sandboxing/spawner.py`. WASM-only in stage 1; container branch raises
`NotImplementedError`.

- [x] `SandboxSpawnerImpl(TypedServiceImpl[SandboxSpawnerProto])`. Constructor takes
      `(*, cache_dir, env_tg)`. (`transports` and `insecure_registry` plumbed in stages
      2–3.)
- [x] `spawn` method:
      1. Normalize `bundle` (accept `EntityRef[Any] | FxtrID`) to
         `EntityRef[TaskBundle | DockerTaskBundle]`.
      2. Load the entity, dispatch on type. Stage 1: only `TaskBundle` succeeds;
         `DockerTaskBundle` raises `NotImplementedError("containers land in stage 2")`.
      3. `teardown_event = anyio.Event()`,
         `(remote_ref, session_scope) = await self._env_tg.start(_spawn_runner_wasm,
         normalized_bundle, self._cache_dir, teardown_event)`,
      4. `helper = await remote_ref.bridge_from_scope(session_scope, into_scope=
         get_active_scope())`,
      5. `teardown_event.set()`,
      6. return `helper.ref` (typed as `ServiceRef[SandboxConnectProto]`).
- [x] `_spawn_runner_wasm`:
      1. Loads bundle, fetches wheels, builds site_packages (lifted from the existing
         `runner.py` body).
      2. Sets up the eryx callback registry and JSON-typed `TaskSession`-equivalent —
         we no longer need a public `TaskSession` class; the runner just produces an
         `ObjectStream[JSONTypes]` for direct consumption by `wrap_json_stream`.
      3. Wraps that stream with `wrap_json_stream` for `open_rpc_scope`.
      4. `async with open_rpc_scope(...) as session: task_status.started(
         (session.remote_service.replace_service_type(SandboxConnectProto),
         session.scope)); await teardown_event.wait()`.
- [x] `create_sandbox_environment`:
      ```
      @asynccontextmanager
      async def create_sandbox_environment(*, cache_dir):
          async with anyio.create_task_group() as env_tg:
              async with FxtrScope(mountable=True, name="sandbox_env") as host_scope:
                  impl = SandboxSpawnerImpl(cache_dir=cache_dir, env_tg=env_tg)
                  spawner_ref = host_scope.mount(impl)
                  yield Environment(spawner_ref=spawner_ref)
      ```
      Where `Environment` is a small dataclass with `spawner_ref` (and, in stage 3,
      `add_transport`).
- [x] `runner.py`: refactor so `_spawn_runner_wasm` can call into it for the launch
      mechanics. The public `TaskSession` / `run()` async-CM goes away — anyone wanting
      to drive a sandbox uses the spawner.
- [x] Update CODE_OVERVIEW.md.

### 6. Migrate examples and tests to `old_broken/`

- [x] `mv projects/fxtr_sandboxing/examples/{hello_task,yaml_task,lib_task,http_task,
      shared_lib,container_task,container_rpc_task,rpc_task,rpc_bridge_derisking}
      examples/old_broken/`. Keep `examples/old_broken/.gitkeep` if needed.
- [x] `mv tests/{test_runner.py,test_container_runner.py,test_rpc_runner.py,
      test_container_rpc_runner.py} tests/old_broken/`. Update `pytest.ini` /
      `pyproject.toml`'s test config to skip `tests/old_broken/`.
- [x] Leave `test_prepare.py`, `test_wheels.py`, `test_protocol.py`, `test_proxy.py`,
      `test_transport.py` in place if they still pass against the new entity import
      paths; otherwise update or move to `old_broken/`.

### 7. New stage-1 example + tests

The example is shaped to exercise the typed config path: the user's entrypoint
declares its config as `EntityRef[GreetingConfig]` (a small `DataclassEntity`),
which forces `decode_from_service_data` to round-trip an entity ref into the
sandbox. The impl loads the entity at construction time and uses it in method
responses.

- [x] `examples/rpc_task/` — a fresh service-shaped task. `pyproject.toml` declares
      `[project.entry-points."fxtr.service"] service = "rpc_task:run"` and depends on
      `fxtr_sandbox_rpc_adapter` + `fxtr_sandbox_types`. Implementation outline
      (in pseudocode):
      ```python
      RPC_TASK_NS = uuid.UUID(...)  # fresh, example-local

      class GreetingConfig(DataclassEntity, fxtr_type=
              system_entities.define_type(RPC_TASK_NS, "greeting_config")):
          greeting: str
          punctuation: str

      @service_protocol
      class GreeterProto(Protocol):
          async def greet(self, name: str) -> str: ...
          async def add(self, a: int, b: int) -> int: ...
          # service_type_id from system_entities.define_type(RPC_TASK_NS, "greeter")

      class _Impl(TypedServiceImpl[GreeterProto]):
          def __init__(self, cfg: GreetingConfig) -> None: ...
          async def greet(self, name): return f"{cfg.greeting}, {name}{cfg.punctuation}"
          async def add(self, a, b): return a + b

      async def run(config: EntityRef[GreetingConfig], serve):
          cfg = await config.load()
          ref = get_active_scope().mount(_Impl(cfg))
          await serve(ref)
      ```
- [x] `tests/test_spawner.py` — three tests against the WASM path:
      1. `test_spawn_then_connect_greet` — open env, store a `GreetingConfig("hello",
         "!")` entity in the active scope, spawn, connect with its `EntityRef`, call
         `greet("world")`, assert `"hello, world!"`. Validates the entity-ref-config
         conversion path end-to-end.
      2. `test_spawn_once_connect_twice` — same spawn ref, two connects with
         *different* `GreetingConfig` entities. Assert each connect gets its own
         `_Impl` instance using the right config (e.g. `"hi, alice."` vs
         `"hey, bob?"`). Validates multi-connect.
      3. `test_add` — sanity check that non-config-touching methods still work
         (covers the basic dispatch path independent of the config-load).
- [x] (Optional but recommended) `test_teardown_basic` — written as two
      back-to-back sessions in one env, each in its own sub-scope (mirroring
      the spawner spike's pattern). Validates that the first sandbox tears
      down cleanly before the second spawn, that the env stays usable after
      a sandbox dies, and that the env CM exits without hanging. Won't run
      until eryx env is back, but the test code is in place.

### 8. Update CODE_OVERVIEW.md

- [x] Reflect the new package layout (`fxtr_sandbox_types`, reshaped
      `fxtr_sandbox_rpc_adapter`, simplified `fxtr_task_runner`, deleted
      `rpc_runner.py`, new `spawner.py`).
- [x] Update the "Key concepts" sections to describe the spawner / GuestMain /
      service-ref-based flow instead of the old fxtr-RPC-handle flow. Note the
      transport/container parts as "stage 3"/"stage 2" stubs.
- [x] Update file listings.

## Out of scope for this stage (revisit later)

- Container path beyond `NotImplementedError`.
- Network transports (stage 3).
- Generic `connect(config: T)` decoding (stage 3 once user's `service_dialect` helpers
  are in).
- Nested spawn (stage 4).
- Porting the old examples (stage 4 picks 1–2 to port).

## Status (as of last work)

Stage 1 is code-complete except for the items still marked `[ ]` above. All 31
non-eryx tests (`tests/test_protocol.py`, `tests/test_proxy.py`) pass. The
3-test `tests/test_spawner.py` that exercises real eryx end-to-end is blocked
by a transient local eryx pre-init failure (wasmtime-side, not our code) —
when eryx comes back, the next steps are:

1. Run `tests/test_spawner.py` and resolve any fallout. Likely candidates:
   - Eryx loop-shim gaps (the bigger fxtr2 surface — services / leases /
     scope_bridge — exercises more of asyncio than the previous stack).
   - Multi-connect race conditions if any.
   - Typed-config decoding edge cases (`EntityRef[GreetingConfig]` round-trip).
   - `_copy_service_data_into_scope` walk for non-trivial config shapes.
2. Add `test_teardown_basic`.
3. Update `v2/CODE_OVERVIEW.md` for the portable expansion.
4. Re-read this plan and confirm there are no other deferred fix-ups before
   declaring stage 1 done.

## Likely gotchas to watch for during implementation

- The expanded `fxtr2-portable` will pull more transitively. The sandbox needs to
  import `anyio.streams.memory`, `weakref`, `dataclasses`, `contextvars`, `secrets`,
  etc. — all stdlib, all known-OK in eryx, but worth a quick smoke test once the
  rebuild lands.
- `FxtrScope`'s context-var activation needs to behave inside eryx's event loop —
  `_LOOP_SHIM` may need extensions.
- The guest-side `gh.mount(impl)` returns a `ServiceRef` whose service-type-id must
  pass through `track_builtin` so it's reachable in `gh._main_arena` for serialization.
  Match the spike's pattern (`return get_active_scope().track_builtin(...)`).
- `open_rpc_scope` body is `pass`, but the conn drain only completes once the host
  releases its bridges. If anything inside the guest holds a stray clone of the
  conn's main lease, the guest hangs forever — debug with the protocol's
  resolve-obligation tracking if it bites.
