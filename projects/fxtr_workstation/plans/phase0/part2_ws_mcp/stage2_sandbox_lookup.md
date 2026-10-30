# Stage 2 — Sandbox machinery + read-only `lookup_entity`

Goal: agent can run sandboxed Python that reads the workstation's
entity catalog. No mutation pipeline yet — the sandbox is purely
observational. This is the first stage where the agent can actually
*do* something with the workstation.

## Scope

- Port the spike's eryx sandbox bootstrap into
  `fxtr_workstation_mcp/sandbox_factory.py`. Same shape: a long-lived
  `eryx.SandboxFactory` built from `fxtr_ws_mcp_sandbox_bridge`'s dep
  closure (`fxtr-sandbox-rpc-adapter[fxtr2-portable]`), wrapped in a
  `DirectSandboxEnvironment`. Built once at MCP startup.
- Establish the `_fxtr_mcp_builtin` interface for v1, but only with
  the read-only methods this stage:
  - `async lookup_entity(short_id: str) -> FxtrID`
  - `def get_service(short_id: str) -> ServiceRefStub`
  - `def get_id_for_service(stub: ServiceRefStub) -> str`
- Wire up `McpHostImpl` (a `TypedServiceImpl[McpHostProto]`) on
  `host_scope`. Stage-2 protocol surface:
  - `async lookup_entity(short_id: str) -> FxtrID` — delegates to the
    workstation `/rpc` connection's `get_entity`.
  - `async list_services(short_ids: list[str]) -> list[ServiceMeta]` —
    metadata-only convenience (returns workstation-tracked metadata
    for known ids; v1 just `(short_id, type_id)` per id, used
    internally to validate `get_service`).
- Add `set_working_dir(path)` validation: `preamble.py` must exist;
  `.fxtr_workstation.toml` must exist and parse; `packages/` is
  optional. Cache the parsed config.
- Add `run_script(script: str)` tool. End-to-end flow:
  - `ensure_connected()` (lazy connect).
  - Read `preamble.py`.
  - Build the per-call `_fxtr_mcp_builtin` closures (only the
    read-only ones this stage; queue containers for the
    write-side ones land empty in stage 2 and unused — they are
    *not* exposed yet).
  - Open a fresh sandbox via `env.open_sandbox(
      entry_point="fxtr_ws_mcp_sandbox_bridge.entry:run",
      volumes=[(plugin/scripts, /sandbox-stub),
               (<workdir>/packages, /local-pkgs) if present],
      sys_path_additions=["/sandbox-stub", "/local-pkgs"])`
  - Call `runner.run(RunnerConfig)` carrying `preamble_src`,
    `script_src`, `host_ref`. Return value (stage 2): an empty
    queued-ops list — the script can only observe, not declare.
- Sandbox-side `RunnerConfig` carries `host_ref:
  ServiceRef[McpHostProto]`. The sandbox's `_GuestImpl.run()`
  resolves it once at the top of the call.

## Sandbox bridge package skeleton

`sandbox/fxtr_ws_mcp_sandbox_bridge/` — same import-on-both-sides
pattern as the spike's `_fxtr_mcp_runner`. Files:

- `protocols.py`:
  - `WS_MCP_NS` UUID + `define_type` calls for `RUNNER_TYPE_ID`,
    `HOST_TYPE_ID`.
  - `McpHostProto` (host service surface). Stage-2 methods: just
    `lookup_entity(short_id) -> FxtrID`. Future stages will add
    `copy_entity_in(...)`, `register_service(...)`, etc.
  - `McpRunnerProto` — `async run() -> RunResult`.
  - `RunnerConfig` plain frozen dataclass: `preamble_src`,
    `script_src`, `host_ref: ServiceRef[McpHostProto]`. No
    workstation references; the host is the only capability.
  - `RunResult` plain frozen dataclass / TypedDict: `queued_ops:
    list[QueuedOp]` (stage-2 always empty; the wire shape exists so
    stages 3 / 4 don't need a protocol bump). Define `QueuedOp` as a
    tagged union over `kind` (export_entity / export_service /
    schedule_invoke / forget); empty types skeleton-only this stage.
- `entry.py` — `async def run(config: RunnerConfig) ->
  ServiceRef[McpRunnerProto]`. Mounts a `_GuestImpl(config)` on the
  active dispatch scope, returns its ref. Same shape as the spike.
- `script_runner.py` — verbatim port of the spike's
  `run_preamble_and_script`. No MCP-specific knowledge.
- `builtins.py` — closures-builder. Stage 2 exposes:

  ```
  def build_builtins(host_proxy: McpHostProto, ...):
      async def lookup_entity(short_id):
          return await host_proxy.lookup_entity(short_id)
      def get_service(short_id):
          return ServiceRefStub(short_id)
      def get_id_for_service(stub):
          return stub.short_id
      return {
          "lookup_entity": lookup_entity,
          "get_service": get_service,
          "get_id_for_service": get_id_for_service,
      }
  ```

- `_GuestImpl.run()` calls `build_builtins(...)` and forwards to
  `script_runner.run_preamble_and_script(...)`. Returns the
  (still-empty) `queued_ops` list.

## Host side: `McpHostImpl`

- `TypedServiceImpl[McpHostProto]`. Stored on `AppContext` along with
  its mount in `host_scope`.
- Method `lookup_entity(short_id)`:
  - Resolves the workstation main service in the active call_scope
    (via `app_ctx.rpc.main_ref.resolve(get_active_scope())` — the
    active scope is the host method's dispatch scope, a child of
    `host_scope` which `include`s the workstation `/rpc` user
    scope).
  - Calls `await ws_main.get_entity(short_id)`.
  - Returns the `FxtrID`. fxtr-RPC bridges it across the *sandbox*
    connection back into the sandbox's connect-dispatch scope.
- Method `list_services(short_ids)` — stage-2 stub returning empty
  metadata; full impl can defer to stage 5 if not needed.

## Scope topology details

```
workstation /rpc user scope (the open_rpc_scope inner scope)
  └─ host_scope (isolated, include=[ws /rpc user scope], mountable)
        ├─ McpHostImpl mount → host_ref (loadable in host_scope)
        └─ per-script open_sandbox call_scope
              └─ sandbox connect dispatch scope (server-side)
                    └─ _GuestImpl mount
```

`run_script` enters `host_scope.as_active_scope()` around
`open_sandbox(...)` so the per-call sandbox scope inherits both
host_scope and (transitively) the workstation `/rpc` user scope —
this is what makes `host_ref` encodable into the sandbox's
connect config.

## ServiceRefStub (sandbox-side)

Plain dataclass:

```
@dataclass(frozen=True)
class ServiceRefStub:
    short_id: str
```

v1 has no type information. We document that future versions will
carry a phantom `T` and validate at construction. `get_service` and
`get_id_for_service` are pure metadata.

## Tests (stage 2)

- `test_sandbox_factory.py` — bootstraps the factory, verifies a
  trivial sandbox open/close cycle. Mostly inherited from the spike;
  faster to copy + adapt than to rewrite.
- `test_lookup_entity_e2e.py` — integration test:
  - Starts a `WorkstationServer`.
  - Opens an `/rpc` connection from a *separate* test peer that
    `copy_entity`'s an entity into the workstation under
    `external_name="bg-data"`. Stays alive (anyio task) for the
    test's duration.
  - Boots the MCP lifespan against the workstation (in-process).
  - `connect()` then `open_session()`.
  - `set_working_dir(<test workdir with preamble.py + a script that
    does `data = await lookup_entity(<short_id>)` then asserts on
    its CBOR>)`.
  - Asserts the script returns successfully (queued_ops empty).
- `test_get_service_metadata.py` — focused: `get_service("s1")`
  returns a stub; `get_id_for_service(stub) == "s1"`. Pure local;
  no workstation hit needed.

## Implementation steps

- [x] Create `sandbox/fxtr_ws_mcp_sandbox_bridge/` package
      (pyproject.toml + protocols.py + script_runner.py +
      builtins.py + entry.py). Also ``sandbox/sandbox_runtime/``
      (the dep-closure stub for ``prepare()``); inner package
      directory must match the project name for hatchling's
      default file selection (renamed to
      ``fxtr_ws_mcp_sandbox_runtime``).
- [x] Add path-source deps on the bridge + sandbox-runtime + sandbox
      types in ``fxtr_workstation_mcp/pyproject.toml``.
- [x] Port `sandbox_factory.py` from the spike, retargeted at
      `fxtr_ws_mcp_sandbox_bridge`. Volume-mount the bridge's
      pyproject-root at ``/sandbox-stub`` (one level above the
      importable package directory) — same pattern as the spike's
      ``plugin/scripts/`` containing ``_fxtr_mcp_runner/``.
- [x] Add `host_impl.py` with `McpHostImpl`. The
      ``WorkstationConnection`` supervisor's ``_open_workstation_rpc``
      helper now resolves the workstation proxy in ``host_scope``,
      constructs ``McpHostImpl(workstation_proxy)``, mounts it on
      ``host_scope``, and stores the resulting ``host_ref`` on the
      ``WorkstationRpc`` data bundle.
- [x] ``host_scope`` already had ``include=[rpc_session.scope]``
      from stage 1.
- [x] Add lazy ``SandboxEnvSupervisor`` (mirrors
      ``WorkstationConnection``'s supervisor pattern) in
      ``sandbox_env.py`` so the eryx factory bootstrap doesn't run
      on every test — only ``run_script``-using tests pay the cost.
      Bootstrap scope must ``include=[GLOBAL_SCOPE]`` so type-
      descriptor entities lift correctly during ``store_cbor``.
- [x] Add `run_script` and upgrade `set_working_dir` to require
      `preamble.py` + summarize `packages/`.
- [x] Stage-2 integration test
      (``tests/test_run_script_lookup_entity.py``) plus a
      ``tests/_peer.py`` helper for opening a separate ``/rpc``
      connection. End-to-end verifies the full
      sandbox→host→workstation lookup chain bridges a ``FxtrID``
      back to the script.
- [x] Update `CODE_OVERVIEW.md`.

## Surprises landed during implementation

- **fxtr2's ``bridge_from_scope`` API change**: the method used to
  return a helper with ``.ref``; current code returns ``None``
  (the ref passed in is now valid in the target scope after the
  call). ``fxtr_sandboxing/direct_launcher.py`` was using the old
  shape; fixed in-place to use the original ``connect_ref``
  directly.
- **Bootstrap scope needs ``GLOBAL_SCOPE``** in its ``include`` so
  type-descriptor entities can lift during ``wheel_pkg.store()``.
- **Hatchling default file selection** requires the package
  directory name to match the project name (with hyphens →
  underscores). Renamed our sandbox_runtime stub's inner dir to
  match.
- **Volume mount path = pyproject root, not its parent**. The
  importable package is ``<pyproject_root>/<package_name>/``;
  mounting the parent of that pyproject root puts the package
  one level too deep on sys.path.

## Resolved (from design discussion)

- `lookup_entity` returns a bare `FxtrID` (v1). The agent wraps with
  `EntityRef(id, T)` if they want a typed handle. We may extend later
  to return `EntityRef[Any]` directly — easy to do additively.
- Unknown short_ids surface as a normal Python exception inside the
  sandbox; the host does not fail.


## Annotated Reflection (by Claude instance that worked on stage 2, with human notes in **bold**)


  What I think is genuinely solid

  - Supervisor task pattern. Owns its own scope chain in one task; tools dispatch via queues / typed
  methods. Stage 3's reconnect loop is a clean addition — while not self._shutdown.is_set(): try:
  ...except: backoff around the existing inner body.
  - Two-layer Exception/BaseException handling on jobs. Will reuse cleanly for ScriptJob/EventJob.
  - Reader → per-request streams + long-lived session-event stream. Stage 3's event handler just
  consumes the existing event stream; no routing changes needed.
  - Worker-queue serialization model. ScriptJob and EventJob slot in naturally — script execution and
  follow-up mutations both serialize through the same drain.
  - Lazy SandboxEnvSupervisor — paying ~3s only on first run_script keeps stage-1 tests fast and the
  pattern is identical to WorkstationConnection's.

  What I'd want to revisit before stage 3 makes it harder

  1. McpHostImpl needs to become per-script, not per-connection. Today it's mounted once on host_scope
  (lifetime = whole connection) with just a workstation proxy. Stage 3 adds copy_entity_in(slot_key,
  entity_id) (needs to know which script_scope to copy into) and read_state_slot(slot_key) (needs the
  state mirror). The cleanest model:

    - Cache workstation_proxy = await workstation_ref.resolve(host_scope) once at connection startup.
    - Per run_script: open script_scope as a child of host_scope, construct a fresh
  McpHostImpl(workstation_proxy, script_scope, state_mirror, queued_ops_collector), mount on
  script_scope, get a per-script host_ref to put in RunnerConfig.
    - On script exit, script_scope drains and the per-script impl + ref go with it.

  Stage 2 happens to work with the persistent mount because lookup_entity is stateless. Doing the
  refactor now (before stage 3 writes more host methods on top of the persistent mount) keeps the cost
  contained. I'd flag this as the most important thing to fix before stage 3.

    **NOTE: Agreed, worth fixing.**

  2. In-flight invoke registrations outlive the ScriptJob. Per the design: invoke-arg RPC registrations
   have to stay alive until the matching InvokeResultEvent lands and is promoted into state. That's
  after ScriptJob completes — so we can't use AsyncExitStack for these (which couples lifetime to a
  single task's scope). Need imperative lifetime management: call workstation.copy_entity(...)
  directly, stash the release_cb_ref in connection.in_flight_invoke_regs[slot], release it from the
  EventJob handler.

  2. export_entity registrations are simpler: AsyncExitStack inside the ScriptJob works fine (released
  on commit since session-pin holds the entity).

  2. Two different lifetime models in the same end-of-script flow. Worth being explicit in the
  stage-3/4 plan about which is which.

    **NOTE: I think this is a misunderstanding. Invoke-arg RPC registrations do _not_ have to stay alive until their result lands. They only have to stay alive until the MutationCommittedResponse is received, and we wait on this per-script anyways, so it should be fine to just use normal async context managers. We should _not_ use imperative lifetime management for any registrations, registrations should always have local structural lifetimes.**

  3. State-mirror placement. Where does data_by_key + tracked_entities + tracked_services live? My
  current instinct: a SessionMirror dataclass held on WorkstationConnection, replaced atomically on
  each commit. Reads from host RPC dispatches are GIL-atomic dict lookups. Writes happen only inside
  the worker task. Worth nailing down before writing code.

    **NOTE: both reads and writes should only happen inside a worker task which is on a single queue so I think this should be straightforward.**

  4. The direct_launcher.py patch. Tracking it as a thing — currently sitting outside our package. The
  cleaner path would be: revert my patch and fix it as a standalone PR to the sandboxing package, since
   it's a real bug there that probably breaks the spike too.

    **NOTE: this is fine, I've already broken it into a separate commit.**

  Smaller things worth noting

  5. The two-package-deep sandbox bridge layout. Still on the watch-list; you said leave for now.
  Mentioning so it doesn't get forgotten.  **Fine to keep for now**
  6. No isolated tests for pure-local sandbox builtins (get_service / get_id_for_service). Trivial but
  the integration test only exercises lookup_entity. Cheap to add.   **OK to have either isolated or unit tests**
  7. expose_entity async-CM ordering for invoke args — once we know we want imperative lifetime per
  (2), this resolves itself but the API friction (helpers built around async-CM use) suggests we'll
  write a thin imperative wrapper alongside the helper.  **I think superseded above**
  8. The Future class lives in ws_connection.py. It's a generic primitive (one-shot result holder);
  when stage 3 introduces ScriptJob/EventJob and possibly other future-shaped patterns, worth lifting
  to a _util.py or similar.
  9. No tests for the supervisor's reader/worker isolation — e.g., that an event arriving during a
  job's await doesn't deadlock anything. We've got the integration tests that exercise these paths in
  aggregate, but no focused unit test of the routing. Stage 3 introduces real follow-up mutations from
  events, which is the case where this isolation actually matters; we should write the focused test
  then.   **OK to skip these tests for now**
  10. Reconnect cleanup mechanics — the design says "discard in-flight, let workstation re-deliver" but
   the actual sequence is:
    - Pending request streams close → awaiters get ConnectionLost
    - Job queue drains → unprocessed jobs get failed completions
    - In-flight invoke registrations on the OLD /rpc connection are gone (RPC connection closed); the
  workstation has already moved any committed-but-not-acked results into its event log, which the new
  subscription will replay
    - We re-resolve workstation_proxy and re-mount McpHostImpl (or refactor as in 1)
    - All session-side state mirror is rebuilt from the resubscription's snapshot

       **This seems acceptable.**
