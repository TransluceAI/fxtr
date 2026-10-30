"""Library of functions for the fxtr tool."""

import dataclasses
import importlib
import json
import logging
import pathlib
import types
from typing import Any, Literal

import cbrrr
from frozendict import frozendict
from sqlitedict import SqliteDict

from ..object_model import collection_tracking, core
from ..object_model import locator as locator_module
from . import fxtrconfig


def load_collection_from_entrypoint(entrypoint: str) -> collection_tracking.Collection:
    """Load a Collection from a Python entry-point-style string.

    The entry-point string can be:
    - A module name like "my.module" - will import the module and extract its Collection
    - An attribute path like "my.module:attr" - will import the module and get the attribute
    - Nested attributes like "my.module:attr.nested.value" are also supported

    Once the target is loaded:
    - If it's already a Collection, it's returned directly
    - If it's a callable, it's called and must return a Collection
    - If it's a ModuleType, extract_collection_from_module is called on it

    Args:
        entrypoint: Python entry-point string

    Returns:
        The loaded Collection

    Raises:
        ValueError: If the module/attribute can't be found or doesn't resolve to a Collection
    """
    # Split on ':' to separate module from attribute path
    if ":" in entrypoint:
        module_name, attr_path = entrypoint.split(":", 1)
    else:
        module_name = entrypoint
        attr_path = None

    # Import the module
    module = importlib.import_module(module_name)

    # Get the target object
    if attr_path is None:
        target = module
    else:
        # Support nested attributes like "foo.bar.baz"
        target = module
        for attr_name in attr_path.split("."):
            target = getattr(target, attr_name)

    # Convert the target to a Collection
    if isinstance(target, collection_tracking.Collection):
        return target
    elif callable(target):
        result = target()
        if not isinstance(result, collection_tracking.Collection):
            raise ValueError(
                f"Callable at {entrypoint} returned {type(result).__name__}, not a Collection"
            )
        return result
    elif isinstance(target, types.ModuleType):
        return collection_tracking.extract_collection_from_module(target)
    else:
        raise ValueError(
            f"Entry-point {entrypoint} resolved to {type(target).__name__}, which cannot be"
            " converted to a Collection"
        )


@dataclasses.dataclass(frozen=True)
class CollectionTask:
    """A task in a collection."""

    module_name: str | None
    canonical_path: str | None
    alias_paths: list[str]
    task_entity: core.Task | core.Workflow


def tasks_in_collection(
    collection: collection_tracking.Collection, module_name: str | None = None
) -> list[CollectionTask]:
    """Extract all Task entities from a collection.

    Args:
        collection: The collection to extract tasks from
        module_name: Optional name of the module for display purposes

    Returns:
        List of CollectionTask objects containing task information
    """
    tasks = []

    # Iterate over the entity definitions in the collection
    for definition in collection.entity_defns:
        entity = definition.entity
        canonical_path = definition.canonical_access_path

        if isinstance(entity, core.Task | core.Workflow):
            # Convert canonical_path from frozen IPLD to string representation
            if canonical_path:
                canonical_path_obj = core.frozen_ipldable_to_access_path(canonical_path)
                canonical_path_str = core.access_path_to_str(
                    root_str="", access_path=canonical_path_obj
                )
            else:
                canonical_path_str = None

            # Convert aliases from frozen IPLD to string representations
            alias_paths = [
                core.access_path_to_str(
                    root_str="", access_path=core.frozen_ipldable_to_access_path(alias)
                )
                for alias in definition.aliases
            ]

            collection_task = CollectionTask(
                module_name=module_name,
                canonical_path=canonical_path_str,
                task_entity=entity,
                alias_paths=alias_paths,
            )
            tasks.append(collection_task)

    return tasks


def get_active_data_locator(
    config: fxtrconfig.FxtrConfig,
) -> locator_module.WritableLocator:
    """Set up and return a locator for the active entity store.

    This is the single place that selects the entity-store backend based on
    `config.data.entity_store_format`: the CID-addressed CBOR file store (default) or a single LMDB
    database. All entity reads and writes route through here so the format switch is honored
    everywhere.

    Args:
        config: The fxtr config containing data store settings

    Returns:
        A WritableLocator for the active entity store (concrete type depends on the configured
        format).
    """
    if config.data.entity_store_format == "lmdb":
        lmdb_path = fxtrconfig.get_active_entity_store_lmdb(config)
        lmdb_path.mkdir(parents=True, exist_ok=True)
        return locator_module.LMDBEntityLocator(
            lmdb_path, map_size=config.data.lmdb_map_size_gb * 1024**3
        )
    entity_store_path = fxtrconfig.get_active_entity_store(config)
    entity_store_path.mkdir(parents=True, exist_ok=True)
    return locator_module.ClosedBinaryCBOROnDiskLocator(entity_store_path)


@dataclasses.dataclass
class TaskRunState:
    """State of a task run."""

    run_name: str
    state: Literal["pending", "starting", "running", "completed", "failed", "cancelled"]
    status_message: str | None
    run_cid: cbrrr.CID | None


def find_all_tasks_with_runs(config: fxtrconfig.FxtrConfig) -> list[cbrrr.CID]:
    """Find all task CIDs that have runs stored.

    Args:
        config: The fxtr config

    Returns:
        List of task CIDs that have runs
    """
    state_store = fxtrconfig.get_active_state_store(config)
    tasks_dir = state_store / "tasks"

    if not tasks_dir.exists():
        return []

    task_cids = []
    for task_dir in tasks_dir.iterdir():
        if task_dir.is_dir():
            try:
                # Task directory names are base32-encoded CIDs
                task_cid = cbrrr.CID.decode(task_dir.name)
                # Check if this task has any runs
                runs_dir = task_dir / "runs"
                if runs_dir.exists() and any(runs_dir.iterdir()):
                    task_cids.append(task_cid)
            except Exception as e:
                # Skip directories that aren't valid CIDs
                logging.warning(f"Could not parse task directory '{task_dir.name}' as CID: {e}")
                continue

    return task_cids


def runs_for_task(
    config: fxtrconfig.FxtrConfig, task_cid: cbrrr.CID, only_completed: bool = False
) -> list[TaskRunState]:
    """Get all runs for a specific task.

    Args:
        config: The fxtr config
        task_cid: The task CID to get runs for
        only_completed: Whether to only return successful runs

    Returns:
        List of TaskRunState objects for the task
    """
    state_store = fxtrconfig.get_active_state_store(config)
    task_cid_str = task_cid.encode(base="base32")
    runs_dir = state_store / "tasks" / task_cid_str / "runs"

    if not runs_dir.exists():
        return []

    runs = []
    for run_file in runs_dir.iterdir():
        if run_file.is_file() and run_file.suffix == ".json":
            try:
                with open(run_file, "r") as f:
                    run_data = json.load(f)

                # Parse the run_cid if present
                run_cid = None
                if "run_cid" in run_data and run_data["run_cid"] is not None:
                    run_cid = cbrrr.CID.decode(run_data["run_cid"])

                if only_completed and run_data["state"] != "completed":
                    continue

                run_state = TaskRunState(
                    run_name=run_file.stem,  # filename without .json extension
                    state=run_data["state"],
                    status_message=run_data.get("status_message"),
                    run_cid=run_cid,
                )
                runs.append(run_state)
            except Exception as e:
                # Skip files that can't be parsed
                logging.warning(f"Could not parse run file '{run_file}': {e}")
                continue

    return runs


def save_run_state(
    config: fxtrconfig.FxtrConfig, task_cid: cbrrr.CID, task_run_state: TaskRunState
) -> None:
    """Save a task run state to disk.

    Args:
        config: The fxtr config
        task_cid: The task CID
        task_run_state: The run state to save
    """
    state_store = fxtrconfig.get_active_state_store(config)
    task_cid_str = task_cid.encode(base="base32")
    runs_dir = state_store / "tasks" / task_cid_str / "runs"

    # Ensure the directory exists
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Prepare the data to save
    run_data = {
        "state": task_run_state.state,
        "status_message": task_run_state.status_message,
        "run_cid": task_run_state.run_cid.encode(base="base32") if task_run_state.run_cid else None,
    }

    # Save to file
    run_file = runs_dir / f"{task_run_state.run_name}.json"
    with open(run_file, "w") as f:
        json.dump(run_data, f, indent=2)


# ============================================================================
# Runtime env config discovery
# ============================================================================


def discover_secret_env_var_declarations(
    collection: collection_tracking.Collection,
) -> dict[
    core.ExternalServiceIdentifier,
    list[core.SecretFromEnvVarDeclaration | core.SecretFromOptionalEnvVarDeclaration],
]:
    """Discover all secret env var declarations in a collection and its dependencies.

    This function traverses the given collection and all of its dependencies recursively,
    collecting all SecretFromEnvVarDeclaration and SecretFromOptionalEnvVarDeclaration entities.
    For each handle (ExternalServiceIdentifier), it returns all declarations found. Multiple
    declarations for the same handle indicate a conflict.

    Args:
        collection: The collection to start the search from

    Returns:
        A dictionary mapping each handle to a list of secret declarations for that handle
    """
    # Collect all collections to search (including dependencies)
    collections_to_search = set()
    collections_queue = [collection]

    while collections_queue:
        current_collection = collections_queue.pop()
        if current_collection not in collections_to_search:
            collections_to_search.add(current_collection)
            collections_queue.extend(current_collection.dependencies)

    # Collect all secret declarations
    all_declarations: list[
        core.SecretFromEnvVarDeclaration | core.SecretFromOptionalEnvVarDeclaration
    ] = []

    for coll in collections_to_search:
        for defn in coll.entity_defns:
            entity = defn.entity
            if isinstance(
                entity, (core.SecretFromEnvVarDeclaration, core.SecretFromOptionalEnvVarDeclaration)
            ):
                all_declarations.append(entity)

    # Group by handle
    declarations_by_handle: dict[
        core.ExternalServiceIdentifier,
        list[core.SecretFromEnvVarDeclaration | core.SecretFromOptionalEnvVarDeclaration],
    ] = {}

    for declaration in all_declarations:
        handle = declaration.handle
        if handle not in declarations_by_handle:
            declarations_by_handle[handle] = []
        declarations_by_handle[handle].append(declaration)

    return declarations_by_handle


def discover_runtime_env_config_assignments(
    collection: collection_tracking.Collection,
) -> dict[
    core.ExternalServiceIdentifier[core.RuntimeEnvRetriever[Any]],
    list[core.RuntimeEnvConfigAssignment],
]:
    """Discover all RuntimeEnvConfigAssignment entities in a collection and its dependencies.

    This function traverses the given collection and all of its dependencies recursively,
    collecting all RuntimeEnvConfigAssignment entities. For each plan (ExternalServiceIdentifier),
    it returns only the assignments that are not overridden by other discovered assignments.

    An assignment A is considered overridden if there exists another assignment B where
    A is in B.overrides (either directly or transitively).

    Args:
        collection: The collection to start the search from

    Returns:
        A dictionary mapping each plan to a list of non-overridden RuntimeEnvConfigAssignment
        entities for that plan
    """
    # Collect all collections to search (including dependencies)
    collections_to_search = set()
    collections_queue = [collection]

    while collections_queue:
        current_collection = collections_queue.pop()
        if current_collection not in collections_to_search:
            collections_to_search.add(current_collection)
            collections_queue.extend(current_collection.dependencies)

    # Collect all RuntimeEnvConfigAssignment entities
    all_assignments: list[core.RuntimeEnvConfigAssignment] = []

    for coll in collections_to_search:
        for defn in coll.entity_defns:
            entity = defn.entity
            if isinstance(entity, core.RuntimeEnvConfigAssignment):
                all_assignments.append(entity)

    # Group by plan
    assignments_by_plan: dict[
        core.ExternalServiceIdentifier, list[core.RuntimeEnvConfigAssignment]
    ] = {}

    for assignment in all_assignments:
        plan = assignment.handle
        if plan not in assignments_by_plan:
            assignments_by_plan[plan] = []
        assignments_by_plan[plan].append(assignment)

    # For each plan, filter out overridden assignments
    result: dict[core.ExternalServiceIdentifier, list[core.RuntimeEnvConfigAssignment]] = {}

    for plan, assignments in assignments_by_plan.items():
        # Build set of all transitively overridden assignments
        all_overridden: set[core.RuntimeEnvConfigAssignment] = set()

        for assignment in assignments:
            # Add direct overrides
            for overridden_assignment in assignment.overrides:
                all_overridden.add(overridden_assignment)

            # Make sure we have handled transitive overrides (if B overrides A, and C overrides B,
            # then C overrides A). This should already happen because we should discover all
            # overridden assignments recursively in the collection.
            for other in assignment.overrides:
                if other not in all_assignments:
                    raise RuntimeError(
                        f"Internal error: Overridden assignment {other} should have been discovered"
                        " during collection discovery?"
                    )

        # Keep only non-overridden assignments
        non_overridden = [a for a in assignments if a not in all_overridden]
        result[plan] = non_overridden

    return result


def discover_runtime_coordinator_bindings(
    collection: collection_tracking.Collection,
) -> dict[
    core.ExternalServiceIdentifier[Any],
    list[core.RuntimeCoordinatorBinding[Any]],
]:
    """
    Discover all RuntimeCoordinatorBinding entities in a collection and its dependencies.

    This function traverses the given collection and all of its dependencies recursively,
    collecting all RuntimeCoordinatorBinding entities. For each handle (ExternalServiceIdentifier),
    it returns only the bindings that are not overridden by other discovered bindings.

    A binding A is considered overridden if there exists another binding B where
    A is in B.overrides (either directly or transitively).

    Args:
        collection: The collection to start the search from

    Returns:
        A dictionary mapping each handle to a list of non-overridden RuntimeCoordinatorBinding
        entities for that handle.
    """
    # Collect all collections to search (including dependencies)
    collections_to_search = set()
    collections_queue = [collection]

    while collections_queue:
        current_collection = collections_queue.pop()
        if current_collection not in collections_to_search:
            collections_to_search.add(current_collection)
            collections_queue.extend(current_collection.dependencies)

    # Collect all RuntimeCoordinatorBinding entities
    all_bindings: list[core.RuntimeCoordinatorBinding[Any]] = []

    for coll in collections_to_search:
        for defn in coll.entity_defns:
            entity = defn.entity
            if isinstance(entity, core.RuntimeCoordinatorBinding):
                all_bindings.append(entity)

    # Group by handle
    bindings_by_handle: dict[
        core.ExternalServiceIdentifier[Any], list[core.RuntimeCoordinatorBinding[Any]]
    ] = {}

    for binding in all_bindings:
        handle = binding.handle
        if handle not in bindings_by_handle:
            bindings_by_handle[handle] = []
        bindings_by_handle[handle].append(binding)

    # For each handle, filter out overridden bindings
    result: dict[core.ExternalServiceIdentifier[Any], list[core.RuntimeCoordinatorBinding[Any]]] = (
        {}
    )

    for handle, bindings in bindings_by_handle.items():
        # Build set of all transitively overridden bindings
        all_overridden: set[core.RuntimeCoordinatorBinding[Any]] = set()

        for binding in bindings:
            # Add direct overrides
            for overridden_binding in binding.overrides:
                all_overridden.add(overridden_binding)

            # Make sure we have handled transitive overrides
            for other in binding.overrides:
                if other not in all_bindings:
                    raise RuntimeError(
                        f"Internal error: Overridden binding {other} should have been discovered"
                        " during collection discovery?"
                    )

        # Keep only non-overridden bindings
        non_overridden = [b for b in bindings if b not in all_overridden]
        result[handle] = non_overridden

    return result


# ============================================================================
# Runset management
# ============================================================================


@dataclasses.dataclass(frozen=True)
class PyOnlyRunSet:
    """Python-only representation of a run set, which is not a fxtr entity.

    Attributes:
        runs: A mapping from task CID to run CID.
    """

    runs: frozendict[core.CID_Of[core.Task | core.Workflow], core.CID_Of[core.Run]]


def get_runsets_dir(config: fxtrconfig.FxtrConfig) -> pathlib.Path:
    """Get the path to the runsets directory.

    Args:
        config: The fxtr config

    Returns:
        Path to the runsets directory
    """
    state_store = fxtrconfig.get_active_state_store(config)
    return state_store / "runsets"


def get_active_runset_path(config: fxtrconfig.FxtrConfig) -> pathlib.Path:
    """Get the path to the active runset sqlite database file.

    Args:
        config: The fxtr config

    Returns:
        Path to the active runset sqlite database file
    """
    return get_runsets_dir(config) / "active.sqlite"


def get_named_runset_path(config: fxtrconfig.FxtrConfig, name: str) -> pathlib.Path:
    """Get the path to a named runset sqlite database file.

    Args:
        config: The fxtr config
        name: The runset name

    Returns:
        Path to the named runset sqlite database file
    """
    return get_runsets_dir(config) / f"{name}.sqlite"


# Keys in the active runset's SqliteDict that begin with this prefix do NOT belong to the runset
# itself: they hold the co-located, durable run-graph cache (see ActiveRunManager). The prefix is
# disjoint from base32-encoded CIDs (which use the alphabet [a-z2-7] and never start with "_"), so
# runset readers can cheaply skip them and the two datasets share one file (and one transaction)
# without colliding.
RUN_GRAPH_CACHE_KEY_PREFIX = "_rg/"


def _load_runset_from_sqlitedict(db_path: pathlib.Path) -> core.RunSet:
    """Load a RunSet from a SqliteDict database.

    Args:
        db_path: Path to the SqliteDict database file

    Returns:
        The loaded RunSet entity
    """
    runs = {}
    with SqliteDict(str(db_path)) as db:
        import tqdm

        for key in tqdm.tqdm(db.iterkeys(), desc="Loading runset", total=len(db)):
            # Skip co-located run-graph cache rows; only runset rows are CID-keyed.
            if key.startswith(RUN_GRAPH_CACHE_KEY_PREFIX):
                continue
            value = db[key]
            task_cid = cbrrr.CID.decode(key)
            run_cid = cbrrr.CID(value)
            runs[task_cid] = run_cid
    return core.RunSet(runs=frozendict(runs))


def _save_runset_to_sqlitedict(db_path: pathlib.Path, runset: core.RunSet | PyOnlyRunSet) -> None:
    """Save a RunSet to a SqliteDict database (full overwrite).

    Args:
        db_path: Path to the SqliteDict database file
        runset: The RunSet entity to save
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Delete existing file to ensure clean overwrite
    if db_path.exists():
        db_path.unlink()
    with SqliteDict(str(db_path), autocommit=True) as db:
        for task_cid, run_cid in runset.runs.items():
            # Use base32-encoded string as key, bytes as value
            db[task_cid.encode(base="base32")] = bytes(run_cid)


def _save_runset_to_path(json_path: pathlib.Path, runset: core.RunSet | PyOnlyRunSet) -> None:
    """Save a runset to JSON at the given path.

    Args:
        json_path: Path to save the JSON file
        runset: The RunSet entity to save
    """
    json_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "runs": {k.encode(base="base32"): v.encode(base="base32") for k, v in runset.runs.items()}
    }
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)


def _load_runset_from_path(
    config: fxtrconfig.FxtrConfig,
    db_path: pathlib.Path,
    json_path: pathlib.Path,
    old_cid_path: pathlib.Path,
) -> core.RunSet:
    """Load a runset with migration from old formats.

    Priority:
    1. db exists -> load from db
    2. JSON exists -> load from JSON, migrate to db, delete JSON
    3. old CID format exists -> load via entity_loader, migrate to db, delete old

    Args:
        config: The fxtr config
        db_path: Path to the sqlite database file (new format)
        json_path: Path to the JSON format file (old format)
        old_cid_path: Path to the old CID format file (oldest format)

    Returns:
        The loaded RunSet entity

    Raises:
        FileNotFoundError: If none of the formats exist
    """
    if db_path.exists():
        # New format: load from sqlitedict directly
        return _load_runset_from_sqlitedict(db_path)

    elif json_path.exists():
        # Old JSON format: load and migrate to sqlitedict
        with open(json_path, "r") as f:
            data = json.load(f)
        runs = frozendict(
            {cbrrr.CID.decode(k): cbrrr.CID.decode(v) for k, v in data["runs"].items()}
        )
        runset = core.RunSet(runs=runs)

        # Migrate: save in new sqlitedict format and delete JSON file
        _save_runset_to_sqlitedict(db_path, runset)
        json_path.unlink()
        return runset

    elif old_cid_path.exists():
        # Oldest format: load CID, fetch entity, then migrate to sqlitedict
        with open(old_cid_path, "r") as f:
            cid_str = f.read().strip()

        try:
            runset_cid = cbrrr.CID.decode(cid_str)
        except Exception as e:
            raise RuntimeError(f"Invalid CID in runset file: {cid_str}") from e

        data_locator = get_active_data_locator(config)
        entity_loader = locator_module.CachingEntityLoader.from_locator(data_locator)
        runset = entity_loader(runset_cid)

        if not isinstance(runset, core.RunSet):
            raise RuntimeError(
                f"Runset CID {cid_str} does not point to a RunSet entity,"
                f" got {type(runset).__name__}"
            )

        # Migrate: save in new sqlitedict format and delete old file
        _save_runset_to_sqlitedict(db_path, runset)
        old_cid_path.unlink()
        return runset

    else:
        raise FileNotFoundError(f"Runset not found at {db_path}, {json_path}, or {old_cid_path}")


def load_active_runset(config: fxtrconfig.FxtrConfig) -> core.RunSet:
    """Load the active runset.

    Args:
        config: The fxtr config

    Returns:
        The active RunSet entity (empty if no active runset exists)
    """
    db_path = get_active_runset_path(config)  # active.sqlite
    json_path = db_path.parent / "active.json"  # old JSON format
    old_cid_path = db_path.parent / "active"  # oldest format (CID only)

    try:
        return _load_runset_from_path(config, db_path, json_path, old_cid_path)
    except FileNotFoundError:
        # No active runset exists, return empty
        return core.RunSet(runs=frozendict())


def save_active_runset(config: fxtrconfig.FxtrConfig, runset: core.RunSet | PyOnlyRunSet) -> None:
    """Save the active runset.

    Args:
        config: The fxtr config
        runset: The RunSet entity to save
    """
    db_path = get_active_runset_path(config)
    _save_runset_to_sqlitedict(db_path, runset)


def insert_into_active_runset(
    config: fxtrconfig.FxtrConfig, task_cid: cbrrr.CID, run_cid: cbrrr.CID
) -> None:
    """Insert a single run into the active runset.

    Args:
        config: The fxtr config
        task_cid: The task CID
        run_cid: The run CID
    """
    db_path = get_active_runset_path(config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteDict(str(db_path), autocommit=True) as db:
        # Use base32-encoded string as key, bytes as value
        db[task_cid.encode(base="base32")] = bytes(run_cid)


def drop_from_active_runset(config: fxtrconfig.FxtrConfig, task_cid: cbrrr.CID) -> None:
    """Remove a single run from the active runset.

    Args:
        config: The fxtr config
        task_cid: The task CID to remove
    """
    db_path = get_active_runset_path(config)
    if not db_path.exists():
        return  # Nothing to remove if the database doesn't exist
    with SqliteDict(str(db_path), autocommit=True) as db:
        key = task_cid.encode(base="base32")
        if key in db:
            del db[key]


def load_named_runset(config: fxtrconfig.FxtrConfig, name: str) -> core.RunSet:
    """Load a named runset.

    Args:
        config: The fxtr config
        name: The runset name

    Returns:
        The named RunSet entity

    Raises:
        FileNotFoundError: If the named runset doesn't exist
    """
    db_path = get_named_runset_path(config, name)  # {name}.sqlite
    json_path = db_path.parent / f"{name}.json"  # old JSON format
    old_cid_path = db_path.parent / name  # oldest format (CID only)

    try:
        return _load_runset_from_path(config, db_path, json_path, old_cid_path)
    except FileNotFoundError:
        raise FileNotFoundError(f"Named runset '{name}' does not exist")


def save_named_runset(
    config: fxtrconfig.FxtrConfig, name: str, runset: core.RunSet | PyOnlyRunSet
) -> None:
    """Save a named runset.

    Args:
        config: The fxtr config
        name: The runset name (must not be "active")
        runset: The RunSet entity to save

    Raises:
        ValueError: If name is "active" or doesn't match identifier pattern
    """
    import re

    # Validate name
    if name == "active":
        raise ValueError("Cannot use 'active' as a named runset name")

    if not re.match(r"^[a-zA-Z0-9_]+$", name):
        raise ValueError(
            f"Invalid runset name '{name}': must contain only alphanumeric and underscore"
        )

    db_path = get_named_runset_path(config, name)
    _save_runset_to_sqlitedict(db_path, runset)


def list_named_runsets(config: fxtrconfig.FxtrConfig) -> list[str]:
    """List all named runsets.

    Args:
        config: The fxtr config

    Returns:
        Sorted list of named runset names (excluding "active")
    """
    runsets_dir = get_runsets_dir(config)

    if not runsets_dir.exists():
        return []

    names = set()
    for path in runsets_dir.iterdir():
        if path.is_file():
            # New sqlite format: {name}.sqlite
            if path.suffix == ".sqlite" and path.stem != "active":
                names.add(path.stem)
            # Old JSON format: {name}.json
            elif path.suffix == ".json" and path.stem != "active":
                names.add(path.stem)
            # Oldest format: {name} (no extension, not "active")
            elif path.suffix == "" and path.name != "active":
                names.add(path.name)

    return sorted(names)


def export_runset_to_json(config: fxtrconfig.FxtrConfig, name: str | None = None) -> pathlib.Path:
    """Export a runset from sqlite to JSON format.

    Creates a JSON file alongside the sqlite database file for backup/recovery purposes.

    Args:
        config: The fxtr config
        name: Runset name, or None for active runset

    Returns:
        Path to the created JSON file

    Raises:
        FileNotFoundError: If the runset doesn't exist
    """
    if name is None:
        runset = load_active_runset(config)
        json_path = get_runsets_dir(config) / "active.json"
    else:
        runset = load_named_runset(config, name)
        json_path = get_runsets_dir(config) / f"{name}.json"

    _save_runset_to_path(json_path, runset)
    return json_path


def add_run_to_runset(runset: core.RunSet, task_cid: cbrrr.CID, run_cid: cbrrr.CID) -> core.RunSet:
    """Add a run to a runset (immutable).

    Args:
        runset: The original runset
        task_cid: The task CID
        run_cid: The run CID

    Returns:
        A new RunSet with the run added

    Raises:
        ValueError: If the task already exists in the runset
    """
    if task_cid in runset.runs:
        raise ValueError(f"Task {task_cid.encode('base32')} already exists in runset")

    from frozendict import frozendict

    new_runs = dict(runset.runs)
    new_runs[task_cid] = run_cid
    return core.RunSet(runs=frozendict(new_runs))


def remove_run_from_runset(runset: core.RunSet, task_cid: cbrrr.CID) -> core.RunSet:
    """Remove a run from a runset (immutable).

    Args:
        runset: The original runset
        task_cid: The task CID to remove

    Returns:
        A new RunSet with the run removed

    Raises:
        KeyError: If the task doesn't exist in the runset
    """
    if task_cid not in runset.runs:
        raise KeyError(f"Task {task_cid.encode('base32')} not found in runset")

    from frozendict import frozendict

    new_runs = dict(runset.runs)
    del new_runs[task_cid]
    return core.RunSet(runs=frozendict(new_runs))


def get_run_ancestors(
    config: fxtrconfig.FxtrConfig, run_cid: cbrrr.CID
) -> list[tuple[cbrrr.CID, cbrrr.CID]]:
    """Get all ancestor runs of a given run.

    Args:
        config: The fxtr config
        run_cid: The run CID

    Returns:
        List of (task_cid, run_cid) pairs for all ancestor runs
    """
    from ..providers import externals_provider

    # Load the run entity
    data_locator = get_active_data_locator(config)
    entity_loader = locator_module.CachingEntityLoader.from_locator(data_locator)
    run = entity_loader(run_cid)

    if not isinstance(run, core.Run):
        raise RuntimeError(f"CID {run_cid.encode('base32')} is not a Run entity")

    # Load the provenance record
    provenance = entity_loader(run.provenance)
    if not isinstance(provenance, core.ImplProvenanceSet):
        raise RuntimeError(
            f"Run provenance CID {run.provenance.encode('base32')} is not an ImplProvenanceSet"
        )

    # Find all RunProvenance entries
    ancestors = []
    for prov_entity in provenance.provenances.values():
        if isinstance(prov_entity, externals_provider.RunProvenance):
            ancestor_run_cid = prov_entity.run_cid
            # Load the ancestor run to get its task CID
            ancestor_run = entity_loader(ancestor_run_cid)
            if isinstance(ancestor_run, core.Run):
                ancestors.append((ancestor_run.task, ancestor_run_cid))

    return ancestors


def get_run_descendants(
    config: fxtrconfig.FxtrConfig, runset: core.RunSet, task_cid: cbrrr.CID
) -> set[cbrrr.CID]:
    """Get all descendant tasks of a given task in a runset.

    Args:
        config: The fxtr config
        runset: The runset to search within
        task_cid: The task CID

    Returns:
        Set of task CIDs that depend on the given task (directly or transitively)
    """
    # Build a dependency graph
    dependencies: dict[cbrrr.CID, set[cbrrr.CID]] = {}  # task -> set of tasks it depends on

    for run_task_cid, run_cid in runset.runs.items():
        ancestors = get_run_ancestors(config, run_cid)
        ancestor_task_cids = {ancestor_task_cid for ancestor_task_cid, _ in ancestors}
        dependencies[run_task_cid] = ancestor_task_cids

    # Find all descendants transitively
    descendants = set()
    to_check = [task_cid]

    while to_check:
        current = to_check.pop()
        for dependent_task, deps in dependencies.items():
            if current in deps and dependent_task not in descendants:
                descendants.add(dependent_task)
                to_check.append(dependent_task)

    return descendants


def resolve_task_identifier(
    identifier: str, collection: collection_tracking.Collection | None = None
) -> cbrrr.CID:
    """Resolve a task identifier to a CID.

    Args:
        identifier: Either a CID string or a task path string
        collection: Optional collection to resolve path strings

    Returns:
        The resolved task CID

    Raises:
        ValueError: If the identifier cannot be resolved
    """
    # Try to parse as CID first
    try:
        return cbrrr.CID.decode(identifier)
    except Exception:
        pass

    # Try to resolve as path string in collection
    if collection is None:
        raise ValueError(
            f"Cannot resolve '{identifier}' as task: not a valid CID and no collection provided"
        )

    # Find the task by path in the collection
    for defn in collection.entity_defns:
        if not isinstance(defn.entity, core.Task):
            continue

        # Check canonical path
        if defn.canonical_access_path:
            canonical_path_obj = core.frozen_ipldable_to_access_path(defn.canonical_access_path)
            canonical_path_str = core.access_path_to_str(
                root_str="", access_path=canonical_path_obj
            )
            if canonical_path_str == identifier:
                return core.entity_cid(defn.entity)

        # Check aliases
        for alias in defn.aliases:
            alias_path_obj = core.frozen_ipldable_to_access_path(alias)
            alias_path_str = core.access_path_to_str(root_str="", access_path=alias_path_obj)
            if alias_path_str == identifier:
                return core.entity_cid(defn.entity)

    raise ValueError(f"Cannot resolve '{identifier}' as task: not found in collection")


# Runserver state management


@dataclasses.dataclass
class RunserverState:
    """State of a running fxtr runserver."""

    runner_url: str


def get_runserver_state_path(config: fxtrconfig.FxtrConfig) -> pathlib.Path:
    """Get the path to the runserver.json state file."""
    state_dir = fxtrconfig.get_active_state_store(config)
    return state_dir / "runserver.json"


def save_runserver_state(config: fxtrconfig.FxtrConfig, runner_url: str) -> None:
    """Save runserver state to disk.

    Args:
        config: The fxtr configuration
        runner_url: The URL where the backend runner is available

    Raises:
        RuntimeError: If a runserver state file already exists
    """
    state_path = get_runserver_state_path(config)

    if state_path.exists():
        raise RuntimeError(
            f"Runserver state file already exists at {state_path}. "
            "Another server may be running, or a previous server may have exited uncleanly. "
            "If you're sure no server is running, delete this file manually."
        )

    state = RunserverState(runner_url=runner_url)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(dataclasses.asdict(state), indent=2))


def load_runserver_state(config: fxtrconfig.FxtrConfig) -> RunserverState | None:
    """Load runserver state from disk.

    Returns:
        RunserverState if the file exists, None otherwise
    """
    state_path = get_runserver_state_path(config)

    if not state_path.exists():
        return None

    try:
        data = json.loads(state_path.read_text())
        return RunserverState(**data)
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        raise RuntimeError(f"Failed to parse runserver state file: {e}")


def remove_runserver_state(config: fxtrconfig.FxtrConfig) -> None:
    """Remove runserver state file from disk."""
    state_path = get_runserver_state_path(config)
    if state_path.exists():
        state_path.unlink()
