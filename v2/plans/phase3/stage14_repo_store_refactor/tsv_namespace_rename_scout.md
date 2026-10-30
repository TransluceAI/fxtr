# TSV Namespace Rename — Reconciled Scout Plan (stage 14 follow-on)

**Status:** Scout synthesis + decisions RESOLVED (2026-06-14). Ready as the source of truth
for the apply phase. All §5 open questions are decided; §1/§2 reflect those decisions.
**Concept rename:** the Python "git store" / "git namespace" -> "tsv namespace"
(a namespace DEFINED VIA TSV FILES, LINKED TO a repo entity store). The repo entity
store keeps the word "store".

This document reconciles 4 regional scout inventories:
- **Region A — fxtr2 core library** (`v2/fxtr2/**`)
- **Region B — cross-repo consumers** (`projects/**`, `nix/**`, `spikes/**`, `examples/**`)
- **Region C — tests / examples / notebooks** (`v2/tests/**`, `v2/examples/**`, `v2/scratch_notebooks/**`)
- **Region D — on-disk strings, packaging, docs**

---

## 1. Canonical identifier rename map

Confirmed mappings from CLAUDE.md are marked **(confirmed)**. Recommendations beyond the
confirmed list are marked **(recommended)** or **(flag)**. Conflicts are pulled out into §5.

| Old | New | Kind | Level | Confidence | Regions | Notes |
|-----|-----|------|-------|-----------|---------|-------|
| `GitNamespaceBackend` | `TsvNamespaceBackend` | identifier | namespace | high **(confirmed)** | A, B, C | Defined `git_namespace.py:74`. ~41 occ. core; consumer call sites in the hub client; ~80 call sites across 4 test files. |
| `GitFxtrNamespace` | `TsvFxtrNamespace` | identifier | namespace | high **(confirmed)** | A, B, C | Defined `fxtr_context_api.py:715`. 9 occ. in core (all in that file); 1 docstring-only ref in `protocols.py`; many example/notebook constructor sites. |
| `GitStoreConfig` | `TsvNamespaceConfig` | identifier | namespace | high **(confirmed)** | A, B, C | Defined `config.py:77` (frozen dataclass). 32 occ. core; type annotations in the hub client; tests in `test_config.py`/`test_sync.py`. Distinct from `RepoStoreConfig` (kept). |
| `git_store` (package dir) | `tsv_namespace` | module-path | namespace | high **(confirmed)** | A, B, C, D | See §3 for the dir move and §4 for import-site fan-out. |
| `git_namespace.py` (module file) | `tsv_namespace.py` | filename | namespace | high **(confirmed)** | A, C | Yields doubled path `fxtr2.data.tsv_namespace.tsv_namespace` — see §5 Q1. |
| `git_ns_cli.py` (CLI module) | `tsv_ns_cli.py` | filename | namespace | high **(confirmed)** | A, C, D | Referenced by console-script `v2/pyproject.toml:42` and imported by `test_cli.py:18`. |
| `test_git_store/` (test dir) | `test_tsv_namespace/` | filename | namespace | high **(confirmed)** | C | 15 files move. See §3. |
| Test classes `TestGitNamespaceBackend*`, `TestGitNamespaceStaleness`, `TestGitNamespaceIndexing*` | `TestTsvNamespaceBackend*`, `TestTsvNamespaceStaleness`, `TestTsvNamespace*` | identifier | namespace | high **(recommended)** | C | Named after the renamed classes; enumerated sites in the 3 test modules. |
| `test_git_namespace.py` (+ `_staleness`, `_indexing` siblings) | `test_tsv_namespace.py` (+ `_staleness`, `_indexing`) | filename | namespace | medium **(recommended)** | C | Mirror source rename. Low churn, self-contained. |
| `ensure_git_namespace_initialized` | `ensure_tsv_namespace_initialized` | identifier | namespace | high **(recommended)** | C | Example-local helper in `run_demo.py` (def :26, calls :83/88/93/98). |
| `use_git_store` / `git_store_dir` / `_init_git_store` | `use_tsv_namespace` / `tsv_namespace_dir` / `_init_tsv_namespace` | identifier | namespace | medium **(recommended)** | C | Example-internal in `store_indexing/{generate,query}_data.py`. Tied to `--use-git-store` flag + `git_store` subdir — see §2. |
| `git_ns` (local var) | `tsv_ns` | identifier | namespace | low **(recommended)** | C | Cosmetic, `generate_data.py:222`. |
| `fxtr-git-ns` (console script) | `fxtr-tsv-ns` | cli/packaging | namespace | high **(confirmed)** | D | `v2/pyproject.toml:42`; only `[project.scripts]` entry in the repo for this concept. Rename script name AND target -> `fxtr2.cli.tsv_ns_cli:main`. |

### KEEP — do NOT rename (store-level or unrelated; recorded so they are not swept up)

| Symbol | Level | Rationale |
|--------|-------|-----------|
| `RepoEntityStore`, `RepoStoreConfig`, `repo_storage.py`, `repo_storage_config.py` | store | CLAUDE.md explicit keepers. |
| `_index_ns` / `index_ns` param / "index namespace" | store | IS the repo entity store (`self._store: RepoEntityStore`). |
| `LEGACY_RESERVED_PREFIX` (`index`), `DEFAULT_RESERVED_PREFIX` (`root`) | n/a | Unrelated to store/namespace axis (`config.py:67-68`). |
| `_fxtr_store_impl` (`Entity.store()` hook) | n/a | Serialization hook, unrelated; `entity_ref_scope.py:168` + converters. |
| `worktree_namespace_id` / `store_namespace_id` (`TsvNamespaceConfig` fields) | namespace | Already say "namespace"; field names not in confirmed list — keep. |

### Previously-ambiguous identifiers — RESOLVED (see §5; all now confirmed renames)

These were flagged for sign-off; the code check confirmed they all operate on the
**namespace marker** (`fxtr_namespace.toml`), not the repo/store dir, so they are
namespace-level and get renamed. We are free to break these APIs (module renames already do).

| Old | New | Kind | Rationale |
|-----|-----|------|-----------|
| `store_root` (`TsvNamespaceConfig` field) | `namespace_root` | identifier | Docstring: "Path to the directory containing `fxtr_namespace.toml`" — the namespace marker dir, not the repo root (that's `.fxtr/`, via `find_fxtr_dir`). ~13+ sites. |
| `store_root_str` (`TsvNamespaceConfig` field) | `namespace_root_str` | identifier | Pre-resolved str of the above. |
| `init_store` (public, `config.py:298`) | `init_namespace` | identifier | Writes `fxtr_namespace.toml`; operates on the namespace marker. |
| `store_exists` (public, `config.py:364`) | `namespace_exists` | identifier | Checks `fxtr_namespace.toml` existence. |
| `store_root` param of `load_config` / `resolve_repo_config_path` / `_default_worktree_namespace_id` | `namespace_root` | identifier | Same dir; rename the param everywhere for consistency. |
| `_STORE_ROOT_GITIGNORE` (constant, `config.py:285`) | `_NAMESPACE_ROOT_GITIGNORE` | identifier | The .gitignore-content generator written by `init_store`; also reword its prose (decision 4). |
| `glease` (test helper, 4 copies) | `tsv_backend_lease` | identifier | The `g` abbreviated "git"; use the rename to make the name non-opaque. |

---

## 2. On-disk string decisions

| String | Level | Decision | Files / source |
|--------|-------|----------|----------------|
| `fxtr_store_indexes.tsv` | **namespace** | **RENAME -> `fxtr_namespace_indexes.tsv`** | `index_file.py` (value of `INDEX_FILE_NAME`, def :23), `index_sync.py`, core `git_namespace.py`, tests `test_index_sync.py`/`test_index_file.py`, `git_store/LOCAL_CODE_OVERVIEW.md`. Hard rename, in-repo only, no back-compat. |
| `INDEX_FILE_NAME` (constant) | namespace | KEEP constant NAME; change VALUE to `fxtr_namespace_indexes.tsv` | `index_file.py:23`. |
| `git_store` (subdir for demo data) | namespace | **RENAME -> `tsv_namespace`** | `store_indexing/{generate,query}_data.py`. This is a demo on-disk dir, NOT the package path. |
| `--use-git-store` / `use_git_store` (argparse flag) | namespace | **RENAME -> `--use-tsv-namespace` / `use_tsv_namespace`** | `store_indexing/{generate,query}_data.py`. |
| `git_store_worktree_test` / `git_store_index_test` (test fixture ns-id values) | namespace | **RENAME -> `tsv_ns_worktree_test` / `tsv_ns_index_test`** | `test_sync.py`. Cosmetic test data, not on-disk filenames. |
| `.gitignore` comment "Fxtr git store local files (auto-generated)" | namespace | **RENAME prose -> "Fxtr tsv namespace local files"** | 4 example `storage/*/.gitignore` files. Backend-generated; source generator changes, committed copies regenerate — see §5 Q4. |
| `fxtr_namespace.toml` | namespace | KEEP (already renamed this stage; `NAMESPACE_CONFIG_FILENAME` `config.py:63`) | core `config.py`/`git_namespace.py`/`key_utils.py`, `repo_storage_config.py`, `git_ns_cli.py`, consumer `stores.py`, examples + tests. |
| `fxtr_namespace.local.toml` | namespace | KEEP (`NAMESPACE_LOCAL_CONFIG_FILENAME` `config.py:64`) | core `config.py`, tests, example `.gitignore` files. |
| `.fxtr.tsv` suffix + `*.fxtr.tsv.dirty` / `.backup-{ts}.tsv` / `DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md` | namespace | KEEP (per-pin TSV/dirty sidecars; token is brand "fxtr", not "store") | `key_utils.py`, `sync.py`, `dirty.py`, `git_namespace.py`, `git_ns_cli.py`, example storage dirs. |
| `fxtr_store_repo_config.toml` | **store** | KEEP (configures the STORE; CLAUDE.md explicit) | `repo_storage_config.py`, core `config.py`, `repo_storage.py`, consumer `stores.py`, `test_config.py`. |
| `REPO_CONFIG_FILENAME` (constant) | store | KEEP (value = `fxtr_store_repo_config.toml`, `repo_storage_config.py:41`) | `repo_storage_config.py`, core `config.py`. |
| `repo_backing_storage.sqlite` | store | KEEP (`DEFAULT_SQLITE_STORE_FILENAME` `repo_storage_config.py:43`; no migration) | `repo_storage_config.py`, `repo_storage.py`. |
| `index:` / `INDEX_NAMESPACE_PREFIX` (`index:{uuid}`) | store | KEEP (`repo_storage_config.py:47`; CLAUDE.md NO migration) | `repo_storage_config.py`, `repo_storage.py`. |
| `index_namespace_id` (config KEY in repo config) | store | KEEP (CLAUDE.md: key remains) | `repo_storage_config.py`, core `config.py`, `test_config.py`. |
| `config.toml` (`USER_CONFIG_FILENAME`, `~/.fxtr/config.toml`) | store | KEEP (user-level config, orthogonal) | `repo_storage_config.py`. |
| `.fxtr` (`FXTR_DIR_NAME` `repo_storage_config.py:40`) | store | KEEP (repo-store/discovery dir) | `repo_storage_config.py`, core `config.py`. |

### "git_store" as an on-disk literal — CONFIRMED ABSENT

All three relevant regions (A, C, D) independently confirm: the literal token `git_store`
**does NOT appear in any on-disk filename, path literal, or `.toml`/`.tsv`/`.sqlite`
marker** in the `v2/fxtr2` source. It exists only as:
- the Python package path `fxtr2.data.git_store` (handled by the dir move, §3), and
- a **demo on-disk subdir name** in `store_indexing/{generate,query}_data.py` (renamed,
  see row above) — this is example data, not a core marker.

This matches the CLAUDE.md expectation. No prominent flag required for the core; the only
caveat is the example demo subdir, captured above.

---

## 3. File / module moves (recommended)

| From | To | Churn vs benefit |
|------|----|------------------|
| `v2/fxtr2/data/git_store/` | `v2/fxtr2/data/tsv_namespace/` | **Confirmed.** Medium churn: 11 in-core import sites (4 in `cli/git_ns_cli.py`, 1 relative in `fxtr_context_api.py`, 6 absolute in package `__init__.py`) + ~13 test import sites + 5 consumer files in the hub client. Intra-package relative imports (`.config`/`.git_namespace`/`.dirty`/`.sync`) ride along free. NO `v2/pyproject.toml` package-list change needed. **High benefit** — most visible "git store" artifact. |
| `v2/fxtr2/data/git_store/git_namespace.py` | `v2/fxtr2/data/tsv_namespace/tsv_namespace.py` | **Confirmed.** Low churn: only `__init__.py:39` imports it by module path in core; caplog logger string `test_git_namespace.py:1071` in tests. **CAUTION:** produces doubled path `tsv_namespace.tsv_namespace` (§5 Q1) — mirrors `sqlite_store/sqlite_namespace.py` precedent. |
| `v2/fxtr2/cli/git_ns_cli.py` | `v2/fxtr2/cli/tsv_ns_cli.py` | **Confirmed.** Low churn: console-script `v2/pyproject.toml:42` + `test_cli.py:18` import. Do together with the `fxtr-git-ns -> fxtr-tsv-ns` rename. |
| `v2/tests/test_git_store/` | `v2/tests/test_tsv_namespace/` | **Confirmed.** 15 files move; no other test dir imports it. Churn: the move + doc heading `tests/LOCAL_CODE_OVERVIEW.md:144` + comment `test_repo_storage.py:32`. |
| `v2/tests/test_git_store/test_git_namespace.py` (+ `_staleness`, `_indexing`) | `test_tsv_namespace/test_tsv_namespace.py` (+ `_staleness`, `_indexing`) | **Recommended.** Self-contained; only churn is the filenames. |
| `v2/scratch_notebooks/nb_01_22_gitstore.py` | `nb_01_22_tsvnamespace.py` | **OPTIONAL/low-priority.** Scratch/throwaway; also has hard-coded `gitstore_demo` / `demo_store` paths (lines 105-108). Discretionary — Region D also marks this file `historical-leave`; see §5 Q5. |

---

## 4. Per-file work-list (grouped for disjoint fan-out)

### Group 1 — CORE (`v2/fxtr2/**`) — Region A
| File | Approx occ. | What changes |
|------|------------|--------------|
| `data/git_store/git_namespace.py` -> `data/tsv_namespace/tsv_namespace.py` | many | Class def `GitNamespaceBackend`->`TsvNamespaceBackend`; `GitStoreConfig` type refs (:42/111/969); `fxtr_store_indexes.tsv` ref; relative imports ride along; prose "git namespace". |
| `data/git_store/config.py` -> `data/tsv_namespace/config.py` | 32-ish (incl. `GitStoreConfig`) | `GitStoreConfig`->`TsvNamespaceConfig` def + `__all__` :54; KEEP `init_store`/`store_exists`/`store_root` (flagged). |
| `data/git_store/__init__.py` -> `data/tsv_namespace/__init__.py` | ~10 | 6 absolute imports (:25/32/39/40/46/47) repoint package + module; `__all__` entries for renamed symbols (:51/66). |
| `data/git_store/sync.py` | 7+ | `GitStoreConfig` annotations (7 sigs); `store_root_str` (flag KEEP). |
| `data/git_store/index_file.py` | few | `GitStoreConfig` sig :26; `INDEX_FILE_NAME` value -> `fxtr_namespace_indexes.tsv`. |
| `data/git_store/index_sync.py` | few | `fxtr_store_indexes.tsv` literal/docstrings. |
| `data/git_store/dirty.py`, `key_utils.py`, `tsv_file.py` | doc/prose | KEEP submodule names; prose "git namespace". |
| `data/fxtr_context_api.py` | ~10 | `GitFxtrNamespace`->`TsvFxtrNamespace` def :715 + error msgs; `from .git_store import GitNamespaceBackend` :26 -> `.tsv_namespace`; backend refs :741/759/766/790. |
| `cli/git_ns_cli.py` -> `cli/tsv_ns_cli.py` | several | 4 package imports (:26/27/33/34); `GitNamespaceBackend` calls (:96/125/154/190/226/264); docstring "fxtr-git-ns" / "git-backed fxtr stores". |
| `data/repo_storage.py`, `data/repo_storage_config.py` | docstring/prose | Module-path mentions of `git_store` in docstrings (`repo_storage.py:13`, `repo_storage_config.py:10/12/14/15`); KEEP all store identifiers/literals. |

### Group 2 — CONSUMERS (`projects/**`) — Region B
| File | Approx occ. | What changes |
|------|------------|--------------|
| `fxtr_workstation/src/.../protocols.py` | 1 | Docstring-only `GitFxtrNamespace`->`TsvFxtrNamespace` (~:198). |

> Submodule segments `.config/.index_file/.key_utils/.tsv_file` are NOT renamed — only the
> `git_store` package segment becomes `tsv_namespace`.

### Group 3 — TESTS / EXAMPLES / NOTEBOOKS (`v2/tests/**`, `v2/examples/**`, `v2/scratch_notebooks/**`) — Region C
| File | Approx occ. | What changes |
|------|------------|--------------|
| `tests/test_git_store/test_git_namespace.py` (-> `test_tsv_namespace/test_tsv_namespace.py`) | many (~80 `.init/.open` across the 4) | `GitNamespaceBackend`, test classes, import root, caplog strings (:625/715/1069-1071). |
| `tests/test_git_store/test_git_namespace_staleness.py` (-> renamed) | several | `GitNamespaceBackend`, `TestGitNamespaceStaleness`. |
| `tests/test_git_store/test_git_namespace_indexing.py` (-> renamed) | several | `GitNamespaceBackend`, `TestGitNamespaceIndexing*`. |
| `tests/test_git_store/test_cli.py` | several | `GitNamespaceBackend`; `from fxtr2.cli.git_ns_cli import app` :18; docstring "fxtr-git-ns". |
| `tests/test_git_store/test_config.py` | several | `GitStoreConfig` :15/329; `fxtr_store_repo_config.toml`/`index_namespace_id` KEEP. |
| `tests/test_git_store/test_sync.py` | several | `GitStoreConfig` :11/28-30; fixture ns-id values `git_store_*_test` -> `tsv_ns_*_test`. |
| `tests/test_git_store/{test_dirty,test_dirty_staleness,test_index_sync,test_index_file,test_tsv_file,test_key_utils}.py`, `__init__.py` | varies | Import-root `fxtr2.data.git_store.*`; `fxtr_store_indexes.tsv` in index tests; caplog strings in `test_dirty.py:998/1042`. |
| `tests/test_repo_storage.py` | ~3 | Import `FxtrConfigError` from `fxtr2.data.git_store` :21; docstring prose :100-101; comment :32. |
| `examples/session_manager_porting/v2_version/run_demo.py` | several | `GitFxtrNamespace` (:14/33/107-109/245); `ensure_git_namespace_initialized` helper; comments. |
| `examples/store_indexing/generate_data.py`, `query_data.py` | several | `GitFxtrNamespace`; `use_git_store`/`git_store_dir`/`_init_git_store`; `--use-git-store` flag; `git_store` demo subdir; `git_ns` var. |
| `examples/.../storage/*/.gitignore` (4) | prose | "Fxtr git store local files" comment regenerate. |
| `scratch_notebooks/nb_01_22_gitstore.py`, `nb_02_06_demo.py` | several | `GitFxtrNamespace` access; optional file/path renames (§5 Q5). |

### Group 4 — PACKAGING / DOCS — Region D
| File | Status | What changes |
|------|--------|--------------|
| `v2/pyproject.toml` | edit | `:42` `fxtr-git-ns = "fxtr2.cli.git_ns_cli:main"` -> `fxtr-tsv-ns = "fxtr2.cli.tsv_ns_cli:main"`. |
| `portable/pyproject.toml` | edit (stale comment) | `:66` code comment "in git_store/sqlite_store ..." goes stale after dir rename. |
| `v2/README.md` | live-update | doc step. |
| `v2/CODE_OVERVIEW.md` | live-update | doc step. |
| `v2/fxtr2/data/git_store/LOCAL_CODE_OVERVIEW.md` (moves with dir) | live-update | doc step. |
| `v2/fxtr2/data/LOCAL_CODE_OVERVIEW.md` | live-update | doc step. |
| `v2/fxtr2/data/remote/LOCAL_CODE_OVERVIEW.md` | live-update | doc step. |
| `v2/fxtr2/data/store_indexing/LOCAL_CODE_OVERVIEW.md` | live-update | doc step. |
| `v2/fxtr2/experimental/LOCAL_CODE_OVERVIEW.md` | live-update | `GitFxtrNamespace` ref :74. |
| `v2/tests/LOCAL_CODE_OVERVIEW.md` | live-update | heading `### test_git_store/` :144. |

### LEAVE — historical plan docs (DO NOT edit)
- `v2/plans/phase3/stage14_repo_store_refactor/overview.md`, `plan.md`
- `v2/plans/phase2/stage1_git_namespace.md`
- `v2/plans/phase3/stage1/stage1post1_git_ns_index.md`
- `v2/plans/track2/stage1_http_api.md`
- All `projects/fxtr_workstation/plans/phase0/**` (11 files, incl. `design/01_git_store.md`, `part2_namespace_export_and_preload.md`, etc.)

### Confirmed NOT in scope (false positives, excluded)
- `nix/fxtr_workdir_harness/.../__init__.py` — `_jj_backing_git_store` / `git_target` are jj/git plumbing, NOT the fxtr family.
- `spikes/thingwatcher/**` — its `FxtrNamespace`/`namespace_view` UI concept is unrelated; zero git_store-family refs.

---

## 5. Open questions / conflicts

### Conflicts to resolve
**None.** All three regions that touch `GitNamespaceBackend` / `GitFxtrNamespace` /
`GitStoreConfig` / `fxtr2.data.git_store` agree on the same target names. The only
divergence is in *what to do with non-confirmed symbols*, captured as open questions below.

### Open questions — RESOLVED (decisions, 2026-06-14)
1. **Doubled module path** `fxtr2.data.tsv_namespace.tsv_namespace` — **ACCEPT** (mirrors
   `sqlite_store/sqlite_namespace.py`). No `backend.py` alternative.
2. **`store_root` / `store_root_str`** — **RENAME -> `namespace_root` / `namespace_root_str`.**
   Code-confirmed namespace-level (the `fxtr_namespace.toml` dir). See §1 resolved table.
3. **`init_store` / `store_exists`** — **RENAME -> `init_namespace` / `namespace_exists`.**
   Operate on the namespace marker; breaking the API is fine (module renames already do).
4. **Example `.gitignore` comment** — **FIX THE GENERATOR** (`_STORE_ROOT_GITIGNORE` ->
   `_NAMESPACE_ROOT_GITIGNORE` at `config.py:285`, reword its prose). Committed demo copies
   regenerate; do not hand-edit them as source.
5. **Scratch notebook `nb_01_22_gitstore.py`** — **LEAVE** (throwaway scratch).
6. **`glease()` test helper** — **RENAME -> `tsv_backend_lease`** (make the opaque name clear).
7. **`fxtr_store_indexes.tsv` -> `fxtr_namespace_indexes.tsv`** — **HARD CUTOVER.** Rename the
   constant value + all string literals; NO read-old/write-new shim, NO checkout migration.
   Assume no existing users.
8. **Doc-live prose** — **REWORD IN THIS PASS, BUT MANUALLY** (not by blind token swap), to
   ensure docs read correctly. Guiding principle:
   - If the text refers to *the namespace / the TSV pin files*, say **"tsv namespace"**.
   - If it refers to *the thing being backed by git*, be explicit about **what lives where**:
     a repo-level entity store, with pin names / TSV files committed to git. Describe the
     actual mechanism rather than calling the whole thing "git-backed".

### No-action notes (recorded so they are not swept up)
- `DEFAULT_RESERVED_PREFIX` (`root`) vs `LEGACY_RESERVED_PREFIX` (`index`) — unrelated.
- `worktree_namespace_id` / `store_namespace_id` config fields already say "namespace" — keep.
- No cross-repo consumer `pyproject.toml` references `fxtr-git-ns`; the script rename has no
  external sweep beyond `v2/pyproject.toml`.
- `protocols.py` (fxtr_workstation): only a docstring ref; the confirmed mapping makes it
  `TsvFxtrNamespace` (the user-facing wrapper), not the backend.
