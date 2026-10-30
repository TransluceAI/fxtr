# Implementation uncertainties — run-graph cache cold build (RunGraphCacheBuilder)

Design notes: `notes/2026_06_15_durable_cache_pt2.md`. This file lists only decisions/guesses that
may need revisiting.

## Provenance not validated during the build
The cold build (`RunGraphCacheBuilder`) does **not** load `run.provenance`. It is validation-only,
is never stored in the cache record, and runtime (`add_completed_run` / `fulfill_reservation`)
re-validates it anyway. So the build trusts the on-disk runset's consistency. If the build is ever
required to *detect* an inconsistent stored runset, this would need an opt-in strict pass.

## Unbuildable nodes: abort by default; cascade-skip opt-in
A runset entry whose entities won't load aborts the build (`RuntimeError`) by default. This means
the **in-place startup path raises out of `_load_runset`** → `SessionManager.create` fails. That is
intentional (corruption shouldn't silently serve a partial graph) but is a behavior change from the
old eager build, which silently skipped such nodes (and thereby caused an eternal-rebuild loop).
`skip_unbuildable=True` (offline `--skip-unbuildable` only) drops the node + its transitive
dependent closure. If we want startup to *degrade* rather than fail on a single corrupt entity, the
deferred alternative is a tombstone `_rg/` row (see notes).

## In-place assumes a single writer
`_cold_build` builds a fresh `dest` and `os.replace`s it over the active store. If another process
appends runs to the active store *during* the build, the swap would drop them. We assume single
writer for in-place (confirmed acceptable). Offline mode reads the source read-only and never swaps.

## Monotonic source assumption
The builder handles only *additions* to the source between runs — no removals or re-pointed runs.
So skip-present uses cid **presence** only (no `run_cid` recheck) and there is no deletion-diff
pass. Removals/re-runs during conversion would need a separate cid-diff delete step.

## `run` object written twice (staging then dest)
Staging holds the full record incl. the (thin) `run`, so stage 2 needs no entity-store reload and
its serial DAG-walk stays purely local. Cost: the run is serialized to staging and again to dest.
Accepted as cheap/local; the alternative (store only `run_cid`, reload in stage 2) would re-serialize
the serial walk's loads.

## Stage-2 in-memory `built` set
Stage 2 holds a set of all built task cids (~O(nodes), ~0.5 GB at 4M). Bounded and far below the old
build's footprint, but the one structure that grows with the runset. Could be swapped for a
dest-presence check if it ever matters.

## Process mode (opt-in) — IPC of run objects
`use_processes=True` (offline `--stage1-processes N`) returns each node's record (incl. the run)
across the process boundary. Verified working (locator + CID + record all pickle). At very large N
the IPC of ~4M run objects may cap the speedup; the deferred fix is per-worker staging shards merged
at the end. Threads remain the default. NOTE: process mode requires the caller to be under a
`if __name__ == "__main__"` guard (the migration script is); naive top-level invocation breaks under
spawn.

## `_load_runset(use_threading=...)` parameter
Now ignored — parallelism is controlled by the builder (threads by default). Left in the signature
for call compatibility; could be removed.
