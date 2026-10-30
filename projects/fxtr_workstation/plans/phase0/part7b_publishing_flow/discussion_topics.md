# Part 7b — discussion topics / open questions

Numbers are referenced from `design/06_proposed_flow.md`. Resolved items keep
their numbers (marked **DECIDED**, with the decision recorded); open items
are re-ranked below.

## Decided (fourth sync, 2026-06-13) — `fxtr_nix_runner` becomes the flake manager

The big reshape: **`fxtr_nix_runner` is promoted from a pure nix transport
to the fxtr2-aware "manager" of the whole fxtr nix flake system.** It gains a
fxtr2 dependency (no cycle — fxtr2 doesn't import the runner, and all current
consumers already depend on fxtr2). Consequences, all DECIDED this sync:

**RUNNER-1. Two tiers in one package.** Keep the existing pure-transport tier
(`Backend`/`Handle`, anyio-only, fxtr2-free *internally*) and layer a new
fxtr2-aware **manager** tier on top within `fxtr_nix_runner`. The `Handle`
gains only generic hooks (expose-host-endpoint + extra-build-env); all
repo-store/fxtr2 knowledge lives in the manager. Consumers obtain their
backend/readers **through a manager factory** so remote wiring is automatic.
(We'll take a stab and revisit if collapsing the tiers turns out simpler.)

**RUNNER-2. Repo entity store, decoupled (PREREQUISITE — its own plan; now
LANDED).** The "index namespace" was pulled out of the namespace backend into
a standalone **repo entity store** (`fxtr2.data.repo_storage.RepoEntityStore`):
by-id read/write, no pins, a `DeepFetchable`. `GitNamespaceBackend` became
`TsvNamespaceBackend` (`fxtr2.data.tsv_namespace`), a *view* = repo entity
store + worktree namespace + a committed TSV-root marker, renamed
`fxtr_store.toml` → `fxtr_namespace.toml` ("store" now means the repo entity
store; the namespace is the pinned view). The internal
`.fxtr/fxtr_store_repo_config.toml` kept its name (and the on-disk
`index_namespace_id` key). Tracked in
`v2/plans/phase3/stage14_repo_store_refactor/overview.md`; **shipped** — part 1
builds on the real API (`RepoEntityStore.open`, `tsv_ns.store`,
`store.get_lease()`; CLI `fxtr-tsv-ns`).

**RUNNER-3. The runner is a second (read-only) consumer of the repo store.**
Given a repo, the manager **discovers** the store (walk up to `.fxtr/`),
**creates an empty one if absent** (matching git-namespace create
semantics), and opens it read-only. It is never handed the store by callers —
they shouldn't have to care. The runner never writes to the store (write-back
would force a stop-and-replan, per DECIDED-7). **Exposure, not mutation:** the
manager *exposes* the opened store as a `DeepFetchable` (+ a clonable lease,
since it owns the open session) so consumers mount it on their **own** scope;
the manager never reaches into a caller's scope to register sources for it.

**RUNNER-4. Rehost is unconditional.** Every managed build stands up the
rehost over the repo store and delivers the `FXTR_REMOTE_*` env to FODs —
not gated on "does this build declare preloads", because builds don't declare
a remote env explicitly and an entity may point at a preload FOD or a
postprocessed entity that transitively needs the store, at build time and/or
load time. Always-rehost is the only way to get clean semantics. (Accepts a
small overhead on the MCP retrieve path.)

**RUNNER-5. Two read adapters (manager-side).** (a) an **entities** read
namespace whose pins are the `fxtr.entities.<attr>` names — `retrieve(name)`
= eval metadata → build `.layer` → materialize → open reader (with the repo
store as `remote=` so remote pointers resolve transparently) → copy/attach
into the caller scope; (b) a per-attr **namespace** read view opened via
`fxtr.namespaces.<name>`, iterating that namespace's own pins, data resolved
through the repo store. Both target the `FxtrReadNamespace` shape.

**RUNNER-6. `rev` vs live workdir.** The manager reads a flake either at a
committed rev or as the live working dir; in both cases blobs come from
whatever the local repo store currently holds (TSVs/attrset pin the
*identity* at the rev; the store/remote supplies the *bytes*).

**RUNNER-7. `update-hashes` is fully runner-owned, no external
orchestration.** `fxtr_nix update-hashes [path]` opens the repo store, stands
up its own rehost, drives the builds, parses mismatches, rewrites the lock —
runnable directly as a user. The "publisher" (example side) only serves
values out; the runner orchestrates all building/setup.

**RUNNER-8. Migration scope.** Port the **MCP** `retrieve_nix_entity` onto
the new entities adapter as an **optional final chunk** (skippable if hairy).
Leave the **dispatcher** `set_preset` as a noted **follow-up** (it carries
extra policy — batched preset table, `(commit, drvPath)` caching, eager
id==key validation — not worth moving wholesale yet).

**OPEN-8 → DECIDED.** Reusable code home = **`fxtr_nix_runner`** (manager
tier): rehost server, repo-store discovery, eval/build/materialize
orchestration, the read adapters, the relock engine. The serving
(`create_fxtr_app` + `serving_context`) + the serve CLI stay **example-side**
(no serve script in the runner for now — revisit once we've ironed out
exactly what it should serve).

## Decided (first design sync, 2026-06-12)

**OPEN-3 → DECIDED. Build-time remote access = FODs with pinned hashes.**
Purity of the final flake is a hard constraint. v0 uses `lib.fakeHash` +
manual re-pinning; the FOD-lock-file + auto-relock mechanism is a tracked
followup (`followups.md`). Design consequence adopted in `design/06`: confine
*all* network to one fetch FOD (layer 0); layers 1–2 are pure and open their
readers/writers with no remote so undeclared fetches fail structurally.
Podman/host rehost reachability is scoped separately in
`design/07_build_rehost_networking.md` (needs verification spikes; extend
`fxtr_nix_runner` with a backend-neutral "expose host endpoint" hook).

**OPEN-4 + OPEN-6 → DECIDED. Defer v2/ts entirely; use `fxtr_report_builder`.**
The MDX step is the report-builder Python API (provenance projection already
handled Python-side, around its bundled TS subprocess) running inside a pure
derivation. No TS layer reader/writer in part 7b — if a step needs preloaded
data, Python preloads it into a layer first. Long-term: redo v2/ts
(provenance + services + token scopes), rebuild the MDX path on it, retire
`fxtr_report_builder` (`followups.md`).

**OPEN-9 → DECIDED. The remote store = the publishing repo's repo entity
store**, exposed by the serving script; a local stand-in for an arbitrary
blob store. Eventual target: the whole flow (serving + builds) hosted on the
behaviors hub. Layers stay remote-agnostic (consumer supplies the
`DeepFetchable`), so the swap is a serving-side change.

**OPEN-1 → DECIDED. The pins subdir is a real tsv namespace.** If the demo
needs the repo entity store's sqlite somewhere custom, prefer using the
existing overrides (`sqlite_store_path` in `.fxtr/fxtr_store_repo_config.toml`;
`repo_config_path` in `fxtr_namespace.local.toml` — see
`v2/fxtr2/data/repo_storage_config.py`) and only tweak namespace/store code if
those don't suffice.

**OPEN-5 → DECIDED (for now).** Published reports reuse
`RootReport`/`SubReport` so `@fxtr-ui/reports` renders them; subject to
change later.

**OPEN-2 → DECIDED (by OPEN-3).** Precomp entries for pinned ids come from
the remote round-trip inside the layer-0 fetch FOD (`mark_as_remote`
semantics). No committed precomp sidecar needed.

**OPEN-13 → effectively resolved.** The "sqlite store that just tracks
blobs" is the existing repo entity store backing sqlite (`RepoEntityStore`);
no new store kind.

**OPEN-7 → DECIDED (second sync).** No write-back: the serving script keeps
crawled layer entities scope-only (in-memory knowledge of what's loadable
from the layered store / a lazy adapter). Soft preference — revisit if the
non-writeback version turns out harder than expected. (The write-back
alternative would have given `/data/<id>` serving "for free" via the
existing repo-store `DeepFetchable`.)

**OPEN-10 → DECIDED (second sync), reframed.** There is no pin merging and
no collision policy: **the flake's `fxtr` attrset is the source of truth**
for what's exposed. Nix ops convert tsv-namespace TSVs into layered-store form
and *reexport* them, in two flavors: (a) a single tsv-namespace pin exported as
(i.e. *being*) a normal `fxtr_layer_entity`; (b) a new
`fxtr.namespaces.<name>` attr naming a whole TSV namespace — nix points
at the whole store, and the server decides how to expose it (~a prefix, but
held as a separate named thing). Mirrors how the MDX output is exposed as
entities via nix. Design folded into `design/06` ("the flake schema").

**OPEN-12 → DECIDED (second sync).** Pure pnpm-in-nix build of the
report-builder CLI, living in **`lib.fxtr`** (exposed by the fxtr flake, so
it's pinned there, built once, shared by all consumers). No vendored
artifacts. Route: nixpkgs `pnpm.fetchDeps` (lockfile FOD) + esbuild build
phase; plus a dist-path override hook in
`fxtr_report_builder/_ts_dist_path.py` (currently a fixed relative path).
**Refined (sixth sync, 2026-06-19):** `reportBuilderCli` is wrapped
(`makeWrapper`) as a self-contained `node` executable; it's delivered via the
part-3 builder's **generic `env` arg** (the demo sets `FXTR_REPORT_BUILDER_CLI`
to its store path), not a system-injected special case — nix has no
report-builder-specific code, and the CLI is *not* put on PATH (internal
helper). `_ts_dist_path` resolution order: arg → env → dev fallback (no PATH).
See `part4_report_builder_nix.md`.

**OPEN-15 → DECIDED (third sync), strengthened.** Separate — and better:
the **namespace export needs no FOD at all**. No nix-sandboxed builder ever
reads it, so it's a *pure* derivation that copies/preprocesses the committed
TSVs into a publication format (which need not be the layer format); the
server reads it and resolves data via the repo entity store, computing
precomp at serve time. FODs exist only where entity bytes must enter a
layered store for **in-build** reads. (Reshaped 2026-06-14: "a FOD that reads
`FXTR_REMOTE_*`" is the general shape — see `part2_namespace_export_and_preload.md`.
Part 2 ships one such helper, `entityFromTsvPin` (the single-pin
`fxtr.entities.<name>`-from-a-TSV-pin case); arbitrary in-build fetching is a
user part-3 builder FOD. The earlier generic `preload_from_remote`
writer is dropped.) Namespace exports therefore never re-pin and are always in
sync with the TSVs.

## Still open — medium priority

**OPEN-16. Namespace export publication format + attr shape.** What the pure
export derivation emits (verbatim TSV copy + small JSON header? normalized
JSON manifest?) and the `fxtr.namespaces.<name>` attr shape (`{ type;
export = <drv>; }`?). Also worth deciding: does the export stage even need a
derivation, or could `nix eval` just point the server at the TSV paths in
the flake source? (A derivation keeps the "buildable leaf" invariant and
gives a hermetic artifact when serving from a rev; leaning derivation.)
Plan-time.
_Daniel note:_ Also leaning derivation. Can aim for simplicity of the overall
system.

**OPEN-7b (residual).** Exposing "all entities in the repo entity store by
id": `/data/<id>` deep-fetch covers retrieval; is any *enumeration* needed
(`/pins` listing every id would be huge)? Default: no enumeration in v0.

## Still open — can decide late

**OPEN-8 → DECIDED (fourth sync).** Reusable code lands in
`fxtr_nix_runner` (the manager tier) — see the fourth-sync block at the top.
The old "expose the repo's store as a `DeepFetchable`" need is subsumed by the
repo entity store (RUNNER-2), which *is* a `DeepFetchable` (`RepoEntityStore`).
Serving + serve CLI stay example-side.

**OPEN-11. Example naming + scaffolding.** `examples/fxtr_publishing_demo/`?
Scaffolded out-of-repo (`prepare_workdir_repo` / `scaffold_workdir.py` style)
vs. runnable in place; flake refs by committed rev (the published story) vs.
bare path during iteration — likely path-during-dev, rev-for-publish, demo
shows the rev form at least once.
_Daniel note:_ `examples/fxtr_publishing_demo/` is good. Lean toward scaffolded out-of-repo to avoid pinning the repo flake inside itself with a relative path (difficult to get working). Dev can happen here but then it can be (re)exported
to run things.

**OPEN-14. Testing strategy.** Cheap pure-python seams (TSV→layer writer
against a local fake remote; union assembly; rehost server) vs. nix-marked
E2E (`py_testing` conventions, `nix_runtime` markers, host+podman sweep).
The TS round-trip concern dropped out with OPEN-6; the new cross-boundary
test is "nix-built layer (incl. report layer) reads back in Python with ids
matching".
