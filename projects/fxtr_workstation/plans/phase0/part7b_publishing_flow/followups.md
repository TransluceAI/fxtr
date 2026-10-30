# Part 7b — followups (known wants, out of scope for this stage)

Things we've explicitly decided to defer. Each should eventually become its
own plan; recording them here so the part-7 design doesn't accidentally
preclude them.

## FOD lock file + auto-relock — PROMOTED into the plan

**No longer a followup** (2026-06-12, third sync): this is now
[`part2b_fod_lock.md`](part2b_fod_lock.md) — the part-6 scaffold would have
been most of the tool anyway, so we build the real thing once. The mechanism
sketch below is kept as the design seed for that plan. Prior-art context:
nix has no first-class mechanism (the `fakeHash` → copy-the-mismatch-error
dance is the norm); adjacent patterns are the lang2nix deps-hash conventions
(`pnpm.fetchDeps` / `npmDepsHash`), eval-time hash JSON files read via
`builtins.fromJSON`, and external updater tools (`nix-update`, `nvfetcher`).
Purity of the final flake is a hard constraint either way.

Mechanism sketch (from the 2026-06-12 discussion — yes, auto-fixing works):

- **Lock file as the substitution target**: `fod_lock.json` keyed by slot
  name; the flake reads hashes via `builtins.fromJSON (builtins.readFile
  ./fod_lock.json)`; a missing entry defaults to a **sentinel hash** (any
  32 bytes is a valid sha256 value, so encode the slot label into the fake
  hash — each mismatch error's `specified:` hash then uniquely identifies
  its slot, no flake parsing needed). Rewriting JSON keyed by name beats
  sed-ing nix source.
- **Relock loop**: start the rehost → `nix build --keep-going <FOD targets>
  --log-format internal-json` → parse the `hash mismatch` messages
  (`specified:`/`got:`) → update lock entries → repeat until clean (usually
  one round; our FODs don't depend on each other).
- **Staleness caveat (why relock is a mode, not just a fixer)**: a FOD
  whose inputs changed but whose lock entry still holds the old hash can be
  satisfied from the store **silently** (no rebuild, stale data). The
  relock command must force verification — `nix build --check` on the FOD
  targets, or invalidate the targeted lock entries first.
- Optional fast path: run the writer directly on the host with the rehost
  env + `nix hash path` the output — skips nix builds but loses the
  sandbox-reproduces-the-hash verification; optimization at most.
- Part 6's scaffold simply runs this tool to initialize the lock after
  seeding (see `part2b_fod_lock.md` / `part6_example_and_e2e.md`).

## v2/ts redo → retire `fxtr_report_builder`

Decision (2026-06-12, OPEN-6): part 7b does **not** build on `v2/ts` — the MDX
step is `fxtr_report_builder` (Python-driven, tiny self-contained TS
subprocess) plumbed into nix. The long-term fix is to redo `v2/ts` to
properly support:
- provenance propagation (`_provenance_parent` sidecar),
- services,
- the new token/lease-based scope system,

then rebuild the MDX/report path on top of the new `v2/ts` and delete
`fxtr_report_builder`. The deferred TS-native **layer reader/writer** (over
the `layered_file_store` format) belongs to that same effort — when the TS
step becomes a first-class entity producer again, it reads parent layers and
writes its own layer directly (see `design/04_typescript_stack.md` §5 for
what that requires).

## Behaviors-hub hosting of the publishing flow

Decision context (OPEN-9): the local repo entity store is a stand-in
for an arbitrary blob store. The eventual target is running the whole
publishing flow — serving *and* nix builds — on the behaviors hub server,
with the hub as the remote store. The part-7 contract to preserve: layers
record only "remote-resolvable"; the consumer supplies the `DeepFetchable`,
so swapping the local repo entity store for the hub is a serving-script change,
not a format change.

## Expose the rehost to a *remote* builder (host-backend offload)

**Scoping followup** (raised 2026-06-14 from part 1's E2E). The build-time
rehost binds the orchestrating host's loopback (`expose_host_endpoint →
127.0.0.1`) for the host backend. That only works when the build runs **on that
host**. When host nix **offloads** a build to a remote builder — notably macOS
building a *linux* derivation via a builder VM (`ssh-ng://…-vm`, e.g. `virby`) —
the builder can't reach the orchestrator's loopback, so a FOD fetch fails.
Podman sidesteps this (gvproxy gives the container `host.containers.internal`);
the generic remote-builder case is open. Part 1 documents the limitation and
skips the FOD-fetch test on the darwin host backend; this followup is to scope a
real fix.

**Is there a standard solution? No single one — it's three candidate approaches,
with very different "standard-ness":**

1. **SSH reverse tunnel (`ssh -R`).** The *standard* general mechanism for
   "make a host service reachable from a remote builder": open a second SSH
   connection to the builder with `-R <port>:127.0.0.1:<rehost_port>` so the
   builder's localhost forwards back to the rehost. **Builder-config-dependent**
   — needs the builder's SSH coordinates (parse nix's `builders` / machines
   config), and nix manages its *own* build SSH connection so we can't just add
   `-R` to it (open a separate one). Robust; bespoke plumbing, but it mirrors
   the stage-8 reverse-tunnel trick the runner already uses for the podman
   socket. **Most general; lean toward this.**
2. **Run the fetch on the orchestrator as a *native-system* FOD, consumed
   cross-system.** NOTE — this is **not** "one FOD with per-system logic":
   every derivation has a fixed `system`, and `preferLocalBuild` /
   `allowSubstitutes` only bias *which* machine builds it *among machines that
   support its system* — they do **not** let an `x86_64-linux` drv run on a
   darwin host. The viable shape is to author the fetch FOD with the
   *orchestrator's native* system (e.g. `aarch64-darwin` on a mac) so nix
   realizes it on the mac itself (loopback reachable), then have the offloaded
   linux layer consume its content-addressed **output** (pure bytes) as an
   input — cross-system *input* references are fine; nix copies the blob to the
   linux builder. **Caveats:** bakes the publisher's arch into the flake's FOD
   `.drv` (a portability/reproducibility wart for a *published* flake — the drv
   differs per publisher host, though the output doesn't), and only works when
   the orchestrator is itself a usable builder for that native system. More of
   a "publisher builds + serves on one machine" optimization than a clean
   general fix.
3. **Bind a routable address + hand the builder a gateway URL.** Like podman's
   `host.containers.internal`, but there's **no universal gateway name** for an
   arbitrary SSH builder — the host-reachable address (LAN IP, VM gateway) is
   **fully bespoke per builder/VM tool** (vfkit/krunkit/QEMU/cloud differ).
   Least general.

Leaning: (1) is the most robust general mechanism; (2) is a possible macOS-
publisher optimization with a reproducibility wart (a spike could confirm the
cross-system-input ergonomics, but it doesn't avoid the wart). Until a fix
lands, **podman is the canonical backend for networked builds on macOS**, and
the host backend's FOD path is "works where builds are local (linux CI)". (See
`design/07` "Spike results" for the original findings + the 2026-06-14
correction.)

## Dispatcher `set_preset` port onto the manager (RUNNER-8)

The fourth sync promoted `fxtr_nix_runner` to the flake manager and decided
to port the **MCP**'s `retrieve_nix_entity` onto its entities adapter (part 6,
optional), but to **leave the dispatcher** (`nix_configurable_dispatch`)
as-is for now. Its `set_preset` carries extra policy the entities adapter
doesn't model: the batched preset-table reshape (renderers + typeEntities in
one eval), the `(commit, drvPath) → RendererEntity` cache, and eager
id==key validation. Porting it onto the manager (so it too gets transparent
repo-store remote resolution) is a separate follow-up once the manager API
has settled.

## Stage the FOD lock in-build so podman bootstrap works from an untracked lock

**Followup** (raised 2026-06-15 from part 2b). `fxtr_nix update-hashes` reads the
flake at the live workdir, where nix's git fetcher only sees git-*tracked*
files. The tool stages a freshly-created `fod_lock.toml` (`git add`, mirroring
how nix auto-stages `flake.lock`) so the build sees it — which works on the
**host** backend. On the **podman** backend, host-side `git add` of a *new* file
writes a git index the builder's in-container libgit2 rejects (`invalid entry` —
a git/libgit2 version skew across the host↔container boundary), so first-run
*from no lock at all* fails there. Workaround today: commit an (empty)
`fod_lock.toml` first (the part-6 example scaffolds this; in-place rewrites of a
tracked lock are fine on both backends). A real fix would stage via the build
context the way nix stages `flake.lock` in-container (e.g. an `add`/stage op on
the `Handle`), rather than from host git.

## Narrow FOD source inputs (efficiency / hygiene)

**Followup** (raised 2026-06-15). `entityFromTsvPin` already narrows its source
with `builtins.path` to the `fxtr_namespace.toml` marker + every `*.fxtr.tsv`
(`nix/lib/entityFromTsvPin.nix`) — good hygiene: precise inputs, and the produced
layer doesn't depend on `flake.nix`/`fod_lock.toml`/VCS files. This is **not**
correctness-critical (part 2b dropped the `drvPath` staleness fingerprint that
would have made it so). Remaining purely-optional cleanups:
- Other FOD helpers (e.g. the part-3 builders) and `tsvNamespaceExport`
  could narrow the same way to avoid unnecessary rebuilds on unrelated flake
  edits / `nixpkgs` bumps. Safe either way — coarse just means "rebuilds more
  often than necessary."

## Smaller items

- **Reusable union-read-namespace**: if the serving script's union assembly
  (merged pins + multi-source deep-fetch) turns out generally useful, promote
  it from the example into fxtr2 (`experimental/`).
- **CI rot-check for the publishing example** (mirrors the part5 followups
  note about lazily-built flake outputs rotting silently).
