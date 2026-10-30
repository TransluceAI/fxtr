# Codebase Reorganization Tracker

Tracking the work to document, test, and clean up the fxtr2 data layer.

## Current Focus
Phase 3 complete! All tests implemented. 216 tests passing.

## Tasks

### Phase 1: Cleanup and Initial Documentation
- [x] Remove "b" node handling from entity_core.py
- [x] Create CODE_OVERVIEW.md (incremental)
  - [x] High-level overview and concepts
  - [x] File-by-file documentation
  - [x] Terminology section
- [x] Create notes/planned_features.md for unimplemented features
- [x] Review and improve docstrings in each file
  - [x] entity_core.py (already well-documented)
  - [x] abstract_arena.py (fixed module docstring)
  - [x] arena_util.py (improved module docstring)
  - [x] entity_serdes.py (improved module docstring with usage example)
  - [x] memory_arena.py (improved module docstring)
  - [x] sqlite_arena.py (has extensive design notes - kept as-is)
  - [x] system_entities.py (improved class docstrings)
- [x] Update CODE_OVERVIEW to reflect codebase state and current knowledge

### Phase 2: Test Infrastructure
- [x] Set up pytest with uv (added pytest-anyio, created conftest.py with fixtures)
- [x] Create test utilities/fixtures if needed (conftest.py has memory_store, memory_arena, active_arena, ground_context, tmp_db_path, tmp_db_url)
- [x] Write test plan for entity_core.py (plans/test_plan_entity_core.md)
- [x] Write test plan for memory_arena.py (plans/test_plan_memory_arena.md)
- [x] Write test plan for sqlite_arena.py (plans/test_plan_sqlite_arena.md)
- [x] Write test plan for entity_serdes.py (plans/test_plan_entity_serdes.md)
- [x] Write test plan for arena_util.py (plans/test_plan_arena_util.md)
- [x] Update CODE_OVERVIEW to reflect codebase state and current knowledge

### Phase 3: Implement Tests
- [x] Unit tests for entity_core.py (55 tests - see plans/test_plan_entity_core.md)
- [x] Unit tests for arena_util.py (26 tests - see plans/test_plan_arena_util.md)
- [x] Integration tests for memory_arena.py (49 tests - see plans/test_plan_memory_arena.md)
- [x] Integration tests for sqlite_arena.py (34 tests - see plans/test_plan_sqlite_arena.md)
- [x] Tests for entity_serdes.py (47 tests - see plans/test_plan_entity_serdes.md)
- [x] Update CODE_OVERVIEW to reflect codebase state and current knowledge

## Questions/Notes

- **Q1** (RESOLVED): Added "value" field to `ContextEntity` (defaults to ".") and renamed `PathRootEntity.root` to `value` to match arena APIs.
