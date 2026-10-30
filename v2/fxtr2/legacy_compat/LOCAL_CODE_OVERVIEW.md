
### legacy_compat/

Compatibility layer for storing fxtr v1 entities in fxtr v2's system:

- **`legacy_compat/data.py`**: Main implementation module
  - **`LegacyCompat[T]`**: Wrapper class that stores v1 `EntityBase` instances as v2 entities (extends `SelfLoadingLegacyEntity`)
    - `value`: The wrapped v1 entity
    - `legacy_cid`: The original v1 CID (for integrity verification)
    - Serializes v1 IPLD structure with CID links replaced by FxtrID strong links
    - Deserializes back to the original v1 entity, verifying CID matches
  - **`TransitionalUpgradedEntity`**: Base class for v2 entities that wrap v1 data (extends `Entity`, mixed with `DataclassEntity`). Provides `_fxtr_type_id()`, `_get_type_name_for_cache()`, `_get_legacy_cid()` for `UpgradeCache` integration.
  - **`UpgradeCache`**: Cache for v1-to-v2 entity translations, backed by a `FxtrNamespace`
  - **`PostOrderEntityCollector`**: Custom visitor for collecting v1 entities in dependency order
    - Uses post-order traversal (dependencies before dependents)
    - Ensures CID→FxtrID mappings are available when processing each entity's IPLD
  - **CBOR traversal helpers**: `walk_and_collect_cids()`, `walk_and_replace_cids()`, `walk_and_collect_fxtr_ids()`, `walk_and_replace_fxtr_ids()`

- **`legacy_compat/transitional_rundata.py`**: Transitional data types for task/run management
  - **`TaskWithPredecessors[T]`**: V2 wrapper for v1 tasks with explicit predecessor tracking
  - **`RunWithPredecessors[T]`**: V2 wrapper for v1 runs with predecessor runs
  - **`ImplProvenanceSet`**: V2 wrapper for v1 implementation provenance
  - **`TaskToRunMap`**: Map from tasks to their runs

- **`legacy_compat/transitional_session_manager.py`**: Session manager for running v1 tasks on v2 storage
  - **`TransitionalSessionManager`**: Main session manager class
  - **`RunCacheManager`**: Manages the run cache namespace
    - `save_run()`: Save a run to the cache with predecessor consistency checks
    - `get_run_for_task()`: Look up a run by task
    - `copy_runs_from()`: Copy runs between run cache namespaces
    - `check_consistency()`: Verify cache invariants

- **`legacy_compat/runset_conversion.py`**: Convert v1 runsets to v2 format
  - **`convert_legacy_runset_to_run_cache()`**: Migrate runs from v1 SqliteDict-based runsets to v2 RunCacheManager

- **`legacy_compat/__init__.py`**: Re-exports key classes and functions for convenience

The wrapped entity format uses type ID `!compat.legacy_v1` and stores the original IPLD in a `data` field with CIDs replaced by strong FxtrID links, plus a `legacy_cid` field for verification.

See `plans/phase2/stage4_fxtr1_compat_entities.md` for the full implementation plan and `plans/phase2/stage10_tsm_run_copying.md` for run copying functionality.
