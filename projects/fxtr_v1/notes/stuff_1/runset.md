Goal: Simplify how sets of runs are manipulated in fxtr.


# Current state

The fxtr library exists and is designed as described in lib/fxtr/README.md.

There is a command line program at `lib/fxtr/fxtr/api/fxtr_cli.py` which can be used to view and launch tasks. It uses other files in the `api` subpackage. In particular `lib/fxtr/fxtr/api/task_launcher.py` is how tasks are launched.

Runs are stored in the "state store"

The `fxtr ls` and `fxtr materialize`

# Desired state

The fxtr CLI should instead operate in terms of **runsets**.

A "runset" is a set of runs, such that there is at most one run of any task in the runset, and if run R is in the runset and depends on previous run R', then run R' is also in the runset.

The new version of the launcher and state manager should maintain an "active runset" that tracks its current view of the tasks in the system.

Runsets are represented as a `RunSet` entity, which is stored as a mapping from task CID to run CID. There is also a `RunSetRef` entity which just holds the CID of a RunSet.

Runsets can be manipulated in a few ways:
- There is always a runset called "active" which is saved in the fxtr data directory and has tooling in `lib/fxtr/fxtr/api/api_lib.py` for loading and saving it. The active runset is always saved into the entity storage, and its base32-encoded CID is stored as "runsets/active"
- You can "name" the current runset by using `fxtr runset save`, which saves it to "runsets/{name}"; here "name" must be an identifier-like name and CANNOT be "active"
- You can "load" a saved runset by using `fxtr runset load`, which loads it from "runsets/{name}"
- You can "export" a runset using `fxtr runset export [name]`, which prints out python code for defining a RunSetRef for the named (or active) runset, of the form `core.RunSetRef(core.decode_cid("..."))`
    - (eventual but out of scope now: clean up the imports so you can do `fxtr.RunSetRef` etc instead)
- You can "drop" runs from the active runset using `fxtr runset drop [runs...] [--and-after]` or `fxtr runset drop --all` which removes those runs, and either also drops all downstream runs (if `--and-after`) or errors if there were unmentioned runs
- You can "adopt" orphan runs into the active runset using `fxtr runset adopt [runs...] [--and-before]` which adds those runs and either also adds their ancestors or errors out if there were unmentioned ancestors. Also if this ends up trying to add a run for a task that already has one, that is a conflict and should be an error.

Run ancestors can be found by looking for `RunProvenance` (`lib/fxtr/fxtr/providers/externals_provider.py`) entries in the run's provenance record

The `fxtr ls` cmd should now only show the runs in the current runset, of which there is at most one per task.

The `fxtr materialize` cmd should not accept any arguments about what runs to use or a run resolution strategy. Instead it should always resolve exactly using the runs in the current runset. It should never re-run any run in the runset; you need to drop the runs first to do that. Every successful run from the materialization gets saved into the updated active runset on completion.

The `fxtr serve` command backend should only serve runs from the active runset into the debug UI.

Also `fxtr ls` and `fxtr materialize` and `fxtr serve` should always use the default collection unless otherwise specified. To specify the collection you should use either `-c collectionname` or `-m pythonmodulename` instead of a positional collectionname. In the latter case it does not even have to be in the fxtr config toml.

Individual runs should still be saved the way they are currently saved, and if you do `fxtr runlog {task_name_or_cid}` you should be able to see them (it tries to find by cid if parseable as cid and by pathstring otherwise).

# Implementation plan

## Overview

The implementation involves:
1. Creating runset management utilities in `api_lib.py`
2. Adding new CLI commands for runset manipulation
3. Modifying existing CLI commands to use the active runset
4. Updating the task launcher to save runs into the active runset

## Step 0: Define RunSet and RunSetRef in `core` ✓

**Status: DONE**

## Step 1: Add runset storage utilities to `api_lib.py`

**Location**: `lib/fxtr/fxtr/api/api_lib.py`

Add the following functions:

### 1.1 Path helpers
- `get_runsets_dir(config: FxtrConfig) -> Path` - Returns `state_store / "runsets"`
- `get_active_runset_path(config: FxtrConfig) -> Path` - Returns `runsets_dir / "active"`
- `get_named_runset_path(config: FxtrConfig, name: str) -> Path` - Returns `runsets_dir / name`

### 1.2 Active runset management
- `load_active_runset(config: FxtrConfig) -> RunSet`
  - Read the CID from the "active" file in runsets dir
  - If file doesn't exist, return empty RunSet (empty frozendict)
  - Load the RunSet entity from the entity store using the CID
  - Return the RunSet entity

- `save_active_runset(config: FxtrConfig, runset: RunSet) -> None`
  - Save the RunSet entity to the entity store
  - Get the CID of the saved entity
  - Write the base32-encoded CID to the "active" file
  - Ensure the runsets directory exists

### 1.3 Named runset management
- `load_named_runset(config: FxtrConfig, name: str) -> RunSet`
  - Similar to load_active_runset but for named runsets
  - Error if the named runset doesn't exist

- `save_named_runset(config: FxtrConfig, name: str, runset: RunSet) -> None`
  - Validate that name != "active" (raise ValueError if it is)
  - Validate that name matches identifier pattern (alphanumeric + underscore)
  - Similar to save_active_runset but for named runsets

- `list_named_runsets(config: FxtrConfig) -> list[str]`
  - List all files in runsets dir except "active"
  - Return sorted list of names

### 1.4 Runset manipulation
- `add_run_to_runset(runset: RunSet, task_cid: CID, run_cid: CID) -> RunSet`
  - Check if task_cid already exists in runset, raise ValueError if it does
  - Return new RunSet with the run added (immutable update)

- `remove_run_from_runset(runset: RunSet, task_cid: CID) -> RunSet`
  - Return new RunSet with the run removed
  - Raise KeyError if task_cid not in runset

- `get_run_ancestors(config: FxtrConfig, run_cid: CID) -> list[tuple[CID, CID]]`
  - Load the run entity from entity store
  - Load its provenance record (ImplProvenanceSet)
  - Find all RunProvenance entries in the provenance set
  - For each RunProvenance, extract the run_cid and look up its task
  - Return list of (task_cid, run_cid) pairs

- `get_run_descendants(config: FxtrConfig, runset: RunSet, task_cid: CID) -> set[CID]`
  - For each run in the runset, check if it depends on the given task_cid
  - Return set of task CIDs that depend on the given task (directly or transitively)
  - This requires checking provenance records of all runs in the runset

## Step 2: Add `fxtr runset` command group

**Location**: `lib/fxtr/fxtr/api/fxtr_cli.py`

### 2.1 Create runset command group
- Add a new `typer.Typer()` for the `runset` subcommand
- Register it with the main app using `app.add_typer(runset_app, name="runset")`

### 2.2 Implement `fxtr runset save <name>`
- Load the active runset
- Validate the name (not "active", valid identifier)
- Save it as a named runset
- Print confirmation with the CID

### 2.3 Implement `fxtr runset load <name>`
- Load the named runset
- Save it as the active runset
- Print confirmation with number of runs and CID

### 2.4 Implement `fxtr runset export [name]`
- If name provided, load that named runset, else load active
- Get the RunSet CID
- Print Python code: `core.RunSetRef(core.decode_cid("<base32_cid>"))`

### 2.5 Implement `fxtr runset drop`
Parameters:
- `runs: list[str]` - Task names or CIDs to drop
- `--and-after: bool` - Also drop downstream runs
- `--all: bool` - Drop all runs (conflicts with runs list)
- `-c/--collection` and `-m/--module` - For resolving task names

Logic:
- Load active runset
- If `--all`, clear the entire runset
- Otherwise, resolve task names/CIDs to task CIDs
- If `--and-after`, find all descendants and add to drop list
- If not `--and-after`, check for descendants and error if any exist
- Remove all specified runs from runset
- Save updated active runset
- Print summary of what was dropped

### 2.6 Implement `fxtr runset adopt`
Parameters:
- `runs: list[str]` - Task names or CIDs to adopt (must have runs in state store)
- `--and-before: bool` - Also adopt ancestor runs
- `-c/--collection` and `-m/--module` - For resolving task names

Logic:
- Load active runset
- Resolve task names/CIDs to task CIDs
- For each task, find its completed runs in state store
- If multiple runs exist, error (need explicit run selection - out of scope for now)
- If `--and-before`, recursively find ancestors and add to adopt list
- If not `--and-before`, check for ancestors not in runset and error if any
- Check for conflicts (task already in runset)
- Add all specified runs to runset
- Save updated active runset
- Print summary of what was adopted

### 2.7 Implement `fxtr runset list`
- List all named runsets (not including "active")
- For each, show name and CID
- Show the active runset CID and number of runs

## Step 3: Add `fxtr runlog` command

**Location**: `lib/fxtr/fxtr/api/fxtr_cli.py`

Implement `fxtr runlog <task_identifier>`:
- Parameters:
  - `task_identifier: str` - Task name or CID
  - `-c/--collection` and `-m/--module` - For resolving task names (optional)

Logic:
- Try to parse task_identifier as a CID first
- If that fails, load collection and resolve as a path string
- Find all runs for the task using `api_lib.runs_for_task`
- Display all runs (not just completed ones) with:
  - Run name
  - State (completed/failed/etc)
  - Status message
  - Run CID (if available)
  - Timestamp (if available in run metadata)

## Step 4: Update `fxtr ls` to use active runset

**Location**: `lib/fxtr/fxtr/api/fxtr_cli.py`, function `ls()`

Changes:
- **Remove positional `module_name` argument**
- Add `-c/--collection <name>` option (uses config to find collection)
- Add `-m/--module <module>` option (loads module directly, doesn't need to be in config)
- Default to collection named "default" from config if neither is specified
- Remove `namespec` parameter (keep filtering but make it optional with `-n/--namespec`)
- Load the active runset at the start
- When displaying runs for tasks, only show the run that's in the active runset (at most one per task)
- Add indicator if task is in runset (e.g., "✓ in active runset")
- Update summary section to show runset statistics

## Step 5: Update `fxtr materialize` to use active runset

**Location**: `lib/fxtr/fxtr/api/fxtr_cli.py`, function `materialize()`

Major changes:
- **Remove positional `module_name` argument**
- Add `-c/--collection <name>` and `-m/--module <module>` options (same as `ls`)
- Default to "default" collection
- **Remove `select_run_by` parameter** - always use active runset
- **Remove `rerun_all` parameter** - to rerun, must drop from runset first
- Load active runset at the start
- Use active runset to resolve all TaskResultHandle dependencies
- If a task being materialized is already in the active runset, skip it (no-op)
- After each successful run, add it to the active runset and save
- Still support `--and-previous` for dependency collection
- Still support `--stop-after-failure`

Key implementation detail:
- Pass the active runset's run mappings to `task_launcher.run_all_tasks()` as `explicit_run_deps`
- Filter out tasks already in runset before calling `run_all_tasks()`
- After each task completes, call `add_run_to_runset()` and `save_active_runset()`

## Step 6: Update `fxtr serve` to use active runset

**Location**: `lib/fxtr/fxtr/api/fxtr_cli.py`, function `serve()`

Changes:
- **Remove positional `optional_module_name` argument**
- Add `-c/--collection <name>` and `-m/--module <module>` options
- Default to "default" collection if neither specified
- Load active runset
- Only serve runs that are in the active runset
- Update the named_urls generation to only include active runset runs

## Step 7: Update `fxtr execmodule` for consistency

**Location**: `lib/fxtr/fxtr/api/fxtr_cli.py`, function `execmodule()`

Minor changes:
- Consider renaming or marking as advanced/internal command
- This command doesn't interact with runsets, but should remain for debugging

## Step 8: Update config to support default collection

**Location**: `lib/fxtr/fxtr/api/fxtrconfig.py`

Add validation:
- Add helper function `get_default_collection_name(config: FxtrConfig) -> str`
  - Look for a source named "default" in config.blueprint.sources
  - Raise helpful error if "default" source doesn't exist
  - Suggest available sources in error message

## Step 9: Update task launcher to remove run_resolution_strategy

**Location**: `lib/fxtr/fxtr/api/task_launcher.py`, function `run_all_tasks()`

Changes:
- **Remove `run_resolution_strategy` parameter** - now always use `explicit_run_deps`
- Require that `explicit_run_deps` contains all necessary task dependencies (no automatic resolution)
- Update docstring to reflect that all task result handles must be in `explicit_run_deps`
- The return value already includes TaskRunState with run_cid, which is sufficient
- The caller (materialize) is responsible for:
  - Passing the active runset as `explicit_run_deps`
  - Updating the active runset after each task completes

## Step 10: Add utility for resolving task identifiers

**Location**: `lib/fxtr/fxtr/api/api_lib.py`

Add:
- `resolve_task_identifier(identifier: str, collection: Collection | None = None) -> CID`
  - Try to parse as CID first
  - If fails and collection provided, resolve as path string in collection
  - Raise clear error if neither works

## Testing considerations

For each step, consider:
- Empty runset handling
- Conflict detection (e.g., adopting task already in runset)
- Cycle detection in dependencies (should be caught by existing code)
- Migration from old state store format (old runs exist but not in runset)
- Invalid CIDs in runset files
- Missing entity data for runs in runset

## Migration notes

- Old runs remain in state store and can be viewed with `fxtr runlog`
- Active runset starts empty
- Users must explicitly adopt old runs if desired
- Named runsets are new, no migration needed

# Current state

(Step 0: done.)

Rest: not started.
