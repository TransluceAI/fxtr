# fxtr_sandboxing Code Overview

A prototype for sandboxed Python execution using Eryx (CPython 3.14 in
WASM) and Docker containers. Tasks are content-addressed bundles built on
fxtr2; communication between host and sandbox uses the fxtr2 RPC bridge from
`open_rpc_scope` plus the typed-service / `ServiceRef` system. See
`plans/stage2/2p1_new_rpc_system/overview.md` for the overall design.

There are three live (git-referenced, nix-built) sandbox modes:

- **nix WASM** — `NixWasmServiceDefn` references a `fxtr.services.<name>`
  flake attr (`kind = "wasm"`); the spawner builds its `sitePackages` and
  runs it in an Eryx CPython 3.14 sandbox; communication via JSON-typed
  callbacks.
- **nix container** — `NixContainerServiceDefn` references a
  `kind = "container"` attr; the spawner builds its nix2container `image`,
  delivers it via skopeo, and runs it; communication via a per-spawn UDS.
  See `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md`.
- **nix pyodide** — `NixPyodideServiceDefn` references a `kind = "pyodide"`
  attr; the spawner builds its `sitePackages` (which may include PEP 783
  `pyemscripten_*_wasm32` binary wheels) + `runtimeBundle` (pyodide assets +
  guest bootstrap) and runs pyodide CPython-wasm inside a permission-locked
  deno subprocess on the host (deno from PATH — an optional host dependency);
  communication via a per-spawn UDS bridged through the deno driver.
  V8-isolate-grade isolation (eryx trust tier, below container-class — the
  deno permission flags are the perimeter; guest python can call arbitrary
  JS inside the isolate). See
  `plans/stage2/2p5_deno_py_sandbox/plan.md`.

Plus one deprecated in-band-bytes mode (superseded by the above):

- **WASM** — `TaskBundle` runs in an Eryx CPython 3.14 sandbox; communication
  via JSON-typed callbacks.

(The deprecated *container* in-band modes — `DockerTaskBundle` and
`PyContainerTaskBundle` — were removed in
`projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage8_podman.md`;
their live replacement is `NixContainerServiceDefn`.)

## Key concepts

### Service-shaped sandbox tasks

Every sandbox task is a service author. Its `pyproject.toml` declares a single
`[project.entry-points."fxtr.service"]` entry, e.g.:

```toml
[project.entry-points."fxtr.service"]
service = "rpc_task:run"
```

The entry-point function takes one argument and returns a `ServiceRef`:

```python
async def run(config: EntityRef[GreetingConfig]) -> ServiceRef[GreeterProto]:
    cfg = await config.load()
    return get_active_scope().mount(_Impl(cfg))
```

The active scope at call time is the `connect` dispatch scope on the sandbox
side — args bridged into it (FxtrIDs, EntityRefs, ServiceRefs) are reachable
from anywhere reachable through the mount. The framework chain-bridges the
returned ref back to the host caller; when the caller releases, the dispatch
scope drains and the impl becomes unreachable.

The `config` parameter can be any `FxtrServiceData`-shaped value (FxtrID,
EntityRef, ServiceRef, primitives, dicts/lists/tuples thereof, dataclass
entities). Its declared annotation is inspected at dispatch time and
`decode_from_service_data` decodes the wire payload against it. Tasks that
need post-teardown cleanup can register an `on_end` callback on
`get_active_scope()._token` from inside their impl (same lease-driven pattern
the network authorizer uses for its witness).

### Spawn → connect

The host opens a sandbox environment, gets a `ServiceRef[SandboxSpawnerProto]`,
and uses `spawn(bundle)`, **then** `replace_service_type(SandboxConnectProto[MyConfig, MyProto])`
(or `SandboxConnectProto[FxtrServiceData, UnknownService]` for the untyped wire shape),
then `resolve().connect(config)`:

```python
async with (
    create_sandbox_environment(cache_dir=...) as env,
    # The env host scope is isolated (parents: GLOBAL_SCOPE only) and is
    # not active in the body — open a bridging scope that includes it
    # (plus, implicitly, your ambient scope) to resolve against.
    FxtrScope(include=[env.host_scope]),
):
    spawner = await env.spawner_ref.resolve()
    sandbox_ref = await spawner.spawn(my_bundle_ref)
    sandbox_ref = sandbox_ref.replace_service_type(
        SandboxConnectProto[MyConfig, MyProto],
    )
    sandbox = await sandbox_ref.resolve()
    user_ref = await sandbox.connect(my_config)
    user = await user_ref.resolve()
    result = await user.some_method(...)
```

`spawn` accepts an `EntityRef[NixWasmServiceDefn]` / `EntityRef[NixContainerServiceDefn]`
(git-referenced services the spawner builds from a git commit, after
checking the attr's `kind`), the kind-agnostic `EntityRef[NixServiceDefn]`
(same triple, no asserted kind — the spawner evals the attr's `kind` and
dispatches to the matching arm; use it when the caller holds only the
reference triple and the flake is the source of truth for its own kind),
or the deprecated `EntityRef[TaskBundle]`
(WASM) and dispatches on bundle type; caller-side ergonomics are
identical. It returns a
`ServiceRef[SandboxConnectProto[OverrideThisTypeBeforeUsing, OverrideThisTypeBeforeUsing]]`
— callers **must** replace with a concrete `SandboxConnectProto[T, U]` before
`resolve` / `connect`. The dialect then encodes `MyConfig` (any
mashumaro-acceptable shape — plain dataclasses, primitives, refs, etc.) and
`connect` returns a pre-typed `ServiceRef[MyProto]` when `U` is `MyProto`.
Untyped callers use `SandboxConnectProto[FxtrServiceData, UnknownService]` and
`replace_service_type` the connect result. Multiple `connect` calls on the
same `sandbox_ref` are valid — each gets its own dispatch scope and a fresh
entrypoint invocation. Lifetimes ride on lease bridges: when the caller
releases the chain-bridged user ref, the guest-side dispatch scope drains;
when nothing is left holding the spawn ref, the sandbox process tears down.

The `SandboxConnectProto` the caller resolves is **not** the guest's
raw main service — it's a small host-side adapter
(`_SpawnConnectWrapperImpl`, mounted on the spawn-dispatch call_scope)
that wraps the guest's internal `SandboxGuestImplProto`. The guest's
wire method is `connect_with_bundle_provenance(config, bundle)`: the
wrapper supplies the bundle ref (the same `EntityRef` the user passed
to `spawn`) on every call, so user code can never forge it. See
*Sandbox provenance* below.

### Nested spawn

Nested sandboxes aren't actually nested at the runtime layer: every
sandbox an `Environment` spawns is a sibling under that env,
regardless of who asked for the spawn. So a
`ServiceRef[SandboxSpawnerProto]` can be bridged into a sandbox
(typically via its `connect` config); the sandbox can call
`spawner.spawn(inner_bundle)`, `replace_service_type(SandboxConnectProto[...])`
on the result, resolve it, and the host's
`SandboxSpawnerImpl.spawn` runs on env_tg the same way as if the
host had called it directly, opening the inner sandbox as a sibling
under env. There is no proxying-up logic — RPC stays out of the
data path between inner and host.

Capability flow rides on `config`. To give an inner sandbox HTTP
access, the outer holds a
`ServiceRef[NetworkHandleAuthorizerProto]` (bridged in via its own
config) and threads it through `inner_sandbox.connect(authorizer_ref)`.
The inner can mint+redeem tokens against the env's
`TransportConnectionServer` directly; the outer isn't on the data
path. Capability-style permissions fall out of this — an inner can
only redeem authorizers explicitly bridged into it.

See `examples/nested_relay_task` for an end-to-end demo (WASM outer that
per-call spawns an inner sandbox and relays HTTP through it).

### Sandbox provenance

Every `connect()` call into a sandbox guest binds a per-call
provenance c-node to the dispatch scope where the user entrypoint
runs. Anything the entrypoint stores / mounts from that point on
carries the c-node as its `_provenance_parent`, propagating through
fxtr2's automatic provenance machinery (see
`v2/plans/phase3/stage9_provenanced_scopes/`).

The c-node's d-node is a `SandboxProvenanceContext` entity carrying:

- **`bundle: EntityRef[UnknownEntity]`** — the bundle entity the host
  used to spawn this guest. Threaded in by the host adapter
  (`_SpawnConnectWrapperImpl`), **not** by user config — users can't
  forge a different bundle.
- **`config_snapshot: FrozenFxtrCbor`** — an immutable snapshot of
  the `connect` config. Same structure as the input
  `FxtrServiceData` tree, except every `ServiceRef` leaf has been
  replaced by an EntityRef-shaped pointer to a stored
  `ServiceConfigReplacementEntity` (a leaf substitute carrying the
  service's `$fxtr.type` and `$fxtr.ctx`). The chain continues across
  sandbox boundaries: an authorizer or inner-sandbox ref bridged
  into the guest brings its own provenance marker through `$fxtr.ctx`,
  and that c-node ends up reachable from this c-node's payload.

Guest-side flow (in `fxtr_sandbox_rpc_adapter/_guest_main.py` /
`_provenance.py`):

1. The host invokes
   `SandboxGuestImplProto.connect_with_bundle_provenance(config,
   bundle)`. The wire arena bridges `config` (with all referenced
   FxtrIDs / ServiceRefs) and `bundle` into the per-call dispatch
   scope.
2. `walk_config_for_provenance(config)` runs a single async pass
   over the FxtrServiceData tree: ServiceRef leaves resolve as
   untyped proxies, the guest invokes `$fxtr.ctx` + `$fxtr.type`,
   builds a `ServiceConfigReplacementEntity`, stores it, and emits
   a `RawStrongFxtrID` for the snapshot. EntityRef-shaped leaves
   (i.e. user-provided FxtrIDs with tag `d` / `n`) are loaded as
   `UnknownEntity` in the same pass and rejected with
   `SandboxProvenanceEscapeError` if their `$fxtr.type` matches
   `SERVICE_CONFIG_REPLACEMENT_TYPE_ID` (escape-safety check —
   top-level only, no recursion into entity contents).
3. The guest builds `SandboxProvenanceContext(bundle=bundle,
   config_snapshot=snapshot)`, converts to cbor via
   `entity._fxtr_convert_to_cbor()`, and mints a c-node via
   `scope.create_c_node(cbor)`.
4. The c-node is bound as the dispatch scope's provenance marker via
   the private `call_scope._apply_context_marker_override(
   provenance_context=c_node, provenance_context_from_scope=call_scope)`
   (TODO: expose as a public scope method; tracked in
   `plans/stage2/2p3_sandbox_provenance/plan.md`). Inheritance of this
   marker into subsequent child dispatch scopes works correctly because
   v2's `_track_provenance_context_from` now mints the read-side lease
   via the new derived scope's ancestor cap-bridges (rather than
   cloning the parent's already-discarded `_main_lease`).
5. The user entrypoint runs in that now-provenanced scope. Its
   returned `ServiceRef` and any entities it stores carry the
   c-node as `_provenance_parent`.

Each `connect()` call builds a fresh c-node (services in config may
vary per call). The c-node id is content-addressed, so replaying
identical `(bundle, config)` pairs yields identical provenance.

### Lifetime nesting

```
host application scope
  └─ create_sandbox_environment (env_tg + exit_stack + spawner mount;
     the host scope is ISOLATED — parents: GLOBAL_SCOPE only — and not
     active in the body: consumers bridge to env.host_scope explicitly)
       └─ spawn() call_scope (caller's scope)
            └─ _spawn_runner_{wasm,nix_wasm,nix_container,nix_pyodide} task on env_tg
                 └─ open_rpc_scope conn to one guest process (isolated —
                    coupled to the call_scope only via the explicit
                    raw_guest_ref bridge, never by scope parentage)
                      └─ guest's GuestMain mounted on guest_host
                           └─ guest-side connect dispatch scope
                                └─ user's mount of their impl
```

Drain on each level releases its bridges; the next level above unblocks.

### `SandboxServiceContext` and the two JSON ObjectStream impls

Each runtime hands `fxtr_sandbox_rpc_adapter.main(ctx, *, entry_point=...)`
its own concrete impl of `SandboxServiceContext` (a `typing.Protocol` in
`fxtr_sandbox_types/service_context.py`). The protocol exposes
`ctx.main_protocol_stream: ObjectStream[JSONTypes]` (the RPC connection)
and `ctx.open_transport_stream(token: str) -> anyio.abc.ByteStream` (one
byte-stream connection through a host-side network route, redeemed
by a single-use token).

The two stream impls live in `fxtr_sandbox_types`:

- **`JSONObjectStream`** — wraps a `(send, receive)`-callable pair (used
  for eryx, where the channel is JSON-typed callbacks rather than a byte
  stream). EOF is signaled in-band via the `["!", "eof"]` escape frame.
- **`SocketJSONStream`** — wraps an `anyio.SocketStream` directly (used
  for containers, where the channel is a real UDS byte stream).
  Length-prefixed JSON on send; native `anyio.EndOfStream` propagation
  on a clean peer half-close; `send_eof()` does a real UDS half-close.
  Mid-frame truncation surfaces as `anyio.BrokenResourceError`.

The runtime's concrete `SandboxServiceContext` impl picks the appropriate
stream:

- WASM: `EryxServiceContext` (in `fxtr_sandboxing/_runner.py`) wraps
  `JSONObjectStream` over the eryx-injected callbacks.
- Container: `ContainerServiceContext` (in
  `fxtr_service_runner/service_context.py`) wraps `SocketJSONStream` over
  the UDS the in-container shim opened to the host.

### Service-type IDs

All cross-process protocols use `system_entities.define_type(NS, "name")` with
`define_type` called at module top-level. The resulting `FxtrBasicType` entity
lands in `GLOBAL_SCOPE`; downstream isolated scopes pick it up via the
lowlevel-mount lift path. Never `track_builtin`. Per-package UUID namespaces:

- `SHARED_SANDBOX_NS` (`fxtr_sandbox_types/_namespace.py`) for cross-process
  protocols that the host and sandbox both speak.
- Each example or downstream user defines its own UUID for its own protocols
  (e.g. `RPC_TASK_NS` shared between `examples/rpc_task` and the
  `container_greeter` test fixture).

### Container task entry-point discovery

WASM bundles pin `entry_point` on the `TaskBundle` entity at `prepare()`
time; the host injects it as a global before executing the runner script.
Containers are opaque to the host — the image is an OCI digest, not Python
source — so entry-point resolution happens *inside* the container.
`fxtr_service_runner.__main__` accepts an optional CLI argument
(`module:function`); if absent, it falls back to discovering a single
`fxtr.service` entry point from installed packages. The
`fxtr_container_task.toml` `[run] cmd_override` is the user-controllable
knob for pinning a specific entry point when the image bakes in multiple
services.

### Network transports

Sandboxes get controlled byte-stream access to host-registered network
routes via a token-keyed capability system.

Host side:

- The env owns one `TransportConnectionServer`
  (`fxtr_sandboxing/transport_server.py`) — a token table keyed on
  unguessable hex strings, mapping each token to a sync
  `TransportHandler = Callable[[ByteStream], None]`. `consume_token`
  is a sync atomic pop-or-raise (`BadTransportTokenError`); a
  `threading.Lock` guards mutations since the eryx callback path
  hits the table from a thread.
- `Environment.add_transport(route)` registers an HTTP route + opens
  an `aiohttp.ClientSession` (closed on env teardown via the env's
  `AsyncExitStack`); `make_http_route_handler(route, session, env_tg)`
  builds the handler — sync, `start_soon`s `proxy_connection` on the
  env task group.
- `Environment.add_passthrough_transport(*, allowed_hosts=None,
  blocked_hosts=None)` registers a host-agnostic HTTPS passthrough
  authorizer. Unlike `add_transport`, each request's `Host:` header
  picks the upstream — one authorizer ref reaches arbitrary hosts.
  Optional `allowed_hosts` / `blocked_hosts` filter (mutually
  exclusive); no filter at all means "any host". Always HTTPS
  outbound; uses `make_passthrough_http_handler` + the same
  `aiohttp.ClientSession` pooling.
- `Environment.add_raw_transport(handler)` accepts a
  user-supplied sync handler directly (for non-HTTP backends, raw
  byte protocols, or tests). All three return a
  `ServiceRef[NetworkHandleAuthorizerProto]` mounted on the env's
  host scope.
- `NetworkHandleAuthorizerImpl`
  (`fxtr_sandboxing/network_authorizer.py`) is the impl behind the
  ref. `get_transport_token()` mints `secrets.token_hex(32)`,
  registers it in the token table, and returns `(token, witness_ref)`
  where the witness is mounted on the dispatch scope; a
  `register_end_callback` on `call_scope._token` revokes any
  unredeemed token when the scope drains. Lease-driven, immune to GC
  cycles. Once a token is redeemed (= consumed from the table), the
  late revoke is a no-op.

Sandbox side:

- `fxtr_sandbox_rpc_adapter.connect_transport(token)` — free function
  that pulls the active `SandboxServiceContext` out of a contextvar
  populated by `main()` and calls `ctx.open_transport_stream(token)`.
  Returns a raw `anyio.abc.ByteStream`; caller owns lifecycle.
- `fxtr_sandbox_rpc_adapter.get_client(authorizer_ref)` — primary
  HTTP surface. Builds an `httpx.AsyncClient` whose backend mints +
  redeems a token per connection. **Takes a ServiceRef, not a
  resolved proxy** — the backend resolves it fresh inside each
  `connect_tcp` child scope so the witness's lifetime tracks the
  per-connection scope. Gated on the `[http]` extra; raises
  `RuntimeError` with an install hint if `httpx` / `httpcore`
  aren't available.

Runtime-specific dialing:

- **Eryx**: four byte-level callbacks (`_transport_connect`,
  `_transport_send`, `_transport_recv`, `_transport_close`,
  `_transport_send_eof`). The connect callback runs sync in eryx's
  thread, calls `consume_token`, makes a socketpair, and queues
  `(proxy_sock, handler)` for a small launcher task to pick up,
  wrap as a `UNIXSocketStream`, and hand to the handler sync.
  `EryxServiceContext.open_transport_stream` (in `_runner.py`)
  wraps the four callbacks as an `anyio.abc.ByteStream`
  (`_EryxTransportByteStream`); `send_eof` shuts down the host-side
  socketpair end's write side via `_transport_send_eof`.
- **Container**: a multiplexed UDS at
  `/run/fxtr/transport.sock`. Each new connection sends a
  length-framed `{"token": "..."}` routing header; the host's
  `handle_transport_conn` reads it, calls `consume_token`, writes
  back `{"ok": true}` / `{"ok": false, "error": ...}`, and on
  success hands the connection to the handler sync.
  `ContainerServiceContext.open_transport_stream` is the dial side
  (in `fxtr_service_runner/service_context.py`).

Routing-header read/write helpers live in
`fxtr_sandbox_types/transport_routing.py` (shared between host and
container shim).

**Resolution-scope footgun.** The lifetime of resources returned by a
service call is bounded by the *resolution scope* of the service ref
the caller called against, not by the active scope at the call site.
This means: pass authorizers around as
`ServiceRef[NetworkHandleAuthorizerProto]`, never as already-resolved
proxies. Resolve in a short-lived child scope per mint. `get_client`'s
backend follows this rule internally; user code that calls
`get_transport_token` directly should follow it too. See
`network_authorizer.py`'s docstring + the
`test_transport_witness_lifetime.py` tests.

## Package structure

The spike is four sibling packages under `projects/fxtr_sandboxing/`:

```
projects/fxtr_sandboxing/
  pyproject.toml              # Host package config
  CLAUDE.md                   # Instructions for working in this package
  CODE_OVERVIEW.md            # This file

  plans/                      # Design docs

  fxtr_sandbox_types/         # Shared types + protocols (host + sandbox)
    fxtr_sandbox_types/
      __init__.py             # Re-exports public symbols
      _namespace.py           # SHARED_SANDBOX_NS UUID
      binding_request.py      # BindingConnectRequest + JSONValue — the standard connect shape fxtr_experiment bindings deliver
      entities.py             # Bundle entities for all three modes + sandbox-provenance entities
      protocols.py            # Cross-process service protocols (SandboxConnectProto + SandboxGuestImplProto + ...)
      service_context.py      # SandboxServiceContext typing.Protocol
      transport_routing.py    # Length-framed JSON routing-header helpers
      json_object_stream.py   # JSONObjectStream — ObjectStream over send/receive callables (eryx)
      socket_json_stream.py   # SocketJSONStream — ObjectStream over an anyio SocketStream (container)

  fxtr_sandbox_rpc_adapter/   # Sandbox-side runtime
    fxtr_sandbox_rpc_adapter/
      __init__.py             # Re-exports main, connect_transport, lazy get_client
      _main.py                # async def main(ctx, *, entry_point)
      _guest_main.py          # _GuestMainImpl(TypedServiceImpl[SandboxGuestImplProto]) — sets up per-call provenance
      _provenance.py          # Single-pass async walker for config snapshot + SCRE escape-safety check
      _transport.py           # Active-SandboxServiceContext contextvar + connect_transport
      _http.py                # Lazy [http] surface — get_client + _AuthorizingBackend

  fxtr_service_runner/        # In-container helper around fxtr_sandbox_rpc_adapter.main
    fxtr_service_runner/
      __main__.py             # python -m fxtr_service_runner [ENTRY_POINT]
      service_context.py      # ContainerServiceContext — SandboxServiceContext impl over UDS

  fxtr_pyodide_runner/        # Pyodide-backend runtime pieces (host driver + guest bootstrap)
    fxtr_pyodide_runner/
      __init__.py             # Paths API for the driver files
      main.ts                 # HOST-TRUSTED deno driver (grant-then-drop, EOF backstop)
      deno.json               # Load-bearing: stops deno config discovery walking up
      guest_bootstrap.py      # FLAKE-OWNED guest bootstrap (copied into the runtime bundle)

  fxtr_sandboxing/            # Host package
    __init__.py
    spawner.py                # SandboxSpawnerImpl + _SpawnConnectWrapperImpl + create_sandbox_environment + SandboxEnvironment
    transport_server.py       # TransportConnectionServer — env-level token table
    network_authorizer.py     # NetworkHandleAuthorizerImpl + per-token witness
    runner.py                 # Low-level WASM launcher (run + run_in_factory)
    direct_launcher.py        # Lower-level alternative to create_sandbox_environment
    _runner.py                # Script injected into the eryx sandbox; builds EryxServiceContext
    pyodide_runner.py         # Runner for NixPyodideServiceDefn guests: per-spawn task/transport UDS + `deno run` on the host-trusted driver with a minimal static permission profile; accept_or_exit + teardown ladder reused from _container_common
    nix_container_runner.py   # Runner for NixContainerServiceDefn images: run an already-delivered image (delivery is the nix-runner backend's deliver_image) with the task/transport UDSes; clear-images/clear-spawn-debris CLI
    _podman.py                # podman-specific machinery: remote detection, sshd-sidecar tunnel pod, debris sweep
    _container_common.py      # Container-runner scaffolding used by nix_container_runner.py; get_container_runtime() → ContainerRuntime (no implicit default)
    proxy.py                  # HTTP proxy primitives backing add_transport / add_passthrough_transport
    nix.py                    # git-flake-ref wrapper for the NixWasmServiceDefn/NixContainerServiceDefn spawn arms, run through a fxtr_nix_runner backend handle (host nix or in-podman): eval_entry_point + eval_service_kind + build_site_packages (materialized) + build_image over git+file://<h.flake_ref_root()>?rev=&dir=#<attr> (--no-update-lock-file); raises RuntimeError with nix stderr on failure. Container-image delivery (skopeo) is the backend's deliver_image, not here
    deprecated/               # the deprecated in-band-bytes prepare/bundle path (superseded by NixWasmServiceDefn); kept working for the still-live prepare_package_bundle. Importers use fxtr_sandboxing.deprecated.<module>
      prepare.py              # async prepare(task_dir, *, allowed_local_pkg_dirs=None) -> EntityRef[TaskBundle]
      _prepare_common.py      # Helpers for prepare.py (pyproject reading, uv lock/export, file walk, whitelist)
      _bundle_materialize.py  # Materialize a bundle into a flat site-packages directory (runner.py imports build_site_packages_for_bundle from here)
      wheels.py               # fetch_wheels(wheel_refs, cache_dir) — sha256-cached downloads

  examples/
    rpc_task/                 # WASM demo (typed-config decoding path)
    transport_task/           # WASM demo for the transport surface
    nested_relay_task/        # WASM demo for nested spawn

  tests/
    test_prepare_file_walk.py             # File-walk source resolution
    test_prepare_whitelist.py             # allowed_local_pkg_dirs enforcement
    test_proxy.py                         # proxy.proxy_connection tests
    test_passthrough_proxy.py             # PassthroughHostsConfig + passthrough_proxy_connection
    test_binding_request.py               # BindingConnectRequest codec units (JSONValue round-trip fidelity)
    test_socket_json_stream.py            # SocketJSONStream tests
    test_nix_container_spawner.py         # End-to-end NixContainerServiceDefn spawn (container_greeter fixture): greet/platform_info, kind-mismatch, no-git_repo_path guard
    test_nix_container_transport.py       # Transport E2E over the NixContainerServiceDefn spawn path (container_transporter fixture)
    test_host_death_cleanup.py            # Host-death self-teardown E2E: spawn in a child process, SIGKILL it, assert the builder + sidecar containers tear themselves down (podman-only; slow)
    test_podman_caches.py                 # Unit tests for the per-process podman micro-caches (podman_is_remote + sidecar host key fetched once)
    test_nix_spawner.py                   # End-to-end NixWasmServiceDefn spawn (git-ref path): prepare_workdir_repo a greeter fixture, spawn via SandboxSpawnerImpl, connect + greet; covers root-flake + flake_path subpath + the no-git_repo_path guard + the kind-agnostic NixServiceDefn auto-dispatch (repeat spawn rides the kind cache). `_workdir_templates/greeter/` is the fixture (copy of examples/rpc_task as a workdir flake)
    test_auto_kind_defn.py                # Unit tests for the auto arm's kind→defn mapping (triple preserved; unknown kind refused) — no sandbox/nix needed
    test_nix_wasm_transport.py            # Transport E2E over the NixWasmServiceDefn spawn path (transporter fixture)
    test_nix_pyodide_spawner.py           # End-to-end NixPyodideServiceDefn spawn (pyodide_greeter fixture): greet + validated_add (a PEP 783 pyemscripten binary wheel dlopens in-guest) + platform_info (Emscripten/wasm32) + kind mismatch; skips without deno on PATH
    test_nix_pyodide_transport.py         # Transport E2E over the NixPyodideServiceDefn spawn path (pyodide_transporter fixture): raw echo + http_get ([http] extra under pyodide); skips without deno
    test_spawner.py                       # End-to-end spawn -> connect (eryx)
    test_spawner_blast_radius.py          # One spawn's RPC abort is contained to that spawn (mocked guest over real wire/RPC/spawner); siblings + env survive, doomed surface degrades request-scoped
    test_spawn_build_error_propagation.py # A pre-`started` build/eval failure propagates its original message via `_contain_failures_after_started`, not an opaque empty ExceptionGroup (the complement of the blast-radius after-`started` containment)
    test_direct_launcher.py               # End-to-end direct launcher path (warm-spawn)
    test_transport_token_table.py         # TransportConnectionServer unit tests
    test_transport_witness_lifetime.py    # Authorizer + witness flow over in-process RPC pair
    test_transport_raw_e2e_wasm.py        # End-to-end raw byte-stream transport (eryx)
    test_transport_http_e2e_wasm.py       # End-to-end HTTP via get_client (eryx)
    test_transport_token_errors.py        # Bad-token surfacing
    test_nested_spawn_wasm_inner.py       # Nested-spawn end-to-end with a WASM inner
    test_nested_spawn_container_inner.py  # Nested-spawn end-to-end with a nix-container inner (container_transporter fixture)
    test_provenance_walker.py             # Unit tests for the guest-side walker (no sandbox)
    test_sandbox_provenance.py            # End-to-end per-connect provenance + escape-safety

  tmp_files_gitignore/        # Wheel cache + smoke-test scratch (gitignored)
```

## Module details

### pyproject.toml

Depends on `fxtr2`, `fxtr-sandbox-types[fxtr2]`, `pyeryx`, `packaging`, `httpx`, `aiohttp`, `h11`. Does NOT depend on `fxtr-service-runner` — that's a guest-only package, installed in the container image.

### plans/

Design docs. See `plans/stage2/2p1_new_rpc_system/overview.md` for the top-level design.

### fxtr_sandbox_types/

Shared types + protocols. Both `fxtr_sandboxing` (host) and `fxtr_sandbox_rpc_adapter` (sandbox) depend on this. Pure-Python; base dep on `anyio`. Paired with `fxtr2` / `fxtr2-portable` via `[fxtr2]` / `[fxtr2-portable]` extras.

#### entities.py / deprecated_entities.py

All use `define_type(SHARED_SANDBOX_NS, ...)` IDs. The live git-ref spawn
reference + the sandbox-provenance entities are in `entities.py`; the
deprecated in-band-bytes *bundle* entity types are in
`deprecated_entities.py` (location is cosmetic — all are still re-exported
from the package root, so `from fxtr_sandbox_types import TaskBundle` works,
and the `define_type` ids are unchanged → same wire types).

`entities.py` (live):

- **git-ref services**: `NixWasmServiceDefn(git_commit, flake_attr, flake_path?)` — a *reference* to a wasm-kind `fxtr.services.<name>` flake attr (not inlined code, no `entry_point` — that lives on the attr) — and `NixContainerServiceDefn` (same fields), its container/OCI-kind sibling pointing at an attr whose buildable leaf is a nix2container `image` (entry point baked into the image `Cmd`; no `entryPoint` leaf at all). `NixPyodideServiceDefn` (same fields) is the pyodide-kind sibling: its attr carries `sitePackages` (may include PEP 783 pyemscripten binary wheels) + `runtimeBundle` + `entryPoint`. `NixServiceDefn` (same fields) is the **kind-agnostic** form: the spawner evals the attr's `kind` and auto-dispatches — for callers holding only the triple (e.g. a stored binding record), where the flake attr owns its kind. The spawner resolves + builds all of them via `fxtr_sandboxing/nix.py`, checking the attr's `kind` (`"wasm"` / `"container"` / `"pyodide"`). Wire-identical dups (of the concrete kinds) live in the sandbox bridge so the agent can construct them in-script.
- **`DirectLauncherSentinel`** — placeholder bundle ref for direct-launcher provenance.
- **Sandbox provenance**: `ServiceConfigReplacementEntity` (substitutes for ServiceRef leaves in the captured config snapshot; carries `service_type_id` + `service_ctx`) and `SandboxProvenanceContext` (d-node payload that gets c-node-wrapped per `connect` call; holds `bundle: EntityRef[UnknownEntity]`, `config_snapshot: FrozenFxtrCbor`, `value=FxtrID(".")`). `SERVICE_CONFIG_REPLACEMENT_TYPE_ID` is exported at top-level for the escape-safety check in `_provenance.py`.

`deprecated_entities.py` (deprecated bundle path, WASM only):

- `PySourcePackage`, `PyWheelPackage`, `TaskBundle`. (The container /
  py_container siblings were removed in stage 8 of part5_nix_workdir.)

#### binding_request.py

`BindingConnectRequest` — the standard connect-request shape for bound
workflow services: `key` (the handle key `FxtrID` a spawn serves),
`config` (a plain-JSON binding blob, `JSONValue`), and `handles`
(name → pre-spawned dependency `ServiceRef`s). Defined here (rather
than in `fxtr_experiment`, its consumer) so host-side connectors and
sandbox guest entrypoints implement against one guest-importable
interface — see `projects/fxtr_experiment/notes/sandboxed_bindings.md`.
`JSONValue`'s bare recursion and union member order are load-bearing
for the typed-service codec (documented in the module).

#### protocols.py

Cross-process service protocols:

- `SandboxConnectProto[T, U]` — user-facing connect surface (`connect(config) -> ServiceRef[U]`). What `spawn` returns to the caller.
- `SandboxGuestImplProto[T, U]` — internal wire protocol the guest-side `_GuestMainImpl` exposes (`connect_with_bundle_provenance(config, bundle) -> ServiceRef[U]`). Wrapped on the host side by `_SpawnConnectWrapperImpl` so end users only see the simpler `SandboxConnectProto.connect(config)` surface.
- `SandboxSpawnerProto`, `NetworkHandleAuthorizerProto`, `TokenWitnessProto` — unchanged.

#### service_context.py

The runtime↔adapter contract. Exposes `main_protocol_stream` (the RPC channel) and `open_transport_stream(token)` (one redeemed byte-stream connection through a host-side network route).

#### json_object_stream.py

`JSONObjectStream` — anyio `ObjectStream` over a `(send, receive)`-callable pair. Escape-frame EOF (`["!", "eof"]`). Used by the eryx runtime where the channel is JSON-typed callbacks rather than a byte stream.

#### socket_json_stream.py

`SocketJSONStream` — anyio `ObjectStream` over an `anyio.SocketStream`. Length-prefixed JSON; native `send_eof` does a real UDS half-close; native `EndOfStream` propagation on clean peer half-close; mid-frame truncation surfaces as `anyio.BrokenResourceError`. Used by the container runtime.

### fxtr_sandbox_rpc_adapter/

Sandbox-side runtime: imports the user callable from the bundle-pinned `module:function`, mounts `_GuestMainImpl`, opens `open_rpc_scope`, exposes the transport surface (`connect_transport`, `get_client`) as free functions backed by a contextvar. Parametric over which `fxtr2` flavor is installed as a peer. Three extras (mix-and-match `[http]` with one of the fxtr2 flavors):

- `[fxtr2]` — full `fxtr2` + `sandbox-types[fxtr2]` (host / full Linux container).
- `[fxtr2-portable]` — pure-Python subset (eryx WASM).
- `[http]` — `httpx` + `httpcore` (gates `get_client`).

`__init__.py` raises an informative `ImportError` at import time if neither fxtr2 flavor is installed.

#### _main.py

`async def main(ctx, *, entry_point)`. Resolves `module:function`, opens the RPC scope, mounts `_GuestMainImpl`, installs `ctx` in the contextvar.

#### _guest_main.py

`_GuestMainImpl(TypedServiceImpl[SandboxGuestImplProto])`. The wire method `connect_with_bundle_provenance(config, bundle)` first runs `walk_config_for_provenance` over `config`, builds a `SandboxProvenanceContext` entity, mints a c-node from its cbor (`scope.create_c_node`), and overrides the per-call dispatch scope's marker (via the private `_apply_context_marker_override`; TODO documented in `plans/stage2/2p3_sandbox_provenance/plan.md`). Then it decodes `config` against the entrypoint's first-param annotation and invokes the user entrypoint inside the now-provenanced scope.

#### _provenance.py

Single-pass async walker `walk_config_for_provenance(value) -> FrozenFxtrCbor`. For each leaf in a `FxtrServiceData` tree:
- `ServiceRef`: resolve as untyped proxy, invoke `$fxtr.ctx` + `$fxtr.type`, store a `ServiceConfigReplacementEntity`, emit `RawStrongFxtrID` to it.
- `FxtrID` / `EntityRef` with tag `d` / `n`: load as `UnknownEntity`, reject with `SandboxProvenanceEscapeError` if the stored `$fxtr.type` matches `SERVICE_CONFIG_REPLACEMENT_TYPE_ID` (top-level only — no recursion into entity contents).
- Other FxtrIDs / primitives: passthrough into `RawWeakFxtrID` / verbatim.
- Mapping → `frozendict`, Sequence → `tuple`.

Replacements minted during the walk are never re-visited; the safety check applies only to user-provided refs.

#### _transport.py

Module-level contextvar for the active `SandboxServiceContext`. `connect_transport(token)` reads it and calls `ctx.open_transport_stream`.

#### _http.py

Lazy `[http]` surface. `_AuthorizingBackend` (httpcore-shaped) resolves the authorizer ref in a per-`connect_tcp` child scope, mints a token, opens the side-channel stream, wraps it as an `AsyncNetworkStream`. `get_client(authorizer_ref)` builds an `httpx.AsyncClient` over that backend.

### fxtr_service_runner/

In-container helper: thin shim around `fxtr_sandbox_rpc_adapter.main`. Guest-only — never installed on the host. Built into the container image via the demo Dockerfile.

#### __main__.py

`python -m fxtr_service_runner [ENTRY_POINT]`. Optional positional CLI arg pins a `module:function`; absent → discovers a single `fxtr.service` entry point from installed packages.

#### service_context.py

`ContainerServiceContext` — concrete `SandboxServiceContext` impl. Wraps the UDS `SocketStream` that `__main__` opened to the host. `open_transport_stream` dials `TRANSPORT_SOCKET_PATH` (`/run/fxtr/transport.sock`), writes the routing header, reads the response. Defines `SOCKET_PATH` and `TRANSPORT_SOCKET_PATH`.

### fxtr_sandboxing/spawner.py

`SandboxSpawnerImpl(TypedServiceImpl[SandboxSpawnerProto])` + `_SpawnConnectWrapperImpl` + `create_sandbox_environment` async CM + `SandboxEnvironment` class. `spawn(bundle)` accepts an `EntityRef` or `FxtrID`, normalizes, and dispatches on bundle type:

- `TaskBundle` → `_spawn_runner_wasm`
- `NixWasmServiceDefn` → `_spawn_runner_nix_wasm`
- `NixContainerServiceDefn` → `_spawn_runner_nix_container`
- `NixPyodideServiceDefn` → `_spawn_runner_nix_pyodide`
- `NixServiceDefn` → `_resolve_auto_defn` (eval the attr's `kind` via the per-env **kind cache**, keyed on the immutable triple and shared with the arms' `_check_service_kind`, so an auto spawn evals the kind once per env per triple; missing kind → the refuse-to-guess error, unknown kind → an error naming the known kinds) → the matching concrete arm. Spawn provenance keeps the caller's `NixServiceDefn` ref as the bundle.

`_spawn_runner_nix_wasm` is the wasm git-ref arm: it resolves the defn against the spawner's configured `git_repo_path` via `nix.py` (kind check + `eval_entry_point` + `build_site_packages`), then launches a bare `eryx.SandboxFactory()` with the realized site-packages store path mounted **read-only** (`run_in_factory(volumes=[(store_path, "/nix-site-packages", True)], sys_path_additions=["/nix-site-packages"])`) — no per-spawn copy, since the store path is immutable. It raises if the spawner has no `git_repo_path`.

`_spawn_runner_nix_container` is the container git-ref arm: kind check + `nix.build_image`, then `h.deliver_image(image_json, runtime=get_container_runtime())` (the backend builds skopeo + loads the image into the runtime, returning the ref + `--platform`), then `nix_container_runner._run_nix_container_internal(image_ref, platform, …)` (run-only) with `SocketJSONStream` wiring over the per-spawn task UDS. Delivery runs inside the `builder_for` block (so the podman backend's deliverer is available). Both git-ref arms share `_require_git_repo` + `_check_service_kind` (a `kind` mismatch or a missing `kind` raises with an error naming the right defn type).

`_spawn_runner_nix_pyodide` is the pyodide git-ref arm: kind check +
`nix.eval_entry_point` + `nix.build_site_packages` + `nix.build_runtime_bundle`
(both outputs materialized so the host deno process can read them), then
`pyodide_runner._run_pyodide_internal(...)` launches the host-trusted deno
driver (`fxtr_pyodide_runner`) against per-spawn task/transport UDSes with a
minimal static permission profile, and the arm serves the accepted stream via
`SocketJSONStream` + `_serve_spawn_rpc` like the container arm. Caches
`(entry_point, site_packages, runtime_bundle)` per env like the other arms.

The spawner resolves its nix backend **lazily**: `_backend()` calls `resolve_backend()` on first nix spawn and caches it; the spawn arms then open a per-call `async with self._backend().builder_for(repo) as h:`. An env that never does a nix spawn never resolves a backend (needs no config). **Warm builder:** on the first nix spawn the spawner also opens one env-lifetime `keep_builder_warm(repo)` pin (`_ensure_builder_warm`, guarded so concurrent first-spawns open exactly one; closed by `aclose_warm_builder`, which `create_sandbox_environment` runs at env teardown). The pin keeps the podman builder container (and its nix-daemon + deliverer) alive across the env's spawns, so `builder_for` per spawn is a cheap shared borrow instead of a start/stop each time — the dominant warm-spawn cost before this (see `plans/stage2/2p4_persistent_spawner/plan.md`). Still lazy: an env with no nix spawn pins nothing. Both nix arms run their eval→build→deliver through `_build_with_recycle`, which retries once if the warm builder/deliverer died mid-build (a `BuilderTransportError` from the backend, which has already invalidated the dead container — see `fxtr_nix_runner`); a `builder.recycled` perf marker records it. **Build-pipeline cache:** the arms key a per-env cache on the immutable `(git_commit, flake_attr, flake_path)` — container caches the `DeliveredImage`, wasm caches `(entry_point, site_packages)` — so a repeat spawn of the same service skips eval/build/deliver entirely (records `spawn.cache_hit`), taking a warm repeat container spawn from ~2.35s to ~1.1s. Valid for the env's life (built store paths + delivered images persist; no external pruning assumed).

All arms share `_serve_spawn_rpc` (open an `open_rpc_scope` on `env_tg` — typed against `SandboxGuestImplProto`, not `SandboxConnectProto` — report `started`, park on the teardown event) and wrap their whole body in `_contain_failures_after_started`: any `Exception` after `task_status.started()` (a guest aborting its RPC connection, a transport failure mid-drain) is logged and contained to that spawn instead of escaping into `env_tg` and killing every sibling spawn; failures before `started` still ride back through `env_tg.start` as the spawn invoke's error, and cancellation always propagates. `spawn` bridges the guest's main service back into `call_scope`. `spawn` then mounts `_SpawnConnectWrapperImpl(raw_guest_ref, bundle)` on `call_scope` and returns its ref — that's the user-facing `SandboxConnectProto` surface. The wrapper's `connect(config)` resolves the raw guest and forwards to `connect_with_bundle_provenance(config, bundle)`, threading in the bundle ref that the user can't forge. `SandboxEnvironment` also exposes `add_transport(route)`, `add_passthrough_transport(*, allowed_hosts=, blocked_hosts=)`, and `add_raw_transport(handler)`. The one env-level kwarg beyond `cache_dir` is `git_repo_path` (the one repo git-ref defn spawns resolve `?rev=` against; `None` → the git-ref arms are unavailable).

### fxtr_sandboxing/transport_server.py

`TransportConnectionServer` — env-level token table (`token -> sync TransportHandler`). `register_token` / `revoke_token` / `consume_token`. `threading.Lock`-guarded for cross-thread access (the eryx callback path hits the table from a thread). `make_http_route_handler` factory; `BadTransportTokenError`.

### fxtr_sandboxing/network_authorizer.py

`NetworkHandleAuthorizerImpl` bound to one `TransportHandler`. `get_transport_token` mints `secrets.token_hex(32)`, registers it in the token table, mounts a witness ref on the dispatch scope, and registers a `register_end_callback` on `call_scope._token` that revokes on scope drain. Lease-driven, immune to GC cycles. Once a token is redeemed (consumed from the table), the late revoke is a no-op.

### fxtr_sandboxing/runner.py

Low-level WASM launcher. Two entry points, both yield a `TaskSession` (internal-only; higher layers wrap with `JSONObjectStream` + `open_rpc_scope`):

- `run(bundle, cache_dir, ...)` — bundle-driven path used by `spawner.py`. Per-spawn extracts source + wheels into a tempdir, builds a per-spawn factory, delegates to `run_in_factory`.
- `run_in_factory(factory, *, entry_point, volumes=[], sys_path_additions=[], ...)` — caller-owned-factory path used by `direct_launcher.py` for the warm-spawn case.

Both register eryx callbacks (h2s/s2h JSON queues + five `_transport_*` callbacks), inject `_SOCKET_SHIM` + `_LOOP_SHIM` + `_ENTRY_POINT`, execute `_runner.py`. Accept an optional `transport_server`.

### fxtr_sandboxing/direct_launcher.py

Lower-level alternative to `create_sandbox_environment` — skips bundle prep + spawner indirection. `create_direct_sandbox_environment(*, factory)` async CM yields a `DirectSandboxEnvironment` around a caller-built factory. `env.open_sandbox(*, entry_point, volumes=[], sys_path_additions=[])` yields a `ServiceRef[SandboxConnectProto[FxtrServiceData, UnknownService]]` bridged into a per-call mountable `FxtrScope`. Used by the workstation MCP plugin for ad-hoc script execution.

### fxtr_sandboxing/_runner.py

Script injected into the eryx sandbox: builds `EryxServiceContext` (wraps eryx callbacks via `JSONObjectStream` + the four byte-level transport callbacks via `_EryxTransportByteStream`) and calls `fxtr_sandbox_rpc_adapter.main(...)`. Host prepends `_ENTRY_POINT = ...` from the bundle.

### fxtr_sandboxing/pyodide_runner.py

**Runner** for `NixPyodideServiceDefn` guests (spawner-internal): the
process-model sibling of `nix_container_runner.py`, minus containers.
`_run_pyodide_internal(entry_point, site_packages, runtime_bundle, *,
transport_server, v8_flags)` opens per-spawn task (+ optional transport)
UDS listeners in a short tmpdir (macOS `SUN_LEN`), launches `deno run` on
the host-trusted driver (`fxtr_pyodide_runner.driver_main_ts_path()`) with
the minimal static permission profile (`--allow-read` = driver dir + bundle
+ site-packages + sockets; `--allow-write`/`--allow-net=unix:` = sockets
only; no env/run/ffi; optional `--v8-flags` soft resource knob), races
accept against early exit, and tears down via the graceful→terminate→kill
ladder — `accept_or_exit` / `transport_accept_loop` /
`teardown_container_proc` all reused from `_container_common.py`. The
driver itself narrows permissions further at runtime (revokes net entirely
before any flake-supplied code runs when no transport socket is
configured) and hard-exits a guest that never unwinds after main-conn EOF.
Deno is discovered on PATH; a missing deno raises with an install hint.

### fxtr_sandboxing/nix_container_runner.py

**Runner** for `NixContainerServiceDefn` images (spawner-internal). *Delivery* — loading a built image into the runtime's store — is the nix-runner backend's job (`fxtr_nix_runner.Handle.deliver_image`, which returns the `image_ref` + `--platform`); this module only runs an already-delivered image. `_run_nix_container_internal(image_ref, platform, *, transport_server)` runs it with the backend-supplied `--platform` (runtimes don't auto-select the sole variant of a loaded index), **no trailing command** (entry point baked into the image `Cmd`). Socket plumbing branches on `podman_is_remote`: remote podman (machine VM) rides the per-spawn sshd-sidecar pod from `_podman.py` (host sockets forwarded onto a shared volume mounted at `/run/fxtr`); local kernels mount the socket files individually. Teardown order matters: the guest's graceful EOF-driven exit happens before the tunnel/pod teardown (`pod rm -f` SIGKILLs members). Also exposes `clear_image_cache()` (prunes the `fxtr-nix-service` repo — the ref convention lives in `fxtr_nix_runner._delivery`) + the `python -m fxtr_sandboxing.nix_container_runner {clear-images,clear-spawn-debris}` CLI.

### fxtr_sandboxing/_podman.py

podman-specific machinery behind the nix container runner. (Host-side
API-socket discovery for image delivery — `resolve_podman_socket` — moved to
`fxtr_nix_runner._delivery`, which owns delivery; the run path here only needs
remote detection + the tunnel.)

- `podman_is_remote(runtime)` — `podman info .Host.ServiceIsRemote`; `true` ⇒ host UDS listeners aren't reachable via `-v` socket-file mounts ⇒ sidecar tunnel. Memoized per runtime (the answer can't change during a run), so it's not a per-spawn `podman info`.
- `_sidecar_host_key(runtime, image_ref, name)` — the sidecar's baked ssh host key, memoized per (content-addressed) image ref so it's fetched once, not via a per-spawn `podman exec cat`.
- `open_sidecar_tunnel(runtime, socket_paths)` — per-spawn `--network none` pod (sidecar + sandbox share a `:Z` named volume = free per-spawn MCS compartment); one host `ssh` whose transport is `ProxyCommand = podman exec -i <sidecar> /usr/sbin/sshd -i` (inetd mode — **nothing listens anywhere**), one `-R` per socket onto the volume; remote command gates a `READY` line on the sockets existing (the runner blocks on it before starting the sandbox — no guest retry loop); sidecar host key fetched via exec + pinned. Yields the sandbox's `--pod`/`-v` run args. Designed + validated in the stage-8 spike.
- Sidecar image: podman-built from an inline Containerfile (alpine + openssh; host keys baked; main process `sleep infinity`), tagged `localhost/fxtr-sshd-sidecar:<containerfile hash>`, built on demand. Same-host concurrent first-builds (parallel spawns on a cold image store) are serialized by an `flock` (`~/.cache/fxtr_sandboxing/sidecar-build-<tag>.lock`, `$XDG_CACHE_HOME`-aware) — waiters re-check the image cache under the lock, so N cold-store spawns do one build. Defense in depth for any corpse that slips through anyway: the Containerfile bakes a `fxtr-role=sshd-sidecar` label (`ROLE_LABEL_KEY`); racing builds are nondeterministic (`ssh-keygen -A`), so a retag loser survives as an untagged `<none>` image, and the label — living in image content — survives the untagging, letting `clear_image_cache` sweep corpses by `--filter label=fxtr-role --filter dangling=true`. (Sequential rebuilds don't corpse: podman's layer cache makes them produce the identical image ID.)
- Per-spawn pods/volumes carry a `fxtr-spawn` label; `clear_spawn_debris(runtime)` sweeps orphans after a crash.
- **Host-death self-teardown:** the ssh remote command holds an exclusive `flock` on `<volume>/.hostlive` over a `cat` on the ssh-forwarded stdin (the dead-man's switch), and the sidecar gets a health check (`flock -n … && exit 1 || exit 0`, `--health-on-failure=stop`, `--rm`). When the host process dies its ssh stdin pipe closes → ssh forwards EOF → `cat` exits → the kernel releases the lock → the sidecar health check sees it free → podman stops+removes the sidecar. The stdin EOF (not merely the flock) is load-bearing: the ssh *client* can orphan-survive the host's death on macOS, so a held flock alone wouldn't release — the pipe closing is what crosses the boundary. Empty pod + volume are left behind (cheap; `clear_spawn_debris`). Sandbox needs no health check — its guest exits when the forwarded task socket drops. Design + spikes: `plans/stage2/2p4_persistent_spawner/plan2_host_death_teardown.md`.

### fxtr_sandboxing/_container_common.py

Container-runner scaffolding used by `nix_container_runner.py` (historically shared with the deleted `container_runner.py` / `py_container_runner.py`):

- `get_container_runtime()` (was `get_runtime`) — `load_config().require_container_runtime()` → a `fxtr_nix_runner.ContainerRuntime` (`cmd` + concrete `type ∈ {podman, docker_compatible}`). **No direct env read** (env resolves in `load_config`) and **no implicit default** — an unconfigured `container_runtime_cmd` raises `ConfigError` pointing at `fxtr_nix runtime setup`. Callers use `.cmd` in argv and `.type` to branch (podman → tarless socket delivery + direct socket mounts; docker_compatible → tar `image load`). podman is the recommended runtime; Apple Container is the supported swept non-default; docker is an untested escape hatch.
- `accept_or_exit(listener, proc)` — race UDS accept against container's early exit.
- `handle_transport_conn(conn, transport_server)` — read routing header, validate token, write response, hand off to the handler.
- `transport_accept_loop(listener, transport_server)` — serve loop over the multiplexed transport UDS.
- `teardown_container_proc(proc)` — graceful wait → terminate → kill ladder.

### fxtr_sandboxing/deprecated/prepare.py

`async prepare(task_dir, *, allowed_local_pkg_dirs=None) -> EntityRef[TaskBundle]`. WASM-specific orchestration on top of `_prepare_common` helpers. Builds wheels via `_build_packages_wasm` (py3-none-any only via `_find_pure_python_wheel`).

### fxtr_sandboxing/deprecated/_prepare_common.py

Helpers for `prepare.py`: `read_pyproject`, `read_entry_point`, `resolve_deps` (uv lock + uv export), `find_uv`, `check_whitelist`, `build_source_package`, `resolve_source_dirs` / `walk_package_dir`, `resolve_local_pkg_whitelist`.

`allowed_local_pkg_dirs` (passed to either prepare), when set, is a whitelist of "trust roots" — every resolved local-pkg directory (and every force-include source path) must fall under one of these roots, else raise. `None` (default) means no restriction. Used by the workstation MCP plugin.

Source-dir resolution order:

1. `[tool.fxtr.sandboxing]` packages
2. `[tool.hatch.build.targets.wheel]` packages
3. auto-discover `<pkg>/` then `src/<pkg>/`

`build_source_package` file-walks a local pkg into a `PySourcePackage` and honors `[tool.fxtr.sandboxing]` packages, `[tool.hatch.build.targets.wheel]` packages, auto-discover, and hatch force-include.

### fxtr_sandboxing/deprecated/_bundle_materialize.py

Materialize a bundle into a flat site-packages directory provided by the caller: `build_site_packages_for_bundle(TaskBundle, ...)`. Caller-owned `target_dir` enables future content-addressed caching. `runner.py` re-exports `build_site_packages_for_bundle`.

### fxtr_sandboxing/deprecated/wheels.py

`fetch_wheels(wheel_refs, cache_dir)` — downloads and caches remote wheels by sha256. `wheel_refs` is an iterable of `(url, sha256)` pairs; callers project both `PyWheelPackage` (`wheel_url` + `wheel_sha256`) and `PyWheelArtifact` (`url` + `sha256`) into that shape.

### fxtr_sandboxing/proxy.py

HTTP proxy primitives backing `add_transport` / `add_passthrough_transport`:

- `TransportRoute` — `upstream_base_url` + `injected_headers` + `allowed_path_prefixes`; lookups are by token, not name.
- `proxy_connection()` — h11-based HTTP proxy.
- `PassthroughHostsConfig` — mutually-exclusive `allowed_hosts` / `blocked_hosts`; default is no filtering.
- `passthrough_proxy_connection` — host-agnostic sibling that picks the upstream from the request's `Host:` header (always HTTPS).

On error responses (400, 403, 502), `_send_error` half-closes + drains before returning. Both `proxy_connection` and `passthrough_proxy_connection` wrap their body in a top-level `except Exception` that logs and returns, so a misbehaving connection never propagates into the env's task group.

### fxtr_sandboxing/nix.py

git-flake-ref wrapper for the `NixWasmServiceDefn` / `NixContainerServiceDefn` spawn arms. Each function takes a `fxtr_nix_runner` backend **handle** `h` (the caller opens `async with backend.builder_for(repo) as h:` once around the eval+build sequence; the handle's backend runs nix on host or inside a podman builder) and forms `git+file://<h.flake_ref_root()>?rev=<commit>&dir=<flake_path>#<attr>`:

- `_attr_url(h, commit, attr, flake_path)` — private ref-construction helper. `h.flake_ref_root()` is the host repo path on the host backend, `/workdir[/relpath]` inside the builder on podman. A remote (`git+ssh`/`git+https`) constructor gets added when remote deployment lands.
- `eval_entry_point(h, commit, attr, flake_path=None) -> str` — `nix eval --raw` of `<attr>.entryPoint` (wasm arm).
- `eval_service_kind(h, commit, attr, flake_path=None) -> str | None` — `nix eval --json --apply 's: s.kind or null'` of the attr's `kind` discriminator; `None` = no kind declared (a flake built against a fxtr lib predating the `kind` discriminator, or a hand-built attr missing it). Lazy — forces no buildable leaves.
- `build_site_packages(h, commit, attr, flake_path=None) -> Path` — `nix build --no-link --print-out-paths` of `<attr>.sitePackages`, then `h.materialize(...)` so the result is host-readable (the wasm arm mounts it into eryx); identity on the host backend (wasm arm).
- `build_runtime_bundle(h, commit, attr, flake_path=None) -> Path` — same build+materialize for `<attr>.runtimeBundle` (pyodide arm; the host deno driver reads pyodide assets + the guest bootstrap from it).
- `build_image(h, commit, attr, flake_path=None) -> Path` — same build for `<attr>.image` (container arm); returns the nix2container JSON manifest store path verbatim (NOT materialized — images are delivered by skopeo from inside the store, never copied to the host).
(Building the nix2container-patched skopeo + delivering the image is the
backend's `deliver_image`, not this wrapper — see `fxtr_nix_runner`.)

The repo-ref calls pass `--no-update-lock-file` so a missing/stale committed `flake.lock` errors instead of silently re-locking in-memory. Failures raise a plain `RuntimeError` with the nix stderr in the message — the spawner doesn't catch it; it propagates as the `spawn` invoke's error (per `11_failure_and_ux.md`). We don't parse stderr to classify the cause. (No typed eval/build error hierarchy — nothing catches one; reintroduce a `NixError` if a catch site ever needs one, see `stage5post2_nix_wrapper_simplify.md`.) Also the intended home for the forward-looking service-identity `.drv`-hash helper (not built in v0).

### examples/

Each example is a service-shaped task bundle; see the per-example pyproject for the entry point and dependencies.

- **rpc_task** — WASM. `EntityRef[GreetingConfig]` config exercises the typed-config decoding path. `GreeterProto`: `greet(name) -> str`, `add(a, b) -> int`. (The container mirror of this demo lives in `tests/_workdir_templates/container_greeter/`.)
- **transport_task** — WASM. Entry-point takes `ServiceRef[NetworkHandleAuthorizerProto]` directly as its config. Methods: `raw_echo` (uses `connect_transport`), `http_get` (uses `get_client`), `redeem_explicit_token` (test hook for bad-token error paths). (Container mirror: `tests/_workdir_templates/container_transporter/`.)
- **nested_relay_task** — WASM nested-spawn demo. Config is a plain Python `@dataclass` (`NestedRelayConfig`) carrying a `ServiceRef[SandboxSpawnerProto]`, an `EntityRef` for an inner bundle, and a `ServiceRef[NetworkHandleAuthorizerProto]`. Single method `relay_http_get(paths)`: per-call spawns the inner, connects with the authorizer, maps `inner.http_get` over the paths, returns wrapped results. Per-call spawn / teardown. Pulls `fxtr-sandbox-rpc-adapter[fxtr2-portable]` only.

### tests/

Tree entries above are one-line summaries. A few of note:

- `test_transport_witness_lifetime.py` — surfaces the resolution-scope-vs-active-scope subtlety; covers witness-drop revokes unredeemed token, witness drop after redemption is no-op, explicit `revoke_token` works.
- `test_direct_launcher.py` — includes the warm-spawn path the MCP plugin uses (factory provides the platform; user entry-point package is reached only through `volumes=` + `sys_path_additions=`).
- `test_nested_spawn_*` — verify a sandbox can hold a `ServiceRef[SandboxSpawnerProto]`, spawn a sibling under the env, thread an authorizer ref through to the inner via `connect`'s config, and aggregate `http_get` results. Lock in the typed-config path (outer's config is a plain `@dataclass`). The container variant proves the capability ref bridges across two sandbox boundaries (host → WASM outer → Linux container).
- Container E2E tests sweep over all supported runtimes via the parameterized `container_runtime` fixture in `conftest.py` (podman + Apple Container; per-runtime skip when not on PATH). They're slow (nix image builds on a cold cache).

### Spawn perf timing

The nix-spawn path is instrumented with `fxtr_nix_runner.perf` spans (opt-in,
inert by default). The spawner arms (`spawn.build_and_deliver`), the nix
eval/build wrappers (`nix.py`), the container runner (`container.sidecar_tunnel`,
`container.run_and_accept` — the latter covers container boot + the in-container
`fxtr_service_runner` Python/fxtr2 import), and the sidecar tunnel
(`sidecar.ensure_image`, `sidecar.ssh_ready`) wrap their phases; every
subprocess is timed via `proc_label`. Set `FXTR_PERF_LOG=1` on any spawn to
print the per-phase breakdown to stderr instead of a silent wait, or use
`perf.collect_perf()` to read structured `PerfEvent`s (the
`spikes/sandbox_spawn_benchmark` driver does this). The builder container is
(re)started per spawn on the podman backend — `builder.start` / `builder.stop`
show that cost.

## Development

```sh
cd projects/fxtr_sandboxing/fxtr_sandboxing   # the package dir (src/ + tests/ + pyproject.toml)
uv sync
uv run pytest tests/ -v -n auto   # runs all tests, parallelized via xdist
```

Pyodide E2E tests (`test_nix_pyodide_*`) additionally need **deno** on the
host PATH (any recent deno 2.x; an optional dependency — only the pyodide
kind uses it) and **skip** when it's absent.

Container E2E tests (`test_nix_container_*`, `test_nested_spawn_container_inner`)
**sweep over all supported runtimes** via the parameterized
`container_runtime` fixture in `conftest.py`: **podman** (the
recommended default) and **Apple Container** (`container`, the
supported non-default backend — it exercises genuinely different
delivery + socket-plumbing surfaces, and is swept so it can't rot
silently). Per-runtime behavior:

- Not on PATH → that half of the sweep **skips**.
- Installed but not running (podman machine stopped / Apple Container
  service not started) → **fails**; deselect a backend explicitly
  (e.g. `-k podman`) if you don't want it.
- An ambient `FXTR_CONTAINER_RUNTIME_CMD` is **ignored by the tests**
  (overridden per-param, with a warning) — outside the tests it still
  selects the runtime as usual.

podman one-time setup (macOS 26+, the recommended shape — applehv
provider so amd64 images run under **Rosetta** at near-native speed):

```sh
# ~/.config/containers/containers.conf: [machine] provider = "applehv"
podman machine init   # once (rosetta defaults to on under applehv)
podman machine start  # per boot

# Rosetta activation is still gated off in current podman-machine-os
# images (upstream disabled it for the macOS 15.x kernel>=6.13 bug and
# left a marker-file hook for when Apple fixed it — which macOS 26 did).
# Flip it on inside the machine VM, once:
podman machine ssh 'sudo touch /etc/containers/enable-rosetta && sudo systemctl start rosetta-activation.service'
```

The marker file lives on the machine VM's disk: it survives stop/start
but is **lost on `machine rm` + re-init** — without it amd64 silently
falls back to qemu-user (slow but correct). Newer machine-os images may
make this client-controlled (containers/podman-machine-os PR #237), at
which point the ssh step goes away.

On macOS < 26 Rosetta is genuinely broken in the guest (vdso `DT_HASH`
assert, kernels >= 6.13); there, skip the marker step and run amd64
under qemu-user — works on both applehv and libkrun providers (see the
stage-8 plan's host-env note for the full history).

No policy or other setup beyond that (the sshd-sidecar socket plumbing
needs no SELinux configuration; the sidecar image is podman-built on
first spawn, which pulls the alpine base — network needed once). The
tests build their images via nix at test time (no pre-pushed registry
images), so cold-cache runs are slow. The delivered-image cache and any
crash debris are managed via
`python -m fxtr_sandboxing.nix_container_runner
{clear-images,clear-spawn-debris}`.

Non-podman runtimes (`FXTR_CONTAINER_RUNTIME_CMD=container` / `=docker`,
i.e. `ContainerRuntime.type == "docker_compatible"`) use the tar-based
delivery fallback and direct socket-file mounts.
docker is a compatibility escape hatch only — it's not in the test
sweep. (Apple Container note, carried from stage 7: its qemu
translator is kernel-sensitive — service images pin the guest kernel
via `mkContainerService`.)

Standalone smoke tests for the portable + adapter wheels live in
`tmp_files_gitignore/portable_smoke/` and `tmp_files_gitignore/adapter_smoke/`.
