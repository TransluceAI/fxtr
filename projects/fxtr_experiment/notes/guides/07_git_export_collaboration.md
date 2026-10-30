# Collaborating via git: export and import

A workflow's entire at-rest state can be projected into a directory of
small, deterministic text files whose id columns reference an entity
store — and loaded back. Put that directory in a git repo and you get
version control, sharing, and forking of *experiment state* using
ordinary git operations. This guide covers the flow and the intended
project shape. The canonical spec is `notes/at_rest_format.md`.

**Caveat up front**: this flow is correct and round-trip-tested, but it
is **not yet optimized for performance** — export rewrites the whole
tree, import is wholesale-destructive, and the entity store is shared
by reference rather than packed/pruned. Fine for real experiments of
moderate size; don't expect incremental sync yet.

## The model: (git repo + entity store)

A workflow at rest is a pair:

- **A directory of TOML/TSV files** mirroring the grid tree — one
  directory per grid (`grid.toml` + `cells.tsv` / `generations.tsv` /
  `sourcing.tsv` / `targets.tsv`), nested like the namespace. This is
  the git-tracked, human-diffable half: every fact is by-reference
  (FxtrID strings), files are byte-deterministic for the same state
  (re-export of unchanged state is a no-op diff), and diffs are
  row-level — a status flip is a one-word diff, a new sweep cell is one
  new row.
- **The entity store** holding every referenced entity's closure. The
  export never copies entity bytes; ids in the files reference the
  session's own store, and the liveness invariant guarantees every
  recorded id's closure is already pinned there.

**Git commits are the version roots.** There is deliberately no
snapshot entity: a commit of the export directory identifies a state;
history, branches, merges (with care), and blame all come from git.

## Exporting and importing

```python
report = await wf.export_state("/path/to/project/workflow_state")
# → refuses if anything is running; writes the whole tree deterministically
```

Then commit with git as usual. Import is the mirror:

```python
report = await wf.import_state("/path/to/project/workflow_state")
# → refuses on a non-empty state db unless force=True (export first;
#   git is the backup), verifies everything before writing, then
#   replaces the state in one transaction
```

The operational rules:

- **At rest only**: both operations refuse while any generation is
  running. Any at-rest state exports — errored, interrupted, stale,
  dangling intent included; you are versioning reality, not only clean
  states.
- **The export directory must be export-owned**: keep READMEs and other
  files *outside* it (make it a subdirectory of the sharing repo).
  Export proves ownership before touching anything, so a misdirected
  path is a safe error.
- **Import is destructive and verified-first**: parsing and
  verification (including "every referenced id resolves in the store")
  happen entirely before anything is written; then the fact and intent
  tables are replaced wholesale. All derived state (currency/grounding
  stamps) re-derives on demand after import — a fresh pipeline imports
  and re-derives *fresh with no re-runs*.
- **After import**: re-register your ephemeral bindings (they are
  runner config, never exported — exactly like after a session reopen);
  sandboxed bindings arrive with the import as the bindings file and
  need only `set_sandbox_repo` pointed at a repo containing the pinned
  commits. Then `wf.lint()` is the what-did-I-just-load check.

**NEEDS CONFIRMATION / TODO**: how does the *entity store* travel in
the current tooling? The format note says the id columns reference "the
session's own store", so a collaborator cloning the git repo also needs
the entity closures. Options I can see: (a) ship the
`entities.sqlite` alongside (not git-friendly), (b) a repo entity
store / TSV namespace inside the repo.
The guide should state the blessed answer and
the concrete commands; right now I don't know which is implemented as
the recommended path.

## The two-repo project shape

The intended shape for a real project is **two repos**:

1. **The code repo** — user-managed: a nix flake, your types package,
   task-fn/service code, renderers, and driver/setup scripts. This is
   ordinary software, developed with ordinary tools. Sandboxed service
   definitions in workflow bindings pin *commits of this repo*
   (`git_commit` + `flake_attr`).
2. **The state repo** — the export of the workflow: the at-rest
   directory tree, plus the sandboxed-bindings TOML file (a lockfile
   pinning which commits of repo 1 serve which handle keys).

The two repos together represent the project: repo 1 says *how things
are computed and rendered*, repo 2 says *what the experiment's state
is* — with the bindings file as the cross-reference that pins repo-2
state to repo-1 commits. Cloned together, a collaborator can:

- browse the state repo's history (every export commit is a legible
  snapshot: what was targeted, what completed, what changed);
- import the state into their own runner, point `set_sandbox_repo` at
  their clone of repo 1, re-bind any ephemeral services, and **resume
  the experiment** — re-demand targets, widen domains, fork the
  analysis;
- verify: lint for structural health, re-run cells and let the
  record-check + safety pass confirm reproductions.

This is also the intended publication shape for the Behaviors Hub —
upload the pair, and the hub can rebuild the views, types, services,
and reports from them.

**NEEDS CONFIRMATION**: is two-repos the recommendation, or is the
blessed layout a single repo with `code/` and `state/` subdirectories
(the sandboxed-bindings note says "two repos ... cloned together";
`examples/fxtr_publishing_demo` may already embody the answer)?

## Collaboration patterns and caveats

- **Serialize writers.** The state repo represents one workflow's
  state; two people running divergent runners and both exporting will
  produce git conflicts in TSV rows that are mergeable only with
  understanding (ids are opaque). v0 guidance: treat the workflow like
  a database with one writer at a time — hand off via export/commit/
  pull/import — or partition ownership by subtree and merge with care.
  **NEEDS CONFIRMATION**: is subtree-partitioned merging actually
  sound (e.g. observation records referencing across subtrees), or
  should the guide say "never merge, always fast-forward"?
- **History is your undo.** Import-from-an-earlier-commit is the
  rollback affordance; the events log deliberately survives imports as
  local history.
- **What does not travel**: ephemeral bindings, the sandbox repo path,
  derived verdict stamps, run logs, and internal bookkeeping ids — all
  by design (they are machine-local or derivable).
- **Determinism of the files** means review is real: a PR to the state
  repo that claims "added two judges and re-ran the analysis" shows
  exactly the new config rows, the new generation rows, and the flipped
  target statuses — nothing else.
