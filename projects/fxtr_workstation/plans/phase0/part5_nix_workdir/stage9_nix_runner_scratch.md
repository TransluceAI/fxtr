# Stage 9 scratch — decision log (2026-06-04 chat)

Raw decision trail behind `stage9_nix_runner.md`. Not a spec; the plan
wins where they disagree.

## Feasibility argument

The whole design rests on: fxtr uses nix to **build files**, never to
execute built artifacts on the host. Host-side consumption inventory:

- wasm `sitePackages` → flat pure-python dir, mounted RO into eryx
  (host process) — host needs to *read* it.
- entity layers / preset `typeEntity` layers → layered-store dirs the
  MCP/dispatcher open readers over — host reads.
- renderer `.mjs` → bytes the dispatcher wraps — host reads.
- nix2container image JSON + layers → consumed by skopeo, which can
  run inside the builder container; host never touches them.
- skopeo itself was the one host-executed nix artifact → moves inside.

So "nix in a container + materialize outputs" covers everything, and an
x86_64-linux builder container makes canonicalSystem builds *native*
(no remote builders) — arguably the biggest win of the stage.

## Store location: host mount vs volume + materialize

Daniel's initial sketch was "host path mounted in as the fxtr-facing
nix store." Rejected in favor of a named volume + materialize because:

- nix builds are metadata-heavy; `/nix` over virtiofs is slow and
  nix's sqlite db on network-ish filesystems is a known sore spot.
- The actual *requirement* was host visibility of consumed outputs,
  which materialization satisfies with one copy per cold build.
- Cache clearing is equally easy both ways (volume rm + scratch wipe).

## Layer closures + the remap hook

Layer dirs are NOT self-contained: `parents.json` holds **absolute
paths** to parent layer dirs
(`v2/fxtr2/data/layered_file_store/layer_format.py` — `read_parents`
returns absolute paths, caller canonicalizes). Options:

1. Rewrite `parents.json` during copy-out — rejected (Daniel): the
   copy-out step would have to reimplement/duplicate the v2 layer
   format parsing; format knowledge would live in two places.
2. **Remap hook in the v2 reader** — chosen. Small, scoped to parent
   resolution only (pins are FxtrIDs, no other paths in the format),
   keeps the closure copy verbatim.

`nix path-info -r` gives the closure; only layer-ish outputs need
`closure=True` (sitePackages / .mjs are self-contained; images never
materialize).

## Mounts vs direct git sync

Considered syncing repos into the builder more directly (git bundle or
push over a `podman exec` ext-transport into an in-container bare
repo). Works for the frozen CODE path (spawner/dispatcher build pinned
commits) but **fails the MCP DATA path**: `retrieve_nix_entity` is
deliberately non-frozen against the dirty working tree (uncommitted
tracked edits count). Faking that with synthetic commits
(`git write-tree` against a temp index) changes semantics subtly — not
worth owning. Hence: mount the working tree RO; and since containers
can't gain mounts after start, **one builder container per repo**
(Daniel's suggestion), all sharing the `/nix` volume. Container keyed
on git repo root (matches nix's bare-path auto-git walk-up) or the
workdir for `path:` fallback.

## Delivery from inside the container

`containers-storage:` direct-write = bind-mounting podman's own
graphroot+runroot into the builder and trusting containers/storage
multi-process locking + version skew — feasible in principle, rejected
as invasive (see `stage8_podman_scratch.md`). Chosen: same
socket-streaming shape as stage 8, just exec'd inside the builder with
the socket mounted. Bonus: `build_skopeo`'s host-system
canonicalSystem exception disappears on this backend (skopeo is
x86_64-linux in-store).

## Backend switch + defaults

- `FXTR_NIX_BACKEND=host|podman`. Host backend preserved indefinitely
  (Daniel: keep host-nix + Apple Container combo working even while
  recommending podman).
- Implement with default `host` (zero-change regression safety), flip
  to `podman` as the explicit final chunk once E2E is green — flipping
  is one line; doing it last means it's done from knowledge.
- Podman-nix backend hard-requires podman as the service-container
  runtime (delivery lands in podman's store); host backend keeps
  stage 8's runtime flexibility.

## Shared package shape

`fxtr_nix_runner` is deliberately a **backend** (process transport +
path translation + lifecycle), not a policy lib: the wrappers keep
their own ref construction and error conventions, and the
frozen/non-frozen variation stays in the wrappers where it belongs.
The backend is the layer beneath all three where the
host-vs-container switch has to live.

## Known accepted costs

- ~~`sandbox = false` inside the builder~~ **SUPERSEDED by gate spike
  (g), 2026-06-06:** the builder runs `sandbox = true` at the moderate
  rung (`SYS_ADMIN + label=disable + unmask=/proc/*`,
  `sandbox-fallback=false`). See the plan's Security posture + Builder +
  deliverer containers.
- Rosetta-speed builds (quantify in spike; expected acceptable).
- One copy per cold materialize.
- `/nix` volume initialized by image copy-up → image-pin bumps don't
  refresh an existing volume; cache clear re-inits.
- ~~Per-container eval caches (not shared)~~ **SUPERSEDED:** shared via a
  second named volume (`fxtr-nix-cache`) mounted as `XDG_CACHE_HOME`
  (see plan).
