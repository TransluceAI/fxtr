# Stage 4 — Nested Spawn + Polish

Validate the recursive case (a sandbox spawning a sub-sandbox) end-to-end against
both runtime types as the inner, then sweep up the historical clutter left over
from earlier stages.

The full stack already supports this — stage 3 spelled out *why* nested spawn
"just works" (every sandbox an env spawns is a sibling under that env, regardless
of who asked for the spawn; capability flow rides on `config`). Stage 4 just
demonstrates it.

## High-level shape

One new outer WASM example, `examples/nested_relay_task`, that takes three things
via config:

- `ServiceRef[SandboxSpawnerProto]` — the env's spawner ref, bridged in
- `EntityRef[Any]` — the inner bundle (either `TaskBundle` or `DockerTaskBundle`)
- `ServiceRef[NetworkHandleAuthorizerProto]` — an authorizer for an HTTP route

Per call, the outer:

1. Resolves the spawner ref, calls `spawn(inner_bundle_ref).resolve()` to get
   the inner sandbox ref.
2. Calls `sandbox.connect(authorizer_ref).resolve()` (and `replace_service_type
   (TransporterProto)`) — passes the authorizer through so the inner can use
   it.
3. Maps `inner.http_get(path)` over the input list of paths.
4. Wraps each result (e.g. prefixes `[outer-wrapped] `) and returns the list.

When the outer's method returns, its dispatch scope drains, the inner spawn ref
drops, and the inner sandbox tears down. The outer itself stays alive for the
lifetime of the host's `connect` to the outer.

The outer is reused with two different inner bundles in tests:

- Inner = `examples/transport_task` (WASM)
- Inner = `examples/container_transport_task` (Docker, skipped without
  runtime + pushed image)

This is enough to prove (a) `EntityRef[DockerTaskBundle]` survives a hop through
a sandbox boundary into another sandbox's spawn call, (b) authorizer refs bridge
across two sandbox layers and are usable from the leaf, and (c) per-call spawn /
teardown of an inner from inside the outer works under both runtime backends.

## Steps

### 1. Add `examples/nested_relay_task/` (outer WASM bundle)

- [x] `examples/nested_relay_task/` package skeleton mirroring
      `examples/transport_task/`:
      - `pyproject.toml` declares
        `fxtr-sandbox-rpc-adapter[fxtr2-portable]` (no `[http]` — the outer
        doesn't make HTTP calls itself; only the inner does) plus
        `fxtr-sandbox-types[fxtr2-portable]` for the protocols. Path
        deps via `[tool.uv.sources]`.
      - `[project.entry-points."fxtr.service"] service = "nested_relay_task:run"`.
- [x] `nested_relay_task/__init__.py`:
      - Fresh module-level UUID `NESTED_RELAY_NS`. Define
        `RELAY_TYPE_ID = system_entities.define_type(NESTED_RELAY_NS,
        "nested_relay")` for the impl's `_service_type_id`.
      - Define a `NestedRelayProto` `@service_protocol` Protocol with
        `async def relay_http_get(paths: list[str]) -> list[str]: ...`.
      - Mirror `TransporterProto` (same `@service_protocol` shape as
        `transport_task.TransporterProto`) so the outer can call
        `inner_user_ref.replace_service_type(TransporterProto)`. Same
        precedent as the test files that mirror demo protocols for
        wire-compat (see `test_transport_http_e2e_wasm.py`).
      - Try a small **plain Python `@dataclass(frozen=True)`** as the
        config type:
        ```python
        @dataclass(frozen=True)
        class NestedRelayConfig:
            spawner_ref: ServiceRef[SandboxSpawnerProto]
            inner_bundle_ref: EntityRef[Any]
            authorizer_ref: ServiceRef[NetworkHandleAuthorizerProto]
        ```
        and use it as the entry point's `config` annotation. If
        `decode_from_service_data` doesn't accept plain dataclasses
        (the documented support is for `DataclassEntity`, not arbitrary
        dataclasses), **fall back to a typed 3-tuple** as the
        annotation:
        ```python
        config: tuple[
            ServiceRef[SandboxSpawnerProto],
            EntityRef[Any],
            ServiceRef[NetworkHandleAuthorizerProto],
        ]
        ```
        Note which we end up with in the demo's docstring + in the
        decisions section of this plan after implementation, and note
        the dialect-extension TODO if the plain-dataclass attempt
        surfaces a real gap rather than just an unsupported case.
- [x] `_Impl(TypedServiceImpl[NestedRelayProto])`. Constructor stashes the
      three refs (or unpacks them from the tuple/dataclass).
- [x] `relay_http_get(paths)` body, per call:
      ```python
      spawner = await self._spawner_ref.resolve(get_active_scope())
      sandbox_ref = await spawner.spawn(self._inner_bundle_ref)
      sandbox = await sandbox_ref.resolve(get_active_scope())
      inner_any_ref = await sandbox.connect(self._authorizer_ref)
      inner_ref = inner_any_ref.replace_service_type(TransporterProto)
      inner = await inner_ref.resolve(get_active_scope())
      results = []
      for path in paths:
          results.append(f"[outer-wrapped] {await inner.http_get(path)}")
      return results
      ```
      All resolves happen against the active scope (the dispatch scope of
      the outer's call), so the bridges drop together when the call
      returns. No long-lived state on the impl.
- [x] `async def run(config) -> ServiceRef[NestedRelayProto]` — standard
      shape: build the `_Impl`, mount it on the active scope, return the
      ref. Same pattern as `transport_task.run`.

### 2. New test: `tests/test_nested_spawn_wasm_inner.py`

- [x] Mirror the upstream-server fixture from
      `test_transport_http_e2e_wasm.py` (small aiohttp on `127.0.0.1:0`).
- [x] Open env, register transport for the upstream, prepare the outer
      bundle (WASM) and the inner bundle (`examples/transport_task`,
      WASM). Build the config (dataclass or tuple to match what the
      example uses).
- [x] Single test `test_nested_spawn_relay_http_get`:
      - Connect the outer with the config, resolve as
        `NestedRelayProto`, call `relay_http_get(["/hello", "/world"])`.
      - Assert the response is
        `["[outer-wrapped] upstream got /hello",
          "[outer-wrapped] upstream got /world"]`.
      - The env CM exits without warnings / hung tasks (the existing
        `create_sandbox_environment` exit-stack drain catches this for
        free; no extra assertion needed beyond the `async with` doing
        its job).

### 3. New test: `tests/test_nested_spawn_container_inner.py`

- [x] Same shape as step 2, but the inner bundle is
      `examples/container_transport_task` (Docker).
- [x] Reuse the runtime+image skip helpers already in
      `test_container_spawner.py` /
      `test_transport_e2e_container.py` (`_runtime_available`,
      `_image_available`). Skip the test cleanly if either is missing.
- [x] `create_sandbox_environment(cache_dir=..., insecure_registry=True)`
      because the demo image lives at `localhost:5000/...`.
- [x] Test `test_nested_spawn_relay_http_get_container` — same body as
      the WASM-inner test, with inner bundle swapped in. No marker
      change needed (the skip handles availability); does not need the
      `slow` marker (image pull is local-only and the test exercises
      one container start).

### 4. Delete `examples/old_broken/` and `tests/old_broken/`

- [x] `git rm -r examples/old_broken tests/old_broken`. Stage 1's
      decision was to keep these as historical reference; with stage 4
      complete and the wheel-dep / editable-dep paths exercised by the
      production examples (`fxtr-sandbox-rpc-adapter[http]` pulls
      external wheels via uv lock; `[tool.uv.sources]` path deps
      exercise editable-dep packaging), there's nothing left these add
      that git history doesn't.
- [x] Drop the `tests/old_broken/` exclusion from
      `pyproject.toml`'s pytest config (`norecursedirs` /
      `--ignore=` whichever is in use).
- [x] If anything is still importing from these directories, it's a
      bug — remove the import along with the directory.

### 5. Update CODE_OVERVIEW.md

Final pass; the structural changes from this stage are small.

- [x] Bump status: stage 4 done.
- [x] Add a "Nested spawn" subsection to "Key concepts". Two
      paragraphs max. Lead with the punchline ("nested sandboxes are
      siblings under the env, not nested at the runtime layer") and
      cover (a) capability flow rides on `config`, no ambient
      transport contextvar; (b) the per-call spawn/teardown lifetime
      pattern the demo uses; (c) how this composes with the existing
      authorizer / token-witness machinery (no new code path needed
      on the host).
- [x] Add `examples/nested_relay_task/` to the file listing.
- [x] Add `tests/test_nested_spawn_wasm_inner.py` and
      `tests/test_nested_spawn_container_inner.py` to the file listing.
- [x] Remove `examples/old_broken/` and `tests/old_broken/` from the
      file listing.
- [x] Drop the "old_broken" mention from the "Status of the rewrite"
      preamble.

## Decisions captured here for future reference

- **One outer demo, two inner backends.** A single
  `examples/nested_relay_task` (WASM) is reused with two different
  inner bundles by the tests — `transport_task` (WASM) and
  `container_transport_task` (Docker). No dedicated container outer
  is built; nested-from-container is a degenerate case (a container
  sandbox can hold a `ServiceRef[SandboxSpawnerProto]` just fine
  because it's a normal RPC ref) and the WASM-outer + container-inner
  test is enough coverage.
- **Per-call spawn/teardown inside the outer.** Each
  `relay_http_get` call spawns the inner, drains, and lets it tear
  down at end of call. Simpler to reason about than caching the
  inner across calls, and exercises the lifetime machinery once per
  call instead of once per outer connect.
- **Capability refs flow through `config`.** The outer receives the
  spawner + authorizer + bundle refs all via its first-arg config —
  no ambient transport contextvar leaks across spawn calls.
  Capability-style permissions fall out of this: an inner can only
  redeem authorizers that were bridged into it, period.
- **No new host code.** Everything in stages 1–3 already supports
  this pattern. The only new artifacts are the demo example and the
  two tests.
- **Old-broken examples and tests are deleted, not ported.** The
  historical reasons to keep them (wheel-dep packaging,
  editable-dep packaging) are exercised by the existing production
  examples — `fxtr-sandbox-rpc-adapter[http]` pulls httpx / httpcore
  as external wheels through uv lock, and the demo pyprojects use
  `[tool.uv.sources]` path deps to pull `fxtr_sandbox_types` /
  `fxtr_sandbox_rpc_adapter` as editable local packages. Both the
  remote-wheel and the local-editable code paths in `prepare()` are
  covered by every existing example. Git history is the historical
  reference.
- **Plain dataclass for config worked — but only after a protocol
  redesign.** Initial attempt with the existing `connect(config:
  FxtrServiceData)` shape rejected the dataclass at
  `validate_fxtr_service_data` (a plain Python dataclass isn't a
  `FxtrServiceData` primitive). Rather than fall back to a tuple, we
  generalized `SandboxConnectProto` to `SandboxConnectProto[T, U]`
  with `connect(config: T) -> ServiceRef[U]`. The host
  `replace_service_type`s the spawn ref to
  `SandboxConnectProto[NestedRelayConfig, NestedRelayProto]`, the
  typed-service framework's args codec runs the dataclass through
  the dialect (mashumaro encodes plain dataclasses), and the connect
  result comes back already-typed as `ServiceRef[NestedRelayProto]`
  (no second `replace_service_type` on the result needed). Wire
  format is structurally identical to the old shape, so existing
  callers using untyped `connect(...)` keep working unchanged
  (`SandboxConnectProto`'s default-untyped form is
  `SandboxConnectProto[FxtrServiceData, UnknownService]`). See "What
  emerged during implementation" below for full notes.

## Out of scope / deferred

- **Fan-out variants** — outer spawning N inners concurrently, or
  multiple outers spawning a shared inner — both are interesting but
  are not in the design contract. Add when there's a concrete need.
- **Concurrent-connect torture tests** — the original sketch had a
  test for many concurrent connects from one outer. Skipped because
  per-call spawn/teardown already exercises the relevant machinery
  in series; concurrent-spawn-from-inside-a-sandbox correctness
  isn't a new code path beyond what the host's spawn flow already
  tests under stage 1's `test_spawn_once_connect_twice` and
  similar.
- **Mid-call cancellation tests** — also from the original sketch.
  The dispatch-scope-drives-teardown semantics already give us
  this: cancelling the outer's call drops its bridges, which drops
  the inner spawn ref, which tears the inner down. Adding a test
  here would be useful insurance, but isn't required to declare
  stage 4 done. Move to a follow-up if it bites in practice.
- **A way for the host to forcibly tear down a misbehaving
  sandbox.** Open-question from the original sketch. Defer; only
  plumb a cancel scope if a real motivating case shows up.

## Likely gotchas

- **Plain dataclass in service-dialect.** If the dialect doesn't
  decode plain dataclasses, the fallback is a tuple; this is a
  small change, not a blocker. If it does work, great — note the
  pattern so future demos know it's an option.
- **Outer's `TransporterProto` mirror.** `replace_service_type` only
  needs the method shape to match; we don't need to share UUIDs
  with `transport_task` because the outer never mounts a
  `TransporterProto` impl (the inner does, and the outer just calls
  through the bridged ref). Same precedent as the host-side test
  files that re-declare protocols structurally.
- **Multiple resolves in one method.** The outer's
  `relay_http_get` resolves the spawner ref, the spawn-result
  sandbox ref, the connect-result inner ref, and finally the
  authorizer indirectly via the inner. All resolves use
  `get_active_scope()` (the outer's call scope). If one of these
  silently uses a different scope, the bridge graph could leak
  across calls. Worth a careful read during implementation.
- **Container-inner test image freshness.** If the existing
  `setup.sh` for `container_transport_task` hasn't been re-run
  since the most recent adapter changes, the image will be stale
  and the test will fail. Document the rebuild step in the test
  module's docstring + `tests/test_container_spawner.py`-style
  skip-on-missing-image helpers handle the absence cleanly.

## What emerged during implementation

- **`SandboxConnectProto` is now generic `[T, U]`.** The original
  plan's "fall back to tuple" path was a code-smell flag; the real
  fix was to make the protocol generic over the config type and the
  user-service type. See the "Plain dataclass" decision above and
  the matching protocol shape in
  `projects/fxtr_sandboxing/fxtr_sandbox_types/fxtr_sandbox_types/protocols.py`.
  The default-untyped form
  `SandboxConnectProto[FxtrServiceData, UnknownService]` keeps every
  existing test working without modification — they just don't
  exercise the typed-config encoding path.
- **Container-inner test passed without an image rebuild.** The
  protocol's wire format is structurally identical pre- and
  post-generic: `SANDBOX_CONNECT_TYPE_ID` is unchanged, and the
  guest's `_GuestMainImpl` uses the default-untyped form so its
  args-codec wire shape is the same as before. The host's
  specialized `replace_service_type(SandboxConnectProto[MyConfig,
  MyProto])` produces wire-compatible bytes because the dialect
  encodes `MyConfig` to a `FxtrServiceData`-shaped value (a dict),
  which is exactly what an untyped guest expects to receive.
  Worth re-checking if anyone changes the SHARED_SANDBOX_NS or any
  of the guest-side codec generation, but for stage 4 this means
  nothing in the existing container images needs rebuilding.

## Update CODE_OVERVIEW.md

(Last step, also bumped after major sub-features per CLAUDE.md.)
- [x] Status of the rewrite — stage 4 done, with protocol redesign callout.
- [x] Spawn → connect — describe the typed-narrow path via
      `replace_service_type(SandboxConnectProto[T, U])`.
- [x] New "Nested spawn" key concept section.
- [x] `examples/nested_relay_task/` listed in the file tree.
- [x] `tests/test_nested_spawn_wasm_inner.py` and
      `tests/test_nested_spawn_container_inner.py` listed.
- [x] `examples/old_broken/` and `tests/old_broken/` removed from
      the file tree.
- [x] Old "**Note**: requires an image rebuild" stale callout
      cleared (post-stage-3 image rebuild has happened; container
      tests pass).
