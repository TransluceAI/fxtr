# Stage 2 Post-1: Refactor fxtr2/ui into thingwatcher/fxtr

## Overview

This plan refactors the fxtr-thingwatcher integration code from `fxtr2/ui/` to `thingwatcher/fxtr/`. Instead of fxtr2 optionally depending on thingwatcher, thingwatcher will unconditionally depend on fxtr2.

**Rationale:** The integration code is about visualizing fxtr entities in thingwatcher's UI. It logically belongs with the visualization layer (thingwatcher) rather than the data layer (fxtr2).

## Design Decisions

Based on discussion:
- **Module path**: `thingwatcher/fxtr/` - short and matches the product name
- **Tests**: Move to `extras/thingwatcher/tests/test_fxtr/`
- **Backwards compatibility**: No stub needed in fxtr2 (code hasn't been released)
- **Demo name**: `extras/thingwatcher/examples/demo_fxtr/`

## Changes Summary

### In thingwatcher (`extras/thingwatcher/`)

1. Add fxtr2 as an unconditional dependency
2. Create `thingwatcher/fxtr/` module with all integration code
3. Create `tests/test_fxtr/` with moved tests
4. Create `examples/demo_fxtr/` with the demo
5. Update CODE_OVERVIEW.md

### In fxtr2 (`v2/`)

1. Delete `fxtr2/ui/` entirely
2. Remove `[ui]` optional dependency from pyproject.toml
3. Delete `tests/ui/`
4. Delete `examples/demo_basic_ui/`
5. Update CODE_OVERVIEW.md to remove ui module references

## New Module Structure

```
extras/thingwatcher/
├── thingwatcher/
│   ├── fxtr/                    # NEW: fxtr integration module
│   │   ├── __init__.py          # Re-exports public API
│   │   ├── cbor_transform.py    # Transform fxtr entities to ThingStates
│   │   ├── immutable_store.py   # ImmutableStore for fxtr entities
│   │   ├── snapshotter.py       # Namespace snapshotter/anchor capturer
│   │   ├── namespace_view.py    # Main context manager
│   │   ├── renderers.py         # Renderer registration for fxtr types
│   │   ├── components/          # TSX renderer components
│   │   │   ├── deno.json
│   │   │   ├── FxtrEntityFallback.tsx
│   │   │   └── FxtrNamespace.tsx
│   │   └── LOCAL_CODE_OVERVIEW.md
│   └── ... (existing modules)
├── tests/
│   ├── test_fxtr/               # NEW: fxtr integration tests
│   │   ├── __init__.py
│   │   ├── conftest.py          # Fixtures for fxtr tests
│   │   ├── test_cbor_transform.py
│   │   ├── test_immutable_store.py
│   │   ├── test_namespace_view.py
│   │   └── test_renderers.py
│   └── ... (existing tests)
├── examples/
│   ├── demo_fxtr/               # NEW: fxtr demo
│   │   └── demo.py
│   └── ... (existing demos)
└── pyproject.toml               # Add fxtr2 dependency
```

## Import Changes

The moved code needs import path updates:

### Before (in fxtr2/ui/)
```python
from fxtr2.data.entity_core import FxtrID, ...
from thingwatcher import ...
```

### After (in thingwatcher/fxtr/)
```python
from fxtr2.data.entity_core import FxtrID, ...  # Same - fxtr2 now a dependency
from thingwatcher import ...                    # Now internal import, could be relative
```

**Note:** Since the imports from fxtr2.data stay the same, the main changes are:
- Update any `from fxtr2.ui.xxx` imports to `from thingwatcher.fxtr.xxx`
- Optionally use relative imports within the fxtr module

## API Changes

### Old API (fxtr2)
```python
from fxtr2.ui import fxtr_namespace_view, register_fxtr_renderer
```

### New API (thingwatcher)
```python
from thingwatcher.fxtr import fxtr_namespace_view, register_fxtr_renderer
```

Also re-export from the main thingwatcher `__init__.py` for convenience:
```python
# In thingwatcher/__init__.py
from thingwatcher.fxtr import fxtr_namespace_view, register_fxtr_renderer
```

## Implementation Steps

### Phase 1: Set up thingwatcher infrastructure

- [x] 1. Add fxtr2 dependency to `extras/thingwatcher/pyproject.toml`
- [x] 2. Create `thingwatcher/fxtr/` directory structure
- [x] 3. Copy files from `fxtr2/ui/` to `thingwatcher/fxtr/`:
  - [x] `__init__.py`
  - [x] `cbor_transform.py`
  - [x] `immutable_store.py`
  - [x] `snapshotter.py`
  - [x] `namespace_view.py`
  - [x] `renderers.py`
  - [x] `components/` directory (with deno.json, TSX files)
  - [x] `LOCAL_CODE_OVERVIEW.md`
- [x] 4. Update imports in copied files (any relative imports, thingwatcher internal imports)
- [x] 5. Update `thingwatcher/__init__.py` to re-export fxtr public API

### Phase 2: Move tests

- [x] 6. Create `tests/test_fxtr/` directory
- [x] 7. Copy test files from `v2/tests/ui/` to `tests/test_fxtr/`:
  - [x] `__init__.py`
  - [x] `test_cbor_transform.py`
  - [x] `test_immutable_store.py`
  - [x] `test_namespace_view.py`
  - [x] `test_renderers.py`
- [x] 8. Update imports in test files (`from fxtr2.ui` -> `from thingwatcher.fxtr`)
- [x] 9. Create `tests/test_fxtr/conftest.py` if needed (may need fixtures from fxtr2)
- [x] 10. Run tests to verify they pass

### Phase 3: Move demo

- [x] 11. Create `examples/demo_fxtr/` directory
- [x] 12. Copy demo from `v2/examples/demo_basic_ui/demo.py`
- [x] 13. Update imports in demo (`from fxtr2.ui` -> `from thingwatcher.fxtr`)
- [x] 14. Test the demo works

### Phase 4: Clean up fxtr2

- [x] 15. Delete `fxtr2/ui/` directory entirely
- [x] 16. Remove `[ui]` optional dependency from `v2/pyproject.toml`
- [x] 17. Delete `v2/tests/ui/` directory
- [x] 18. Delete `v2/examples/demo_basic_ui/` directory

### Phase 5: Update documentation

- [x] 19. Update `extras/thingwatcher/CODE_OVERVIEW.md`:
  - [x] Add fxtr/ module to directory structure
  - [x] Add fxtr module documentation section
  - [x] Update test count
- [x] 20. Update `v2/CODE_OVERVIEW.md`:
  - [x] Remove ui/ from directory structure
  - [x] Remove ui module documentation section
  - [x] Update references to point to thingwatcher
  - [x] Update test count
- [x] 21. Update `thingwatcher/fxtr/LOCAL_CODE_OVERVIEW.md`:
  - [x] Update paths and import examples
  - [x] Update references to "this module"

## Test Plan

After implementation, verify:

1. **Unit tests pass**: `uv run pytest` in both `extras/thingwatcher/` and `v2/`
2. **Demo works**: Run `uv run python examples/demo_fxtr/demo.py` from thingwatcher
3. **Imports work**: Verify both import styles work:
   ```python
   from thingwatcher.fxtr import fxtr_namespace_view
   from thingwatcher import fxtr_namespace_view  # If re-exported
   ```

## Open Questions / Future Work

- **Cross-package testing**: The tests require both thingwatcher and fxtr2 to be installed. This is fine since thingwatcher now depends on fxtr2.
- **Version compatibility**: If fxtr2 makes breaking changes, thingwatcher.fxtr may need updates. Consider version pinning if this becomes an issue.
