# Stage 3 — Network Transports as Services

Replace the route-name-keyed transport system with token-keyed capabilities.
Each network endpoint is exposed as a `ServiceRef[NetworkHandleAuthorizerProto]`
that mints single-use tokens; sandboxes redeem each token over the
runtime-specific side channel for one byte-stream connection. RPC stays out
of the data path — once the side channel hands a stream back, the token has
served its purpose and bytes flow direct. HTTP semantics live in
`proxy_connection()` exactly as they do today; only the *who's allowed to
talk to me* check changes.

The capability story:

- A `ServiceRef[NetworkHandleAuthorizerProto]` is a "permission to open
  connections on this route" — it can be bridged into sandboxes (or sub-
  sandboxes) without leaking the route's secrets.
- A `(token, witness_ref)` pair is a "permission to open one connection."
  The token is the bearer; the witness bounds its lifetime so unredeemed
  tokens get revoked when their caller-side scope drains.
- A redeemed token (= an open byte stream) is the actual data path. The
  token is gone from the table the moment the side-channel handshake
  completes; revocation after that is a no-op.

## Components at a glance

```
host process                                                       sandbox process
────────────                                                       ───────────────

  ┌────────────────────────────────────────────────┐
  │ TransportConnectionServer                       │
  │   (env-level, one per env)                      │
  │   ─ token table {token: HandlerFn}              │
  │   ─ consume_token(token)  → HandlerFn           │
  │   ─ serve_connection(token, stream, …)          │
  │                                                 │
  │   HandlerFn = sync (ByteStream) -> None         │
  │     - takes ownership of the stream             │
  │     - closes it on its way out                  │
  │     - if it needs async work, start_soon's it   │
  │       on a task group of its choosing           │
  │     - server hands off and returns;             │
  │       does not track handler lifetime           │
  └────────────────────────────────────────────────┘
            ▲                ▲
            │ register       │ consume + serve
            │                │
   ┌────────┴────────┐  ┌────┴─────────────────────┐
   │ _NetworkHandle  │  │ runner.py / container_   │
   │   AuthorizerImpl│  │ runner.py — side-channel │
   │   ─ owns handler│  │ accept paths             │
   │   ─ mints       │  └──────────────────────────┘
   │     token+      │
   │     witness     │
   └─────────────────┘
            ▲
            │
   ┌────────┴───────────────────────────────────────────┐
   │ Environment.add_transport(route)                    │
   │   - opens aiohttp.ClientSession (env exit-stack)    │
   │   - handler = (stream) -> proxy_connection(stream,  │
   │                              route, session)        │
   │ Environment.add_raw_transport(handler)              │
   │   - user-supplied sync handler used directly        │
   │   - handler does its own async spawning if needed   │
   └─────────────────────────────────────────────────────┘

                       ┌────────────────────────────────────────────────┐
                       │ fxtr_sandbox_rpc_adapter                       │
                       │   ─ contextvar(SandboxServiceContext)          │
                       │   ─ connect_transport(token) → ByteStream      │
                       │   ─ get_client(authorizer) → httpx.AsyncClient │
                       │       (gated on [http] extra; runtime error    │
                       │        if httpx/httpcore not installed)        │
                       └────────────────────────────────────────────────┘
                              ▲
                              │
                       ┌──────┴───────────────────────────────────────┐
                       │ Sandbox user code:                            │
                       │   async def run(config, serve):               │
                       │     async with get_client(config.authorizer)  │
                       │       as client:                              │
                       │       r = await client.get("/v1/foo", ...)    │
                       └──────────────────────────────────────────────┘
```

## Steps

### 1. Service protocols (in `fxtr_sandbox_types/protocols.py`)

- [x] Add `NETWORK_HANDLE_AUTHORIZER_TYPE_ID = system_entities.define_type(SHARED_SANDBOX_NS, "network_handle_authorizer")`.
- [x] Add `TOKEN_WITNESS_TYPE_ID = system_entities.define_type(SHARED_SANDBOX_NS, "token_witness")`.
- [x] Define:
  ```python
  @service_protocol
  class TokenWitnessProto(Protocol):
      """Witness bounding a transport token's lifetime.
      While alive, the token can be redeemed. When the witness goes away
      (caller scope drains, or revoke_token() called), an unredeemed token
      is removed from the env's table. Once the token has been redeemed
      (side-channel connect succeeded), revoke is a no-op.
      """
      async def revoke_token(self) -> None: ...

  @service_protocol
  class NetworkHandleAuthorizerProto(Protocol):
      """Capability to open connections on one route.
      get_transport_token() mints a single-use token + a witness ref.
      """
      async def get_transport_token(
          self,
      ) -> tuple[str, ServiceRef[TokenWitnessProto]]: ...
  ```

### 2. `SandboxServiceContext` extension (in `fxtr_sandbox_types/service_context.py`)

- [x] Add `async def open_transport_stream(token: str) ->
      anyio.abc.ByteStream` to the protocol. Each runtime's concrete impl
      uses its own side-channel mechanism. The returned stream is a
      regular anyio `ByteStream` (closeable via `aclose()` / supports
      `async with`); no outer context-manager wrapper. The caller owns
      the lifecycle.
- [x] Update the docstring / module docstring to drop the "stage 3 will
      add ..." TODO and describe the actual surface.

### 3. `TransportConnectionServer` + handler shape (NEW: `fxtr_sandboxing/transport_server.py`)

- [x] `class BadTransportTokenError(LookupError)` — raised on lookup miss.
- [x] Type alias:
      ```python
      type TransportHandler = Callable[[anyio.abc.ByteStream], None]
      ```
      The contract: the handler is invoked synchronously when a
      sandbox redeems a token. It takes ownership of the byte stream
      (including close) and is responsible for any async work — if it
      needs to do anything beyond returning, it should `start_soon` on
      a task group of its choosing. The server hands off and forgets;
      it does not track handler lifetime. The underlying socket is
      owned by the env / runner, so when the env tears down, the
      stream's other end closes and any in-flight handler async work
      sees EOF / a broken stream.
- [x] `make_http_route_handler(route, session, env_tg) ->
      TransportHandler` — tiny factory. The returned sync handler does
      `env_tg.start_soon(proxy_connection, stream, route, session)`.
      ``proxy_connection`` was updated to own the stream's close
      (`async with client_stream:` inside the function), so no wrapper
      helper is needed. The handler does not own the session's
      lifecycle; the env-level exit stack does.
- [x] `class TransportConnectionServer`:
      - Internal: `self._lock = threading.Lock()` guards the token
        dict. The dict is touched from both the host's async loop
        (RPC dispatch for `get_transport_token` / `revoke_token`) and
        the eryx thread (sync callback `consume_token`). CPython
        already makes individual dict ops atomic under the GIL, but
        an explicit lock is cheap and makes the contract obvious.
      - `register_token(token, handler)` — sync; populates the dict
        under the lock.
      - `revoke_token(token)` — sync; idempotent under the lock (no-op
        if missing — handles "consumed before revoke" race).
      - `consume_token(token) -> TransportHandler` — sync atomic
        pop-or-raise under the lock. Raises `BadTransportTokenError`
        on miss.
      - **No** `serve_connection` method. Both the eryx callback and
        the container accept loop catch `BadTransportTokenError` from
        `consume_token` and surface failure their own way (eryx's sync
        callback raises out; the container path writes a
        routing-header error response and closes the stream). Keeps
        the server's responsibilities narrow: registry + atomic
        lookup.
- [x] **Routing-header helpers (in `fxtr_sandbox_types/transport_routing.py`)**:
      `read_routing_header` / `write_routing_header` /
      `read_routing_response` / `write_routing_response`. Length-framed
      JSON over an `anyio.abc.ByteStream`. Lives in
      `fxtr_sandbox_types` so both the host and the in-container shim
      share the same wire-protocol code.

### 4. `Environment.add_transport` / `add_raw_transport` (in `fxtr_sandboxing/spawner.py`)

- [x] Replace the `SandboxEnvironment` frozen dataclass with a regular
      class owning private instance variables: ``_transport_server``,
      ``_exit_stack`` (for async cleanups, e.g. ``aiohttp.ClientSession``
      closes), ``_host_scope`` (so authorizer impls can be mounted),
      ``_env_tg`` (HTTP route handlers ``start_soon`` proxy_connection
      tasks on it). ``spawner_ref`` exposed as a public attribute.
- [x] ``create_sandbox_environment(...)`` constructs the env, opens
      the exit stack, threads ``transport_server`` into
      ``SandboxSpawnerImpl``.
- [x] ``Environment.add_transport(route: TransportRoute) ->
      ServiceRef[NetworkHandleAuthorizerProto]``: builds an
      ``aiohttp.ClientSession``, registers its close on the env's
      exit stack, makes an HTTP route handler, mounts an authorizer
      impl on the host scope, returns its ref.
- [x] ``Environment.add_raw_transport(handler: TransportHandler) ->
      ServiceRef[NetworkHandleAuthorizerProto]``: mounts an authorizer
      impl with the user-supplied sync handler. No session
      registration.

### 5. Authorizer impl + token witness (in `fxtr_sandboxing/network_authorizer.py`)

- [x] `class NetworkHandleAuthorizerImpl(TypedServiceImpl[NetworkHandleAuthorizerProto])`:
      ```python
      async def get_transport_token(self):
          token = secrets.token_hex(32)
          self._server.register_token(token, self._handler)

          call_scope = get_active_scope()
          witness_impl = _TokenWitnessImpl(token, self._server)
          witness_ref = call_scope.mount(witness_impl)

          # Lease-driven cleanup: when call_scope drains, revoke
          # any unredeemed token (idempotent — no-op if already
          # consumed via the side channel or via revoke_token()).
          server = self._server
          call_scope._token.register_end_callback(
              lambda: server.revoke_token(token)
          )
          return token, witness_ref
      ```
      The end-callback approach replaces the original ``weakref.finalize``
      sketch — verified during implementation that finalizer-based cleanup
      doesn't fire promptly because the dispatch scope's drain doesn't
      align with Python GC. ``register_end_callback`` on
      ``call_scope._token`` fires deterministically when the scope's
      lifetime token ends.
- [x] `class _TokenWitnessImpl(TypedServiceImpl[TokenWitnessProto])` —
      simple impl: holds ``(token, server)``, has one
      ``revoke_token()`` method that calls ``server.revoke_token(token)``.
      No finalizer; lifetime-bounded cleanup is wired by the
      authorizer impl via the call-scope end callback above.

### 6. Sandbox-side surface (in `fxtr_sandbox_rpc_adapter`)

- [x] New module `_transport.py`:
      - Module-level `_ctx_var: ContextVar[SandboxServiceContext | None]`.
        Holds the active service context — `connect_transport` reads
        `ctx.open_transport_stream` off it.
      - `async def connect_transport(token: str) -> anyio.abc.ByteStream` —
        thin wrapper that pulls the active ctx out of the contextvar and
        calls `ctx.open_transport_stream(token)`. Raises if the contextvar
        is unset (i.e. the caller invoked it outside
        `fxtr_sandbox_rpc_adapter.main`). Returns the raw anyio
        ByteStream — caller owns the stream's lifecycle.
      - Internal `_set_active_ctx(ctx)` context manager so ``main()``
        can install + reset the contextvar around the entrypoint run.
- [x] New module `_http.py` (gated):
      - `class _AuthorizingBackend` — on each `connect_tcp`, opens a
        child `FxtrScope`, resolves the authorizer **ref** in that
        scope (so the witness returned by `get_transport_token()`
        lands in the same scope), opens the side-channel stream via
        `connect_transport(token)`, wraps it as an httpcore-shaped
        ``AsyncNetworkStream``. The stream's lifecycle is independent
        of the child scope. (Doesn't subclass
        ``httpcore.AsyncNetworkBackend`` — httpcore is a lazy import,
        so we duck-type via structural compatibility.)
      - `def get_client(authorizer_ref:
        ServiceRef[NetworkHandleAuthorizerProto], *, base_url: str =
        "http://proxy") -> httpx.AsyncClient` — primary user-facing
        helper. **Takes a ServiceRef, not a resolved proxy** — the
        backend resolves it fresh inside each `connect_tcp` child
        scope so witness lifetimes track the per-connection scope. If
        the user resolves the authorizer once in a long-lived scope
        and passes the proxy in instead, every minted witness would
        be pinned to that long-lived scope, defeating the prompt
        cleanup the witness pattern is meant to provide. Imports
        `httpx` / `httpcore` lazily at call time; raises an
        informative `RuntimeError` directing the user to install the
        `[http]` extra if either import fails.
- [x] `pyproject.toml` for `fxtr_sandbox_rpc_adapter`: add `[project.
      optional-dependencies]` block with `http = ["httpx", "httpcore"]`.
      `get_client` is the only function affected by this gate; raw
      `connect_transport` works without it.
- [x] In `_main.py`, install the active ctx into `_ctx_var` before the
      `open_rpc_scope` body runs. Reset on exit so a fresh `main()`
      invocation picks up its own ctx.
- [x] Re-export `connect_transport`, `get_client` from the adapter
      package's `__init__.py`. `get_client` is re-exported as a thin
      shim function that imports `_http` lazily on first call so the
      adapter's import chain works without the `[http]` extra
      installed.

### 7. Eryx integration (`fxtr_sandboxing/runner.py`)

- [x] Replace `transports: list[TransportRoute] | None` parameter on `run()`
      with `transport_server: TransportConnectionServer | None`. Spawner
      threads the env's server through.
- [x] Replace the `route_map` / `upstream_sessions` / `transport_names`
      machinery with: when `transport_server` is non-None, register the
      four `_transport_*` callbacks. `_transport_connect(token)` runs in
      the eryx thread, so it can't directly do async work (wrapping a
      raw socket as `anyio.UNIXSocketStream` requires `await`). It
      delegates to a small async launcher via the existing queue:
      ```
      # eryx thread (sync)
      def _transport_connect(token):
          handler = transport_server.consume_token(token)   # raises on bad
          callback_sock, proxy_sock = socket.socketpair()
          conn_id = uuid.uuid4().hex
          connections[conn_id] = callback_sock
          new_proxy_conns.put((proxy_sock, handler))
          return conn_id

      # host async loop (launcher task)
      async def _proxy_launcher():
          while True:
              item = await anyio.to_thread.run_sync(new_proxy_conns.get, ...)
              if isinstance(item, _ShutdownSentinel): break
              proxy_sock, handler = item
              stream = await anyio.abc.UNIXSocketStream.from_socket(proxy_sock)
              handler(stream)   # sync handoff; handler owns stream from here
      ```
      The launcher is now just a sync→async bridge; it no longer holds
      a task group of its own (the handler manages whatever async work
      it spawns). HTTP route handler does
      `env_tg.start_soon(proxy_connection, ...)` from inside its
      sync body; raw handlers do whatever they want.
      Bad-token raises propagate out of the eryx callback so the sandbox
      sees the error directly — no separate handshake response needed
      since the callback's success/failure already conveys that.
- [x] `EryxServiceContext` (in `_runner.py`) gains an
      `open_transport_stream(token)` method. Implementation: register two
      additional eryx callbacks (or reuse the existing `_transport_*`
      callbacks via a thin async wrapper) that adapt the byte-level
      callback path into an `anyio.abc.ByteStream`.
      - Implementation: the byte-stream wrapper class lives in
        `_runner.py` itself (alongside `EryxServiceContext`), not in a
        separate injected module. We already have a reference impl in
        the deleted `_proxy_transport.py` (CallbackNetworkStream +
        helpers); the stage-3 version targets `anyio.abc.ByteStream`
        rather than `httpcore.AsyncNetworkStream`. The httpcore-shaped
        backend in `fxtr_sandbox_rpc_adapter._http` builds on top of
        the ByteStream the contextvar exposes.

### 8. Container integration (`fxtr_sandboxing/container_runner.py`)

- [x] Replace `_transports: list[TransportRoute] | None` parameter on
      `_run_container_internal` with `transport_server:
      TransportConnectionServer | None`.
- [x] Routing-header upgrade. New on-the-wire form (no backwards
      compatibility — nothing else speaks the old shape):
      - **Request**: `[4-byte length][JSON {"token": "<hex>"}]`.
      - **Response**: `[4-byte length][JSON {"ok": true}]` on success
        or `[4-byte length][JSON {"ok": false, "error": "<msg>"}]` on
        bad/consumed token. After "ok" the connection is the byte
        bridge (h11 HTTP for routes; raw bytes for connectables).
- [x] `_handle_transport_conn(conn, transport_server)` drives the
      handshake protocol directly (no `serve_connection` shim):
      ```
      try:
          token = await read_routing_header(conn)
      except (anyio.EndOfStream, ValueError) as e:
          await write_routing_response(conn, ok=False, error=str(e))
          await conn.aclose()
          return
      try:
          handler = transport_server.consume_token(token)
      except BadTransportTokenError as e:
          await write_routing_response(conn, ok=False, error=str(e))
          await conn.aclose()
          return
      await write_routing_response(conn, ok=True)
      handler(conn)
      ```
      The handler owns the stream from the sync handoff onward.

### 9. Container-side dialer (`fxtr_service_runner/service_context.py`)

- [x] `ContainerServiceContext.open_transport_stream(token)`:
      ```
      sock = await anyio.connect_unix(TRANSPORT_SOCKET_PATH)
      send_routing_header(sock, token)
      response = await read_routing_response(sock)
      if not response["ok"]:
          await sock.aclose()
          raise BadTransportTokenError(response.get("error", ...))
      return sock  # anyio.SocketStream — already ByteStream-shaped
      ```
- [x] Drop `_MultiplexedBackend` / `_SocketStream` / `get_transport` /
      `get_client` from `fxtr_service_runner/transport.py` — file
      deleted entirely. Routing-header read/write helpers live in
      `fxtr_sandbox_types/transport_routing.py` (shared with the host
      side); the container-side dial logic lives in
      `service_context.py` itself.
- [x] `TRANSPORT_SOCKET_PATH = "/run/fxtr/transport.sock"` stays.

### 10. Spawner wiring (`fxtr_sandboxing/spawner.py`)

- [x] `_spawn_runner_wasm` and `_spawn_runner_container` thread
      `self._transport_server` into `_run_wasm` / `_run_container_internal`.
- [x] `SandboxSpawnerImpl.__init__` gains a `transport_server` parameter,
      held alongside `cache_dir` / `env_tg` / `insecure_registry`.

### 11. Cleanup of old transport surface

- [x] Drop `name` field from `TransportRoute` in `proxy.py` — it was the
      lookup key in the old name-based design and no longer carries
      information. The route's identity is now its
      `(upstream_base_url, injected_headers, allowed_path_prefixes)`
      triple.
- [x] Drop dormant stage-3 plumbing comments from `runner.py` and
      `container_runner.py` (the ones noting "kept for stage 3"). Replace
      with the live versions.

### 12. Examples / smoke

- [x] Add a new dedicated `examples/transport_task` that exercises both
      ``connect_transport`` (raw byte stream) and ``get_client``
      (HTTP). Takes a ``ServiceRef[NetworkHandleAuthorizerProto]`` as
      its config (entry-point first arg, no wrapping dataclass since
      ``DataclassEntity`` fields can't carry a ServiceRef). Methods:
      ``raw_echo``, ``http_get``, ``redeem_explicit_token``.
- [x] **Big entrypoint redesign** (folded in here): the ``serve``-callable
      shape was replaced with "entrypoint returns a ServiceRef". The
      new shape is ``async def run(config) -> ServiceRef[...]``. The
      adapter's ``connect`` calls the entrypoint and returns whatever
      it produces; the framework chain-bridges the result back to the
      host caller. No more per-connect ``user_scope``,
      ``_entrypoint_runner``, ``_copy_service_data_into_scope``, or
      ``serve`` ceremony. ``rpc_task``, ``container_rpc_task``, and
      ``transport_task`` all use the new shape.
- [x] `examples/container_rpc_task` rebuild — source updated for the
      entrypoint redesign (drop `serve`, return `ServiceRef`), image
      rebuilt by the user, full container test suite green.

### 13. Tests

- [x] **`tests/test_transport_token_table.py`** — unit tests for
      `TransportConnectionServer`:
      - `register_token` + `consume_token` round-trip.
      - `consume_token` of a missing token raises
        `BadTransportTokenError`.
      - `consume_token` is single-use: second consume raises.
      - `revoke_token` removes an unredeemed token; `consume` after
        revoke raises.
      - `revoke_token` is idempotent (no-op on already-consumed or
        already-revoked).
      - `register_token` rejects collisions.
      - `serve_connection` ok path: handshake response + handler called.
      - `serve_connection` bad token: error response written, exception raised.
      - `serve_connection` no-handshake mode skips response.
- [x] **`tests/test_transport_witness_lifetime.py`** — host-only RPC test
      using a memory-stream pair, exercising the authorizer/witness flow:
      - Mint a token, drop the witness ref's caller scope (the scope
        the authorizer ref was resolved in), assert the token is
        revoked.
      - Mint, redeem (consume from the table), drop the witness, assert
        the redeemed connection is unaffected.
      - `revoke_token()` explicit call works.
      - Surfaced and locked in the "resolution scope, not active
        scope" lesson — the authorizer must be resolved inside the
        scope you want the witness's lifetime bounded by.
- [x] **`tests/test_transport_raw_e2e_wasm.py`** — uses
      `add_raw_transport(handler)` with a tiny user-supplied async
      handler that echoes the bytes back. Sandbox calls
      `connect_transport(token)` directly (no httpx) and validates the
      round-trip. Surfaced the need for a real eryx half-close
      primitive (``_transport_send_eof``) since the bridge had been
      glossing over half-close; that's now wired through.
- [x] **`tests/test_transport_e2e_container.py`** — combined raw +
      HTTP + token-error e2e tests against a Linux container, using
      a new dedicated `examples/container_transport_task` bundle.
      Skipped automatically without container runtime + pushed image.
      Build / push with
      `bash examples/container_transport_task/setup.sh`.
- [x] **`tests/test_transport_http_e2e_wasm.py`** — full HTTP round-trip
      via `get_client(authorizer)` against a tiny aiohttp upstream
      registered through `add_transport(route)`. Tests: basic GET,
      header injection, path-allowlist denied. Path-filtering test
      surfaced + fixed a pre-existing bug in ``_send_error``: closing
      the connection right after sending an error response races the
      client's outstanding writes and surfaces as ``BrokenPipeError``.
      Fix: ``_send_error`` now half-closes + drains before returning
      so the client can finish writing cleanly.
- [x] HTTP container e2e: folded into the combined
      `test_transport_e2e_container.py` above (`test_container_http_get_round_trip`
      + `test_container_http_header_injection`).
- [x] **`tests/test_transport_token_errors.py`** — sandbox tries to
      redeem an unknown token (raises clear error) and a
      already-consumed token (raises clear error). Exercises the
      eryx callback raise path; container variant deferred to the
      container e2e tests.

### 14. Update CODE_OVERVIEW.md

- [x] Status of the rewrite — stage 3 done.
- [x] Service-shaped sandbox tasks — entry-point shape change.
- [x] Lifetime nesting — drop user_scope.
- [x] SandboxServiceContext — `open_transport_stream`.
- [x] Network transports section — full rewrite (no longer "stage 3 — not implemented").
- [x] Package structure — new files (`transport_server.py`,
      `network_authorizer.py`, `_transport.py`, `_http.py`,
      `transport_routing.py`, `examples/transport_task/`); updated
      adapter package extras and entrypoint shape; new test files.

## Decisions captured here for future reference

- **Single-use, per-connection tokens.** Each `httpcore.AsyncNetworkBackend.connect_tcp`
  call mints a fresh token via the authorizer and redeems it once on the
  side channel. With HTTP/1.1 keep-alive (and HTTP/2) the cost is amortized
  to one mint per *connection*, not per *request*. Stolen tokens are limited
  to one connection's worth of damage.
- **Witness-bounded lifetimes for unredeemed tokens.** Instead of a finalizer
  bolted onto the long-lived authorizer impl, each
  `get_transport_token()` returns `(token, witness_ref)`. The witness is
  mounted on the dispatch scope, chain-bridged back to the caller. When
  the caller's bridge drops, the host-side impl GCs and a `weakref.finalize`
  pulls the unredeemed token from the table. The `revoke_token()` method
  is a manual fast-path; the finalizer is the safety net. Once a token has
  been consumed (= the side-channel connect succeeded), revocation is a
  no-op — the table entry is already gone.
- **`get_client(authorizer)` is the primary surface.** Returns a normal
  `httpx.AsyncClient`; the custom backend handles token minting +
  redemption per connection. `connect_transport(token)` exists as a
  lower-level escape hatch for code that needs the raw byte stream
  (tests, non-HTTP protocols).
- **Convention: open a child scope around per-connection mint+connect,
  and resolve the authorizer ref inside it.** Subtle but important:
  resources returned by a service call have their lifetime bounded by
  the **resolution scope** of the service ref the caller called
  against, not by the scope active at the call site. So the authorizer
  must be passed around as a `ServiceRef[NetworkHandleAuthorizerProto]`
  (never a resolved proxy), and `_AuthorizingHttpcoreBackend.connect_tcp`
  opens a per-connection child scope and resolves the ref in *it*.
  Then the witness returned by `get_transport_token()` lands in that
  child scope, and dropping it on connect_tcp exit drops the witness
  too — prompt revocation if the side-channel connect failed; a no-op
  if the token was consumed before close.
- **Runtime-specific connect lives on `SandboxServiceContext`, surfaced
  via contextvar.** Adding `open_transport_stream(token)` to the protocol
  keeps the contract typed and structurally checked across runtimes;
  the contextvar in `fxtr_sandbox_rpc_adapter._transport` is just a
  late-binding mechanism so user code can call free-function
  `get_client` / `connect_transport` without threading a context object
  through every call site.
- **Token registry is owned by the env's `TransportConnectionServer`.**
  The runner code (eryx, container) doesn't see route data, sessions, or
  the table directly — it just hands `(token, client_stream)` to the
  server. This lets us extend the system to non-HTTP backends (raw
  handlers, future sandbox-to-sandbox networks) without changing the
  runner code.
- **`add_raw_transport` takes a sync handler callable directly.**
  `TransportHandler = Callable[[ByteStream], None]`. The handler is
  invoked synchronously when a sandbox redeems a token; it takes
  ownership of the stream (including close) and is responsible for
  any async work — `start_soon` on a task group of its own choosing.
  The server hands off and forgets; it doesn't track handler lifetime
  or do defensive close. The underlying socket is owned by the env /
  runner, so when the env or sandbox tears down the stream's other
  end closes naturally and any in-flight handler async work sees EOF.
  The HTTP route case is one small factory that captures `(route,
  session, env_tg)` and `start_soon`s `proxy_connection` on env_tg
  inside the sync callable. No env-level bridging, no `Awaitable`
  return type, no separate `ByteStreamConnectable` protocol —
  simplest possible model.
- **`connect_transport(token)` returns the raw `anyio.abc.ByteStream`.**
  Caller owns its lifecycle (`async with` or explicit `aclose()`). No
  outer context-manager wrapper. `get_client(authorizer)` is the
  ergonomic surface; `connect_transport` is the lowest layer we expose.
- **`get_client` is gated behind the `[http]` extra.**
  `fxtr_sandbox_rpc_adapter[http]` pulls `httpx` + `httpcore`. Calling
  `get_client(...)` without the extra installed raises a clear
  `RuntimeError` directing the user to install it. Users that only
  need raw byte access through `connect_transport` don't pay the
  http-stack cost.
- **`TransportRoute.name` is dropped.** It was the lookup key in the
  old name-keyed design; in the token-keyed world it carried no
  information. Routes are now identified by their content
  (`upstream_base_url`, `injected_headers`, `allowed_path_prefixes`).
- **Container handshake protocol is upgraded, not extended.** Old
  routing header was `{"name": ...}` with no response. New is
  `{"token": ...}` with a length-framed JSON response (`{"ok": true}` or
  `{"ok": false, "error": ...}`). Nothing else speaks the old shape, so
  no compat path. The eryx side keeps its synchronous callback model
  — the callback's success/failure conveys what the container's
  handshake response conveys.
- **Per-route `aiohttp.ClientSession` lives at the env level.** One
  session per registered HTTP route, owned by the env's `AsyncExitStack`
  so it's closed deterministically at env teardown. Multiple sandboxes
  spawned in the same env share the upstream pool for a given route.

## Why this composes for nested spawn (stage 4 preview)

Nested sandboxes aren't actually nested at the runtime layer: every
sandbox in an env is a sibling under that env. So if an outer sandbox
hands an inner sandbox a `ServiceRef[NetworkHandleAuthorizerProto]`,
the inner sandbox can call `get_transport_token()` (RPC up to the host
authorizer), get a token, and dial its own (env-owned) side channel
with it. The outer sandbox isn't on the data path. Capability story
still holds: an inner sandbox can only redeem authorizer refs that
have been explicitly bridged into it.

No proxying-up logic. Stage 4 just needs to write the demo + test.

## Open questions

(none — flagged questions all resolved during planning.)
