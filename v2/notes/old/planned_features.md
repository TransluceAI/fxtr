# Planned Features

This document describes features that are designed but not yet implemented in fxtr2.

## Task Runner and Automatic Provenance Tracking

The core motivation for fxtr's provenance system is to enable automatic tracking of data lineage during task execution.

### Intended Flow

1. **Top-level task definition**: Create entities describing:
   - Input arguments for a job
   - Environment specification (Python version, dependencies, entry point)

2. **Task runner setup**: The runner:
   - Starts the environment
   - Captures a lockfile as a context entity
   - Creates a `c` node attached to the context
   - Creates a `.` node whose `_provenance_parent` is this `c` node
   - Creates a `p` node wrapping input arguments with metadata about the job

3. **Task execution**: Application code runs with:
   - `ACTIVE_NODE_CONTEXT` set to the `.` node with provenance parent
   - All created entities automatically inherit this context
   - A `ProvenanceArena` (not yet implemented) inspects `_provenance_parent` on stored entities

4. **Task completion**: The runner:
   - Retrieves the provenance node for the final result
   - Saves this as the actual job output
   - Top-level code can unwrap to get data or use provenance API to inspect history

### Components Needed

- **ProvenanceArena**: Arena that automatically handles `_provenance_parent` during store/load
- **Task runner**: Orchestrator that sets up execution context
- **Environment capture**: Logic to serialize and restore execution environments

## "l" (Lift) Nodes

Lift nodes were designed as a potential simplification of path (`p`) nodes. The exact semantics are not fully specified. If implemented, they would provide an alternative way to attach provenance to a subgraph.

**Status**: Design incomplete, not implemented.

## Streaming Outputs

The `StreamSink` and `OutputStream` classes in `entity_serdes.py` are experimental infrastructure for streaming computation results.

### Intended Use

- Tasks can emit intermediate results as they compute
- Streams can be nested (substreams)
- When a stream finishes, it provides a final result that "replaces" the streamed items

**Status**: API exists but not integrated with task runner.

## Database Signaling

The SQLite backend may eventually support signaling mechanisms (possibly via files and inotify) to notify readers of:
- Pin creation/removal in a namespace
- Garbage arena cleanup

**Status**: Mentioned in sqlite_arena.py but not implemented.

## Full Reference Tracking for Visualization

The system tracks strong vs weak links and backrefs. This is intended to support:
- Visualizing data flow graphs
- Understanding which entities "own" vs "reference" others
- Debugging data lineage

**Status**: Infrastructure exists, visualization tools not built.

## Global Type Arena

Currently, when using `DataclassEntity`, the type builtin (e.g., `FxtrID.builtin("my_type")`) must be explicitly tracked in the arena before storing entities of that type. This is because the entity's CBOR includes a weak reference to the type.

### Planned Approach

- A global arena where types register themselves when `DataclassEntity` subclasses are defined
- When creating a new arena, types can be copied or attached from the global arena
- This would make `entity.store()` work without manual `track_builtin()` calls

**Status**: Not implemented. Currently requires explicit `arena.track_builtin(type_id)` before storing.
