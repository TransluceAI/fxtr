I have an old system in
@lib/behaviors/behaviors/staging/object_model/blueprint_loader.py that I am converting to use a
new system in @lib/behaviors/behaviors/staging/object_model/collection_tracking.py. The old
system was used by the code in @lib/behaviors/behaviors/staging/api/ and also in the debug visualizer and examples described below.

# INSTRUCTIONS

This note is the SOURCE OF TRUTH for the current state of the refactor. Edit it as we go. (Still use a separate uncertainties file for questions and uncertainties, but use this file to track in-progress work that you are just saving for later. You can put the uncertainties file in notes folder.)

We will be completing these top-level todos one at a time. After each of these top-level tasks is completed, STOP.

# INITIAL TOP-LEVEL TODOS

1. [DONE] make a new utility in @lib/behaviors/behaviors/staging/api/api_lib.py for loading a fxtr
  collection object from a Python entry-point-style-string: either the name of a module, or the
  name of an attribute in the module. Convert it so that, if its already a Collection, it is just
  used, if it is a fn, the fn is called and must return a Collection, and if it is a ModuleType,
  get the collection via extract_collection_from_module

2. [DONE] Revamp the collection design again to make it more similar to the old design to simplify future
   porting: fill in the "TODO: need to walk the definitions similar to the _create_executed_module_data" piece

3. [DONE] change the fxtr.toml format used in @lib/behaviors/behaviors/staging/examples/ so that instead
  of pointing at source files with namespace strings, it instead points to python module names

4. [DONE] change the fxtrconfig.py file to handle the new fxtr.toml format

5. [DONE] change examples in examples dir to use regular python imports instead of load()

6. change fxtr_cli.py to use collection loading logic instead of the blueprint module stuff, and to load based on a python entry-point-string instead of a blueprint module "local:" name thing

7. change debug_visualizer to show collections instead of how it currently shows executedmoduledata


# IMPORTANT NOTES

- The new utility should handle both "module.name" (import a module) and "module.name:attr" (import and get an attribute) formats
- Need to decide on error handling: what if the function doesn't return a Collection? What if the module has no collection?
- The extract_collection_from_module function exists at collection_tracking.py:524 and raises ValueError if the module isn't a collection module
- api_lib.py currently has build_blueprint_resolver which returns the old HardcodedBlueprintResolver - this will eventually be replaced


# CURRENT STATE

## Todo #1: COMPLETE ✓

Added `load_collection_from_entrypoint` function to `api_lib.py` at line 18. The function:
- Uses `importlib.import_module()` to load modules
- Supports nested attribute paths with `.` separators after `:`
- Handles Collection objects, callables that return Collections, and ModuleType objects
- Raises errors immediately without wrapping (as specified)

## Todo #2: COMPLETE ✓

Implemented the `build` method in `FxtrCollectionBuilder` (collection_tracking.py:258). The implementation:
- Walks entity definitions similar to `_create_executed_module_data` from blueprint_loader
- Uses `_FXTR_ENTITY_TO_BUILDER` to look up entities instead of a graph manager
- Raises errors if an entity is in an incomplete collection builder
- Creates `LocalEntityDefn` objects with canonical paths, aliases, and child tracking
- Infers dependencies from foreign entities
- Returns a complete `Collection` entity with all metadata

Added helper function `_walk_items_dict` to walk the items dictionary and find entity access paths.
Added `_AliasState` dataclass for tracking entity aliases during processing.
Added `_get_entity_key_for_foreign_entity` helper to deduplicate foreign entity key creation and add warnings for entities with unknown provenance (unless they're builtins).

## Todo #3: COMPLETE ✓

Updated fxtr.toml format in both example directories to use Python module entry-point strings instead of file paths:
- Changed `path = "./index.fxtr.py"` to `module = "behaviors.staging.examples.demo_09_15.user.index"`
- Updated both demo_09_15 and demo_agentic_misalignment examples

## Todo #4: COMPLETE ✓

Updated fxtrconfig.py to handle the new format:
- Changed `SourceConfig.path` to `SourceConfig.module`
- Module field now stores Python entry-point strings like "my.module" or "my.module:attr"

## Todo #6: COMPLETE ✓

Updated fxtr_cli.py to use collection loading instead of blueprint module execution:
- Updated `task_matches_namespec` to use `CollectionTask` instead of `BlueprintTask`
- Added `tasks_in_collection` helper function in api_lib.py to extract tasks from collections with proper access path conversion
- Added `CollectionTask` dataclass to replace `BlueprintTask`
- Updated `execmodule` command to load collections using `load_collection_from_entrypoint()`
- Updated `serve` command to use collections and changed URLs from `/view/module/` to `/view/collection/`
- Updated `ls` command to use collections
- Updated `materialize` command to use collections

All commands now:
1. Load collections using `load_collection_from_entrypoint()` with the module entry-point string from config
2. Extract tasks using `tasks_in_collection()` instead of `tasks_in_module()`
3. Save collections to the entity store using `entity_subgraph_to_ipld_blocks()`

### Cleanup

Removed all outdated blueprint-specific code:
- Removed `build_blueprint_resolver()` function from api_lib.py
- Removed `execute_and_save_module()` function from api_lib.py
- Removed `BlueprintTask` dataclass from api_lib.py
- Removed `tasks_in_module()` function from api_lib.py
- Removed `blueprint_loader` import from api_lib.py

All blueprint-related code has been replaced with collection-based equivalents.

## Todo #7: COMPLETE ✓

Updated debug_visualizer to work with collections:

### Python server updates:
- The old `server.py` (which used `ExecutedModuleEntityData`) was already removed
- Updated `locator_server.py` documentation and UI text to reference "collections" instead of "modules"
- Changed docstring example from `{"demo_module": "/view/module/..."}` to `{"demo_collection": "/view/collection/..."}`
- Updated HTML error message from "No modules available" to "No collections available"
- Updated HTML page title text from "Available modules:" to "Available collections:"
- Updated comment from "links to all modules" to "links to all collections"
- The `locator_server.py` implementation was already generic and works with any entities via the locator

### TypeScript UI updates:
- Updated all URL routes from `/view/module/` to `/view/collection/` in main.tsx, EntityKeyLink.tsx, EntityDefinitionPage.tsx
- Updated page titles from "Module:" to "Collection:" in ModulePage.tsx
- Updated section headers from "Module Code", "Module Entity Data", "Raw Module Data" to "Collection Code", "Collection Entity Data", "Raw Collection Data"
- Updated back navigation link from "Back to Module:" to "Back to Collection:"
- Updated ModulePage.tsx to display Collection structure (name, namespace_id) instead of ExecutedModuleEntityData structure (path_in_project, module_code)
- Removed "Collection Code" section since Collection doesn't have a module_code field
- Updated dependencies display to show Collection CIDs as clickable links (currently displays CIDs; could be enhanced later to fetch and display collection names)
- Updated EntityDefinitionPage.tsx to use collection name instead of path_in_project
- Updated comments from "same module"/"different module" to "same collection"/"different collection"

The debug visualizer now fully supports the collection system. The `fxtr_cli.py` serve command uses `FxtrLocatorDebugServer` and generates collection URLs with the format `/view/collection/{cid}`.
