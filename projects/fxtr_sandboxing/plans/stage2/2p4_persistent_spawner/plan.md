# Stage 2p4 — Persistent (warm-builder) spawner

## Goal

Stop re-creating the podman **builder container** on every sandbox spawn. Give
the nix-runner backend a small, reusable primitive that keeps a repo's builder
warm across a declared scope, and have the spawner declare that scope for the
lifetime of a `create_sandbox_environment`. The same primitive should be
adoptable, unchanged, by the other per-call `builder_for` consumers (the
workstation MCP's `retrieve_nix_entity`, the nix dispatcher).

## Background

The container-spawn path was profiled in `spikes/sandbox_spawn_benchmark`
(see its `PLAN.md` for the full breakdown). After the `--stop-timeout=0` fix, a
**warm** container spawn is ~4.0s, and the largest single remaining piece is
`builder.start` at ~1.66s — **incurred on every spawn**:

```
spawn.build_and_deliver  ~2.84s
  builder.start          ~1.66s   podman run (~1.1s) + bootstrap + nix-daemon launch/poll
  nix.eval.kind          ~0.39s
  nix.build.image        ~0.46s   (cached)
  deliver.image          ~0.16s   (cache hit)
  builder.stop           ~0.18s
container.sidecar_tunnel ~0.60s
container.run_and_accept ~0.47s   (guest boot + in-container fxtr2 import)
```

Why it recurs: `PodmanBackend` already refcount-*shares* a builder — its
`RefcountedResourceRegistry[Path, _Builder]` runs `_start_builder` only on the
0→1 borrow transition and `_stop_builder` on 1→0. But each spawn arm does
`async with self._backend().builder_for(repo): …` and closes it before the next
spawn, so refcount returns to 0 between spawns and the container is torn down +
re-created every time. Nothing holds a borrow *across* spawns.

The nix-runner mental model already anticipates this
(`projects/fxtr_nix_runner/CODE_OVERVIEW.md`): *"a long-lived consumer holds one
backend and reuses it, so concurrent same-repo borrows refcount-share a
builder"* — and the deferred "env-lifetime builder handle" is called out in
`stage9post1_backend_config_wizard.md`. This stage builds it.

## End-user semantics

No API change for spawn callers. Observable change: within one
`create_sandbox_environment`, the builder container + its nix-daemon are created
**once, on the first nix spawn** (and the deliverer once, on the first delivery
that misses the image cache), and they stay alive until the env tears down.
Second and later spawns skip `builder.start` / `builder.stop` entirely.

- Warm spawn drops ~4.0s → ~2.2s (removes `builder.start` + `builder.stop`).
- An env that never does a nix spawn still starts **no** builder (lazy — the
  "needs no config until first nix use" invariant is preserved).
- Cold first spawn is unchanged (it always had to start the builder).

## Design

### The primitive: `Backend.keep_builder_warm(repo)`

A new async-CM method on the `Backend` protocol (`fxtr_nix_runner/_backend.py`):

```python
def keep_builder_warm(self, repo: Path) -> AbstractAsyncContextManager[None]:
    """Pin repo's builder warm for the block. The first builder_for(repo)
    inside starts it; it then stays alive between borrows (instead of tearing
    down at refcount 0) until this block exits. Lazy (no builder is started
    just by entering) and reentrant. No-op on the host backend."""
```

It yields nothing: consumers keep calling `builder_for(repo)` exactly as today;
the pin just changes *when* the underlying container is torn down. That makes
adoption a one-line wrap around existing code, for the spawner and for the
MCP/dispatcher later.

- **Host backend** (`_host.py`): trivial `@asynccontextmanager` that just
  `yield`s — there is no container.
- **Podman backend** (`_podman.py`): delegates to a new registry `pin` (below),
  keyed on `git_repo_root(repo)` — the same key `builder_for` uses, so the pin
  and the per-spawn borrows refer to the same `_Builder`.

### Registry support: `RefcountedResourceRegistry.pin`

`_lifecycle.py` gains a second, orthogonal counter alongside the borrow count.
Today the registry tracks, per key: the resource (or absent) + a borrow count;
teardown fires when borrow count hits 0. Add a **pin count**:

```
per key:  resource | absent,  borrow_count,  pin_count

borrow(key):                       # unchanged externally
  0→1 borrow_count and resource absent → run factory
  on exit: borrow_count -= 1; maybe_teardown(key)

pin(key):                          # new
  pin_count += 1                   # does NOT instantiate
  on exit: pin_count -= 1; maybe_teardown(key)

maybe_teardown(key):               # run teardown iff nothing wants it anymore
  if borrow_count == 0 and pin_count == 0 and resource present:
      teardown(resource); drop resource
```

Invariants preserved: factory/teardown still run **outside** the bookkeeping
lock (so one key's slow start/stop never blocks another). The only new rule is
that teardown is gated on `pin_count == 0` too. Lazy falls out naturally: `pin`
never calls the factory, so a pin with no borrow leaves the resource absent; the
first `borrow` instantiates it; subsequent 1→0 borrow cycles don't tear down
while pinned; releasing the pin (with no active borrows) tears it down.

This keeps `pin` a general lifecycle primitive, not a podman-specific hack.

### Spawner adoption (`fxtr_sandboxing/spawner.py`)

`SandboxSpawnerImpl` opens exactly one pin, lazily, for the env's configured
repo, and holds it for the env lifetime:

- New fields: `_warm_stack: AsyncExitStack` (env-lifetime), `_warm_lock:
  anyio.Lock`, `_warm_pinned: bool = False`.
- New helper:

  ```python
  async def _ensure_builder_warm(self, repo: Path) -> None:
      async with self._warm_lock:            # concurrent first-spawns → one pin
          if not self._warm_pinned:
              await self._warm_stack.enter_async_context(
                  self._backend().keep_builder_warm(repo))
              self._warm_pinned = True
  ```

- Both nix arms call `await self._ensure_builder_warm(repo)` immediately before
  their existing `async with … builder_for(repo) as h:` block. The arms are
  otherwise unchanged — the `builder_for` borrow is now a cheap shared borrow
  (refcount 1→2→1), so `builder.start`/`builder.stop` no longer fire per spawn.

The pin is a `keep_builder_warm(repo)` where `repo == self._require_git_repo()`
(the env's single `git_repo_path`), so one pin covers every spawn in the env.

### Teardown ordering

`_warm_stack` must be closed at env teardown, and only after all nix work is
done. This is safe because the builder handle is used **only** during a spawn's
build+deliver phase, which always completes before that `spawn()` call returns
(the spawner's `env_tg.start(runner, …)` awaits until the runner reports
`started`, which is after build+deliver+run). By the time the env CM tears down,
no build+deliver is in flight.

`create_sandbox_environment` closes the stack during its existing teardown. The
spawner exposes `async def aclose_warm_builder(self)` (closes `_warm_stack`);
the env `await`s it in the teardown sequence (before `env_tg` exits). Note the
pin is *entered* from a spawn task (on `env_tg`) and *exited* from the
env-teardown task — fine, because `registry.pin`/`borrow` are task-agnostic
(they only touch the lock + counters, never task identity).

### Concurrency

- Lazy pin-init is guarded by `_warm_lock` so two concurrent first-spawns don't
  both enter a pin (which would be two pins — harmless via refcount, but we want
  exactly one and a stable `_warm_pinned` flag).
- Sharing one warm builder across concurrent spawns is safe: all spawns target
  the same repo (one `_Builder`, one handle shape), and nix-daemon is built for
  concurrent clients (`podman exec nix …` in parallel is fine).

### Robustness: warm-builder daemon death

New risk introduced by warmth: today a broken nix-daemon self-heals because the
next spawn gets a fresh builder; with a pinned builder, a dead daemon would
poison the rest of the env. Options (decide before shipping; can be a fast
follow):

1. **Recycle on transport failure (recommended).** A `podman exec` against a
   gone/stopped builder fails distinctly from a nix build error (podman's own
   error + rc 125, *not* nix returning nonzero) — so this can be detected
   without the stderr-classification the codebase avoids for nix errors. On that
   signal, the podman backend drops the cached `_Builder` (so the next
   `builder_for` re-runs the factory even while pinned) and the spawn retries
   once.
2. **Health-probe on reuse.** Cheap `podman exec test -S <daemon socket>` when a
   *pinned, already-existing* builder is borrowed; recycle if dead. Costs a
   roundtrip (~0.1s) per spawn — partially erodes the win.
3. **Document-and-accept (v0).** Ship warmth without recycle; a dead daemon
   fails the env. Lowest effort; revisit if it bites.

Shipped v0 as (3) (warmth landed). Option (1) is specced in **Follow-up:
transport-failure recycle** below and is the next step.

## Reuse by other consumers (not in this stage)

The workstation MCP's `retrieve_nix_entity` and the nix dispatcher have the same
per-call `builder_for` pattern and the same per-call builder churn. Once
`keep_builder_warm` exists, each can wrap its session/request scope in
`async with backend.keep_builder_warm(repo):` for the same win. Out of scope
here, but the primitive is designed for it (hence backend-level, not a private
spawner field).

## Testing

- **Registry (`test_lifecycle.py`)**: pin does not instantiate; first borrow
  under a pin instantiates once; borrow 1→0→1 cycles under a pin do not tear
  down; releasing the pin with no active borrows tears down; a borrow that
  outlives the pin still tears down at its own 1→0; concurrent pin+borrow.
- **Backend (`test_host_backend.py` / `test_podman_backend.py`)**: host
  `keep_builder_warm` is a no-op CM. Podman (live): under a pin, two sequential
  `builder_for` borrows reuse one container (same id) and `_start_builder` runs
  once; after the pin closes the builder is gone.
- **Spawner E2E (`test_nix_container_spawner.py`)**: wrap the existing
  two-spawn test in `perf.collect_perf()` and assert exactly **one**
  `builder.start` event across both spawns (regression lock tying the perf
  instrumentation to the warmth guarantee). Add a no-nix-spawn env test that
  asserts **zero** `builder.start` (laziness preserved).
- **Recycle (if implementing option 1)**: kill the warm builder mid-env
  (`podman rm -f` it out from under the spawner) and assert the next spawn
  recovers.

## Implementation steps

- [x] `RefcountedResourceRegistry.pin` + the `pin_count`/`maybe_teardown`
      changes; unit tests in `test_lifecycle.py`.
- [x] `Backend.keep_builder_warm` on the protocol + host (no-op) + podman
      (delegates to `registry.pin(git_repo_root(repo))`); backend tests
      (host no-op; podman covered via the spawner E2E).
- [x] Update `fxtr_nix_runner/CODE_OVERVIEW.md` (the new primitive + registry
      pin).
- [x] Spawner: `_warm_stack` / `_warm_lock` / `_ensure_builder_warm` +
      `aclose_warm_builder`, called from both nix arms; wire teardown into
      `create_sandbox_environment`.
- [x] Spawner E2E test: the existing two-spawn greeter test now asserts
      `builder.start` fires ≤ 1× across both spawns (0 on host, 1 on podman).
- [x] Update `fxtr_sandboxing/CODE_OVERVIEW.md` (persistent-builder behavior).
- [x] Re-run `spikes/sandbox_spawn_benchmark` to confirm warm spawn ~4.0s →
      ~2.2s; record the new breakdown in that spike's `PLAN.md`.
- [x] Recycle-on-transport-failure (robustness option 1) — implemented; see the
      Follow-up section's own checklist below.

## Follow-up: transport-failure recycle (option 1)

Recover when a warm builder's container dies mid-env (VM restart, a concurrent
`fxtr_nix clear`, manual `podman rm`, OOM). Pre-warm, this self-healed because
each spawn got a fresh builder; the warm pin removes that, so we re-add it
explicitly. This is **entirely internal to the podman backend** — the `Handle`
protocol shape is unchanged (only a documented contract refinement on
`run_nix`), the host backend is untouched, and recycle is opt-in at the one
catch site, so no other consumer must change.

### Detection: exit code 125, not stderr

A `podman exec <cid> nix …` has three failure shapes:

- **nix build/eval error** — the exec ran; nix exited nonzero (1 / 100 / …).
  Normal `NixResult`, must **not** recycle.
- **container gone / not running** — `podman exec` can't start the session and
  returns **rc 125** (podman's "command couldn't execute" code; nix never uses
  125). This is the transport-failure signal — detectable from the **exit
  code**, so it honors the codebase rule of not classifying nix stderr.
- **container alive, daemon dead** — exec runs, nix returns rc 1 with a
  daemon-connection error. Indistinguishable from a build error without stderr
  parsing → **out of scope** (documented limitation). The common death is the
  whole container going (VM restart / clear / rm), which is the rc-125 case;
  because the first builder exec in a spawn (`nix eval` for the kind check, or
  `_read_platform`'s `cat`) hits it, the whole build+deliver retry recovers it.

The **deliverer** container has the exact same staleness risk, and it's one
2p4 *introduced*: `_ensure_deliverer` caches `deliverer_id` on the `_Builder`
and returns it with no liveness check, so once it's warm a dead deliverer
fails **every** future cache-miss delivery (`podman exec <dead deliverer>
skopeo` → rc 125), not just a mid-copy one. (Pre-2p4 the deliverer was
recreated with the per-spawn builder, so it self-healed.) So the deliverer
skopeo exec gets the same rc-125 check — recycled at its own granularity
(below) so a deliverer-only death doesn't rebuild a healthy builder. Only
cache-miss spawns touch it (`image_in_runtime` is a host-side inspect, not an
exec), which is exactly the path recycle is for.

### Signaling path (handle → backend → registry)

`PodmanHandle` learns a backref for invalidation. `builder_for` constructs it
with the backend + the repo-root key it already computed:

```python
# _podman.py: builder_for(repo)
yield PodmanHandle(builder, self._config, target=…, backend=self, repo_root=repo_root)
```

A single exec wrapper centralizes the rc-125 check for **both** the builder
container (used by `run_nix`, `_read_platform`, `_build_skopeo`) and the
deliverer container (the skopeo copy in `deliver_image`), recycling at the right
granularity for each:

```python
# PodmanHandle
async def _exec(self, container_id, argv, *, on_dead) -> NixResult:
    rc, out, err = await _run([PODMAN, "exec", *argv_with(container_id, argv)])
    if rc == 125:                       # transport failure: that container is gone
        await on_dead()                 # builder → invalidate registry entry;
        raise BuilderTransportError(self._repo_root, err)   # deliverer → clear id
    return NixResult(rc, out, err)

# builder execs (run_nix, _read_platform, _build_skopeo):
#   on_dead = lambda: self._backend._invalidate_builder(self._repo_root, self._builder)
# deliverer exec (skopeo copy):
#   on_dead = self._recycle_deliverer   # clears builder.deliverer_id + rm -f the corpse
```

`run_nix` becomes `self._exec(builder_id, ["nix", *argv], on_dead=<invalidate builder>)`.
`BuilderTransportError(PodmanError)` is one exception covering either container's
death — the failing exec has already done the correct invalidation before
raising, so the retry loop stays agnostic.

`_recycle_deliverer` just clears the cached field (under `deliverer_lock`) so the
next `_ensure_deliverer` recreates it; it does **not** touch the warm builder:

```python
async def _recycle_deliverer(self) -> None:
    b = self._builder
    async with b.deliverer_lock:
        dead, b.deliverer_id = b.deliverer_id, None
    if dead is not None:
        await _run([PODMAN, "rm", "-f", dead])   # best-effort; no-op if gone
```

`PodmanBackend._invalidate_builder(key, builder)` delegates to the registry:

```python
# RefcountedResourceRegistry
async def invalidate(self, key, resource) -> None:
    """Drop a resource believed dead so the next borrow rebuilds it. Keeps the
    entry (and any pin), so a pinned warm builder re-warms on the next borrow.
    Identity-checked: a no-op if the resource was already replaced."""
    async with self._guard:
        entry = self._entries.get(key)
        if entry is None or entry.resource is not resource:
            return                      # already recycled / gone
        entry.resource = None           # next _acquire sees "not built" → rebuilds
    # best-effort teardown of the dead resource, outside the guard, shielded +
    # bounded like _decref (podman rm -f of a gone container just no-ops).
    await self._teardown_bounded(resource)
```

Why setting `resource = None` is safe with the dying borrow still live: the
failing borrow's `_decref` reads `entry.resource` (now `None`) → tears nothing
down, and with the spawner's pin held (`pincount ≥ 1`) the entry survives to be
rebuilt. Concurrent double-failures are handled by the identity check (second
`invalidate` is a no-op) and the existing creator/waiter path in `_acquire`
(one rebuilds, the other shares).

### Retry: once, around the whole build+deliver

The recycle is driven at the one place that owns the `builder_for` block — the
spawn arms — so a death *anywhere* in eval→build→deliver recovers by redoing the
sequence against a fresh builder:

```python
for attempt in range(2):
    try:
        async with perf_span("spawn.build_and_deliver"), builder_for(repo) as h:
            await self._check_service_kind(h, defn, "container")
            image_json = await nix.build_image(h, …)
            delivered = await h.deliver_image(str(image_json), runtime=…)
        break
    except BuilderTransportError:
        if attempt == 1:
            raise
        record("builder.recycled")      # perf marker; also proves it in tests
```

Both nix arms get the same wrapper (factor a small helper so the retry policy
lives in one place). `_ensure_builder_warm` need not re-run — the pin still holds
the (now-rebuilt) entry.

### run_nix contract note

`Handle.run_nix`'s docstring gains: *returns a `NixResult` for nix-level
outcomes (including nonzero nix exits); may raise a backend transport error only
when the builder transport itself is unusable (podman backend: the builder
container is gone).* The host backend never raises it. Consumers that don't
catch it see it as an error — the same as any un-recovered build failure — so
this is backward-compatible for everyone who doesn't opt into recycle.

### Limitations (documented, not handled here)

- Daemon-dead-but-container-alive: the builder container is up but its
  `nix-daemon` process crashed, so nix returns rc 1 (not 125) with a
  daemon-connection error. Detecting it needs the nix-stderr classification we
  deliberately avoid, so it's not auto-recovered. (Rare; a `nix-daemon` crash
  independent of its container is unusual.)

### Tests

- Registry: `invalidate` drops + rebuilds under a pin (next borrow gets a fresh
  resource, factory called again); identity mismatch is a no-op; teardown of the
  dead resource is attempted.
- Podman (live): between two spawns, kill the warm **builder** out from under
  the env (`podman rm -f` by label); assert the second spawn recovers and emits
  a `builder.recycled` marker (via `collect_perf`). A second case kills only the
  **deliverer** (leaving the builder alive) before a cache-miss delivery and
  asserts it recovers without rebuilding the builder (the builder container id is
  unchanged across the recycle).

### Implementation steps (follow-up)

- [x] `RefcountedResourceRegistry.invalidate` + `_teardown_bounded` refactor;
      unit tests.
- [x] `BuilderTransportError` (exported), `PodmanHandle` backref + `_exec`
      wrapper routing the builder execs (`run_nix` / `materialize` /
      `_read_platform` / `_build_skopeo`, on_dead = invalidate registry entry)
      **and** the deliverer skopeo copy (on_dead = `_recycle_deliverer`);
      `PodmanBackend._invalidate_builder`; `run_nix` docstring note.
- [x] Spawn-arm `_build_with_recycle` retry-once helper wrapping both nix arms.
- [x] Tests: registry unit (`invalidate`), backend `_exec`/`_recycle_deliverer`
      unit + live `test_warm_builder_recycled_after_death`; CODE_OVERVIEW updates.
      (A dedicated live *deliverer*-only kill test is left as future work — the
      bookkeeping is unit-tested and the whole-VM death path is covered live.)

## Open questions / future

- Should `keep_builder_warm` ever be *opt-out* at the env level (e.g. a spawner
  that wants aggressive teardown)? Default automatic seems right; add a knob
  only if a caller needs it.
- After this, the next warm-spawn levers (from the benchmark) are: collapsing
  `nix.eval.kind` into the build (one `podman exec nix` instead of two) and the
  per-spawn sidecar tunnel (~0.6s) + guest boot / fxtr2 import (~0.5s). Separate
  stages.
