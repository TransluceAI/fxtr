# Stage 2p4 (part 3) — Warm-spawn latency low-hanging fruit

## Goal

Shave the cacheable/redundant cost out of a **warm** container spawn (~2.35s
today, measured by `spikes/sandbox_spawn_benchmark`) without touching the
structural costs (per-spawn isolation, container boot under emulation). The
target is the common hot path — spawning **many instances of the same
service** — where almost all of the `build_and_deliver` phase is redundant.

## Spike findings (podman 5.8.2 / applehv, warm builder)

Timed inside a warm builder (min of 5 runs each):

| step | cost |
|---|---|
| `nix --version` (nix startup + `podman exec` RTT floor) | ~185 ms |
| A) eval `kind` (`--apply 's: s.kind or null'`) | ~380 ms |
| B) build `#attr.image` (direct attr path — **eval-cache hit**) | ~445 ms |
| deliver cache-hit check (`podman exec cat` + `image inspect`) | ~160 ms |
| E) eval `#attr.kind` (direct path, no `--apply`) | ~435 ms |

Two results that **killed the idea we were leaning on** (merging the kind read
and the image derivation into one eval):

1. **The flake eval cache accelerates `nix build`, not `nix eval` of a
   derivation.** `nix build #attr.image` is ~459 ms because the eval cache
   stores the derivation-relevant facts (`drvPath` / `outPath` / is-a-deriv)
   that `build` needs — no image-expression eval. But **any `nix eval` that
   forces `.image` is ~2s**, and this is *not* about `--apply`: measured, all
   three of `--apply s.image.drvPath`, a **direct-path** `#attr.image.drvPath`,
   and `--expr` + `builtins.getFlake` are ~2.0–2.1s — a full, real eval of the
   nix2container derivation. So there's no cache-fast way to get the drvPath via
   `eval`; `nix build` is the only fast way to touch the image, and it realizes
   rather than just returning the path. The merge (get kind+drv in one eval,
   then realize) is therefore ~3–4× *worse*. **Dead.**
2. **The kind eval can't be made cheaper, only skipped.** Direct-path
   `#attr.kind` (~435 ms) ≈ the `--apply` version (~380 ms) — `.kind` is a
   trivial string, cheap to eval either way; both are dominated by the ~185 ms
   nix-startup + flake-resolution floor that every separate `nix` invocation
   pays. Switching eval forms saves nothing.

Conclusion: the eval/build/deliver costs are per-`nix`-invocation floors that
can't be shrunk in place — the only lever is to **not run the invocation** on
repeat spawns. That's caching, not merging.

## Design — three caches, all in-env, all keyed by immutable inputs

### 1. Build-pipeline cache (the big win)

`(git_commit, flake_attr, flake_path)` fully + immutably determines the build
outputs (`?rev=` is a frozen commit; results are content-addressed). Cache the
*end* of the pipeline per that key, on the spawner (env-lifetime, next to the
warm-builder pin):

- **Container arm:** cache the `DeliveredImage` (`image_ref` + `platform`). On a
  repeat spawn, skip the whole `build_and_deliver` block (kind eval + build +
  deliver) and go straight to `_run_nix_container_internal(cached.image_ref,
  cached.platform)`.
- **Wasm arm:** cache `(entry_point, site_packages_path)`. Repeat spawns skip
  `eval_entry_point` + `build_site_packages` + `materialize`.

```python
# SandboxSpawnerImpl
self._delivered_cache: dict[tuple[str, str, str | None], DeliveredImage] = {}
...
key = (defn.git_commit, defn.flake_attr, defn.flake_path)
delivered = self._delivered_cache.get(key)
if delivered is None:
    await self._ensure_builder_warm(repo)
    delivered = await self._build_with_recycle(repo, _build_and_deliver)  # kind+build+deliver
    self._delivered_cache[key] = delivered
async with _run_nix_container_internal(delivered.image_ref, delivered.platform, …):
    ...
```

Savings on a repeat spawn: ~380 (kind) + ~445 (build) + ~160 (deliver) ≈ **~1.0 s**.
First spawn of a defn is unchanged (populates the cache). The kind *safety
check* is preserved — it just runs once per defn instead of every spawn (the
cached entry means the defn already validated).

**Validity / staleness.** The key is immutable, and the built store path (shared
`/nix` volume) + the delivered image (podman image store) survive builder
recycles, so the cache stays valid for the env's life. The only way an entry
goes stale is an external `nix store gc` / `podman image prune` mid-env (we
never trigger these). If that ever bites, a cached-but-gone image surfaces as a
run failure; a follow-up could re-validate on miss (`image_in_runtime` before
trusting the cache) — not built in v0.

### 2. Cache `podman_is_remote`

`container.runtime_detect` runs `podman info` (~100 ms) **every** spawn, but the
answer can't change during a process's life. Cache it (module- or backend-level,
like `PodmanBackend._filter_syscalls`). Saves ~100 ms on *every* spawn (first +
repeat). Trivial.

### 3. Pin the sidecar host key once per sidecar image

`open_sidecar_tunnel` does a `podman exec <sidecar> cat …ssh_host_ed25519_key.pub`
(~100 ms) **every** spawn to pin the host key. But the key is baked into the
(content-addressed) sidecar image at build time, so it's identical for every
sidecar from that image. Fetch it once per image ref and cache it (keyed by the
sidecar image tag). Saves ~100 ms on every spawn.

## Expected outcome

- **Repeat spawn of the same service: ~2.35 s → ~1.2 s** (cache 1 removes
  ~1.0 s; caches 2+3 remove ~0.2 s). This is the hot path for fan-out
  (N workers of one service).
- **First spawn of a new service: ~2.35 s → ~2.15 s** (only caches 2+3 apply).

The remaining ~1.2 s (sidecar tunnel + container run + guest Python/fxtr2 boot
under Rosetta) is the structural floor — see "Punted".

## Punted (deliberately, with the reason)

- **Merge kind-read + image build into one eval** — Dead (spike: ~3–4× slower).
  The reason isn't `--apply` specifically: the flake eval cache serves
  `nix build`'s drvPath/outPath lookup, but *any* `nix eval` that forces the
  `.image` derivation is ~2s — measured for `--apply`, a **direct-path**
  `#attr.image.drvPath`, and `--expr`+`getFlake` alike. So there's no cache-fast
  way to get the drvPath by eval; `nix build #attr.image` is the only fast way
  to touch the image. (Also, even setting caching aside, `nix eval` is
  single-installable and `nix build` only takes derivations, so there's no one
  call returning a *string* attr + a *derivation*; and there's no persistent nix
  *evaluator* — the nix-daemon is store-only — so each call re-pays the ~185 ms
  startup floor. You *could* have the flake expose `kind` as a buildable and
  `nix build #attr.image #attr.kindText` in one process, but that saves only one
  ~185 ms startup, which the build-pipeline cache dwarfs.)
- **Fold the kind check into the build** (drop the eval, interpret a build
  failure) — saves ~380 ms but only until cache 1 lands (which already skips it
  on repeats), and it costs the friendly kind-mismatch error, the no-`kind`
  guard, and forces nix-stderr classification (which the codebase avoids). Not
  worth it once cache 1 exists.
- **Sidecar tunnel round-trip reduction** (~0.6 s) — the per-spawn pod/volume is
  the security compartment; structural.
- **Guest boot / `import fxtr2` speed + arm64-native images** (~0.5 s) — real
  but a bigger project (import trimming; dropping Rosetta emulation).
- Most of the residual is the macOS podman-machine per-call RTT tax, not a
  fxtr-design cost — native-Linux podman is already materially faster.

## Testing

- **Benchmark** (`spikes/sandbox_spawn_benchmark`): the existing per-phase
  breakdown already shows the win — after cache 1, a repeat iteration's
  `spawn.build_and_deliver` should drop to ~0 (add a `spawn.cache_hit` marker so
  it's visible). Record the new repeat-spawn number in that spike's `PLAN.md`.
- **Cache-hit unit/behavior test**: two spawns of the same defn in one env issue
  the nix eval/build only once — assert via `perf.collect_perf` that
  `nix.eval.kind` / `nix.build.image` appear exactly once across two spawns
  (mirrors the `builder.start` ≤ 1 assertion already in
  `test_nix_container_spawner`).
- `podman_is_remote` cache: unit test that a second call doesn't re-invoke
  `podman info` (monkeypatch the runner, assert one call).
- Host-key cache: assert the `cat` exec happens once across two tunnels for the
  same sidecar image.

## Implementation steps

- [x] Cache `podman_is_remote` (module memo in `_podman`) + test.
- [x] Cache the sidecar host key per image ref (`_sidecar_host_key`) + test.
- [x] Build-pipeline cache on `SandboxSpawnerImpl` (container `DeliveredImage`;
      wasm `(entry_point, site_packages)`), keyed by
      `(commit, attr, flake_path)`; skip `build_and_deliver` on hit +
      `spawn.cache_hit` perf marker.
- [x] Cache-hit test (the two-spawn container E2E now asserts `nix.eval.kind` /
      `nix.build.image` run once + `spawn.cache_hit` once across two spawns).
- [x] Re-ran the benchmark: **warm repeat container spawn ~2.35s → ~1.08s**
      (build/deliver skipped via cache, `runtime_detect` 0.00s, host-key exec
      gone). Better than the ~1.2s estimate. Remaining ~1.08s = sidecar tunnel
      (~0.54s) + container run/guest-boot (~0.50s) — the structural floor.
- [x] Updated `fxtr_sandboxing/CODE_OVERVIEW.md` (build-pipeline cache; the two
      `_podman` micro-caches). (`podman_is_remote` lives in `fxtr_sandboxing`,
      not `fxtr_nix_runner` — no nix-runner overview change needed.)

## Open questions

- Cache scope: per-spawner (env lifetime) is simplest and matches the
  warm-builder pin. A backend-level build cache could also serve the MCP /
  dispatcher, but they don't spawn repeatedly the way a worker fan-out does —
  start env-scoped.
- Whether to re-validate a build-cache hit with a cheap `image_in_runtime`
  before trusting it (guards against external prune). Cheap (~66 ms) but adds a
  call back; leave out of v0 unless staleness is seen in practice.
