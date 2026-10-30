# Plan: Optimize git_store Path Resolution

## Context

The `git_store` module resolves file paths repeatedly on every pin/unpin/retrieve operation. Each operation triggers multiple `Path.resolve()` syscalls (realpath) and many `Path.__truediv__` allocations. The fix: resolve the store root once at construction time, then use fast string operations for all derived paths internally. Also update the shared `lock.py` and `tsv_file.py` utilities to accept `str` so callers don't need to wrap in `Path()` at every call site.

## Implementation Steps

- [x] Step 1: `lock.py` — accept `str | Path`, fast path for strings
- [x] Step 2: `tsv_file.py` — accept `str | Path`
- [x] Step 3: `key_utils.py` — return strings from `parse_pin_key` / `make_full_key`
- [x] Step 4: `config.py` — add `store_root_str` to `GitStoreConfig`
- [x] Step 5: `dirty.py` — switch to string paths
- [x] Step 6: `sync.py` — string paths for all functions
- [x] Step 7: `git_namespace.py` — string joins in public methods
- [x] Step 8: `git_store_cli.py` and `__init__.py`
- [x] Step 9: Full test suite + docs
