# TSV Namespace Rename — Implementation Notes

Running log of judgement calls / surprises / non-mechanical decisions made while applying
the rename described in `tsv_namespace_rename_scout.md`. Things here may be worth revisiting
or confirming.

## Process
- Order: structural `git mv`s (serial) → mechanical edits (fan-out workflow, 4 disjoint
  groups) → manual prose pass → verify (`uv run pytest -n auto` in `v2/.venv`).

## Outcome
- Done. ~550 mechanical renames + ~75 prose locations across ~55 files. Verification:
  `v2/tests/test_tsv_namespace/` + `test_repo_storage.py` = **335 passed**; full `v2` suite green
  except 3 pre-existing/unrelated failures (`test_streams`, `test_typed_service`, `test_legacy_compat`
  — confirmed identical on a clean `git stash`ed checkout).

## Judgement calls / things worth revisiting

1. **Cross-group field seam (functional fix).** Core renamed `TsvNamespaceConfig.store_root` →
   `namespace_root` and the public `TsvFxtrNamespace.__init__` param `store_root` → `namespace_root`.

3. **User-visible CLI output strings changed** (intentional, per decision 8): `No fxtr store found`→
   `No tsv namespace found`; `Initialized fxtr store`→`Initialized tsv namespace`; status label
   `Store root:`→`Namespace root:`; init-exists →`A tsv namespace already exists`. `test_cli.py`
   assertions were updated to match. Lower-level `config.init_namespace` `FxtrConfigError` also reworded
   `Store already exists`→`Namespace already exists`. **Revisit if any downstream tooling greps CLI text.**

5. **`INDEX_FILE_NAME` constant name kept; value → `fxtr_namespace_indexes.tsv`** (hard cutover, no
   back-compat shim, no checkout migration, per decision 7). Same for the on-disk demo subdir / argparse
   flag / test-fixture ns-id values in `examples/store_indexing`.

6. **Doubled module path accepted:** `fxtr2.data.tsv_namespace.tsv_namespace` (mirrors
   `sqlite_store/sqlite_namespace.py`), per decision 1.

7. **Stale `LOCAL_CODE_OVERVIEW.md` TODO removed:** it claimed the `fxtr-tsv-ns` CLI was "not yet
   implemented" — it is (`fxtr2/cli/tsv_ns_cli.py`). Replaced with a pointer line.

8. **Intentionally left untouched:** `v2/scratch_notebooks/nb_01_22_gitstore.py` (throwaway, decision 5);
   `nix/fxtr_workdir_harness` `_jj_backing_git_store` (jj/git plumbing, unrelated); all historical plan
   docs under `v2/plans/**`; the `plans/phase2/stage1_git_namespace.md` path reference in
   `tsv_namespace.py:106` (that historical file keeps its old name).

9. **`__pycache__` gotcha:** the `git mv` of the package dir carried stale `.pyc` files, causing a
   transient `EOFError: marshal data too short` on first test run. Cleared all `__pycache__`/`*.pyc`.
