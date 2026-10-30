# Stage 4 post-1: Fix TransitionalUpgradedEntity abstract method bug + enable pyright in pre-commit

## Background

The stage 4 entity registry refactor introduced `SelfLoadingLegacyEntity` as a compatibility bridge
for old-style entity types. `TransitionalUpgradedEntity` was made to inherit from it, but the
transitional rundata classes (`TaskWithPredecessors`, `ImplProvenanceSet`, `RunWithPredecessors`)
actually use `DataclassEntity` for serialization. The unimplemented abstract methods from
`SelfLoadingLegacyEntity` (`_to_fxtr_cbor`, `_from_fxtr_cbor`, `_fxtr_type_id`) make these classes
uninstantiable at runtime due to Python's ABC enforcement.

Additionally, `DataclassEntity._fxtr_store_impl` has an unnecessarily narrow return type
(`EntityRef[DataclassEntity]` instead of `EntityRef[Self]`), causing pyright conflicts when classes
inherit from both `DataclassEntity` and `SelfLoadingLegacyEntity`.

See `stage4post1_transitional_bugfix_scratch.md` for detailed analysis.

## Implementation steps

### 1. Fix `DataclassEntity._fxtr_store_impl` return type

**File:** `fxtr2/conversion/converters/mashumaro_entities.py`

- [x] Change `_fxtr_store_impl(self) -> EntityRef[DataclassEntity]` to
  `_fxtr_store_impl(self) -> EntityRef[Self]`
- [x] Add `Self` to the typing imports (already imported)

This is more accurate — the implementation uses `_infer_default_type_params(type(self))` which
returns a type based on the actual class, not `DataclassEntity`. Fixes 3 pyright
`reportIncompatibleMethodOverride` errors.

### 2. Remove `SelfLoadingLegacyEntity` from `TransitionalUpgradedEntity`

**File:** `fxtr2/legacy_compat/data.py`

- [x] Change `class TransitionalUpgradedEntity(SelfLoadingLegacyEntity, Entity)` to
  `class TransitionalUpgradedEntity(Entity)`
- [x] Add `_fxtr_known_type_id: ClassVar[FxtrID | None]` declaration
- [x] Add a concrete `_fxtr_type_id()` classmethod
- [x] Kept `SelfLoadingLegacyEntity` import — `LegacyCompat` still uses it

This removes the 3 abstract methods that prevent instantiation. The concrete `_fxtr_type_id()`
delegates to `DataclassEntity._fxtr_known_type_id`, which is set by `__init_subclass__` when
the concrete classes pass `fxtr_type=...`.

### 3. Run pyright and verify all 5 errors are fixed

- [x] Run `uv run pyright` in v2/ — 0 errors, 0 warnings, 0 informations

### 4. Write upgrade round-trip test

**File:** `tests/test_legacy_compat/test_transitional_rundata.py`

Create a test that exercises the upgrade path end-to-end with synthetic v1 entities, inspired by
the existing `test_data.py` patterns and the `run_demo.py` example:

- [x] Define synthetic v1 entity types (simple Task-like entities with dependencies)
- [x] Test `TaskWithPredecessors` creation and upgrade:
  - Create a v1 Task, wrap in `TaskWithPredecessors` via `upgrade_from`
  - Verify it can be stored and loaded back (round-trip)
  - Verify `_get_legacy_cid()` returns the correct v1 CID
- [x] Test `ImplProvenanceSet` creation and upgrade
- [x] Test `RunWithPredecessors` creation and upgrade:
  - This involves creating a Task, provenance, and run data
  - Verify the full dependency chain is preserved
- [x] Test `UpgradeCache` integration:
  - `save_upgraded` works for transitional entities
  - `lookup_upgraded` retrieves them correctly
- [x] Test dependency chain:
  - Create tasks with predecessors (A depends on B)
  - Upgrade in dependency order
  - Verify predecessor refs are correct after round-trip

The test should use the existing scope/namespace fixture patterns from `conftest.py` and
`test_data.py`. It should NOT depend on `demo_09_15` or any external experiment definitions.

### 5. Run all tests

- [x] Run `uv run pytest -n auto` in v2/ and confirm all tests pass

### 6. Add pyright to pre-commit

**File:** `.pre-commit-config.yaml`

- [x] Add pyright as a dev dependency in `v2/pyproject.toml` (if not already present)
- [x] Add a local pre-commit hook:
  ```yaml
  - repo: local
    hooks:
      - id: pyright-v2
        name: pyright (v2)
        entry: uv run --directory v2 pyright
        language: system
        files: ^v2/
        pass_filenames: false
  ```

### 7. Verify pre-commit runs pyright

- [x] Run `pre-commit run pyright-v2 --all-files` and confirm it passes

### 8. Update CODE_OVERVIEW.md
- [x] Note the fix in the legacy_compat section if relevant
