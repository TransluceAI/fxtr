# Plan: Package Entities for TaskBundle

Replace the inline `source_files` + `pylock_toml` fields on `TaskBundle` with a structured `packages` dict of `EntityRef[PySourcePackage | PyWheelPackage]`, so that editable local dependencies become content-addressed bundled entities and remote packages remain lightweight fetch-metadata references.

## New Entities

### PySourcePackage

```python
@dataclasses.dataclass(frozen=True)
class PySourcePackage(DataclassEntity, ...):
    # The distributed source files of the package (from the built wheel,
    # minus .dist-info). Keys are relative paths within site-packages.
    source_files: dict[str, str]
```

Built by: building the package with `uv build`, unpacking the `.whl`, extracting all files except `.dist-info/`.

### PyWheelPackage

```python
@dataclasses.dataclass(frozen=True)
class PyWheelPackage(DataclassEntity, ...):
    name: str          # normalized package name
    version: str
    index: str         # e.g. "https://pypi.org/simple"
    wheel_url: str     # direct URL to the py3-none-any .whl
    wheel_sha256: str  # sha256 hash for verification
```

### Updated TaskBundle

```python
@dataclasses.dataclass(frozen=True)
class TaskBundle(DataclassEntity, ...):
    packages: dict[str, EntityRef[PySourcePackage | PyWheelPackage]]
    entry_point: str        # "module:function"
    requires_python: str    # e.g. ">=3.14"
```

Key is the normalized package name.

## Changes to prepare.py

Current flow: validate pyproject.toml -> `uv lock` -> `uv export --format pylock.toml --no-emit-project` -> collect `.py` files from task dir -> build TaskBundle.

New flow:

1. Validate pyproject.toml (same as today: check entry point, requires-python).
2. `uv lock` (same).
3. `uv export --format pylock.toml --no-dev` (**without** `--no-emit-project`, so the task package itself appears).
4. Parse the pylock.toml and classify each `[[packages]]` entry:
   - **Has `directory` field with `editable = true`**: Build into `PySourcePackage`.
   - **Has `directory` field without `editable = true`**: Raise error ("non-editable local dependencies not yet supported").
   - **Has `index` + `wheels`**: Find the `py3-none-any` wheel, extract metadata into `PyWheelPackage`.
   - **Has `vcs` or `archive`**: Raise error ("VCS/archive dependencies not yet supported").
   - **No distribution info at all** (shouldn't happen after removing --no-emit-project, but guard): Raise error.
5. For the task package itself: it will appear in the pylock as a `directory` entry (editable, path = `.`). Build it the same way as any other editable dep.
6. Return `TaskBundle` with the `packages` dict. Caller stores it and the referenced entities.

### Building a PySourcePackage from an editable dep

Given a `directory.path` from the pylock entry (resolved relative to the task dir):

1. Run `uv build --wheel -o <tempdir>` in that directory to produce a `.whl` file in a temporary directory (not `dist/` inside the package).
2. Unzip the wheel.
3. Walk the extracted contents, skipping any `*.dist-info/` directory.
4. Collect remaining files into `source_files: dict[str, str]` (relative path -> content).
5. Return `PySourcePackage(source_files=...)`.

Note: we read files as text (str). If we encounter binary files we should raise an error for now — pure Python packages shouldn't have any.

### prepare() return type

`prepare()` stores each package entity into the active fxtr2 scope via `.store()`, which returns an `EntityRef`. These refs are placed directly into the `TaskBundle.packages` dict. The bundle itself is also `.store()`d, and `prepare()` returns the `EntityRef[TaskBundle]`.

```python
def prepare(task_dir: Path) -> EntityRef[TaskBundle]:
```

The caller just needs an active `FxtrScope`. All entities (packages + bundle) are stored into it during `prepare()`.

## Changes to wheels.py

Rename/refactor to work with `PyWheelPackage` entities instead of parsing pylock.toml.

```python
def fetch_wheels(packages: Iterable[PyWheelPackage], cache_dir: Path) -> list[Path]:
    """Download and cache wheels for remote packages."""
```

Each `PyWheelPackage` has `wheel_url` and `wheel_sha256` directly — no need to parse pylock or find compatible wheels. The filtering to py3-none-any already happened at prepare time.

## Changes to runner.py

Current flow: fetch wheels -> create SandboxFactory with wheel paths -> write source files to temp dir -> mount as volume at `/task` -> execute _runner.py which adds `/task` to sys.path.

New flow:

1. Separate `bundle.packages` into `PySourcePackage`s and `PyWheelPackage`s (resolve the EntityRefs).
2. Fetch wheels for `PyWheelPackage`s (same as before but using new API).
3. Write all `PySourcePackage` contents into a single temp `site_packages/` directory (combine all source_files dicts).
4. Create `SandboxFactory`:
   - `packages=[...wheel_paths...]` for remote wheels.
   - `site_packages=site_packages_dir` for the combined local source.

   Try using both `packages` (for wheel paths) and `site_packages` (for local source) together on SandboxFactory. If that doesn't work, fall back to combining everything into one site_packages dir ourselves (unzip wheels + write source files together).

5. No volume mount needed for task source (it's in site_packages now).
6. The _runner.py script no longer needs to manipulate sys.path — imports just work.

### Updated _runner.py

Simplifies to:
```python
import importlib
# _ENTRY_POINT set by host
class TaskContext: ...  # same as before
_module_name, _func_name = _ENTRY_POINT.split(":", 1)
_module = importlib.import_module(_module_name)
_entry_fn = getattr(_module, _func_name)
await _entry_fn(TaskContext())
```

No `sys.path` manipulation needed.

## Changes to tests

All three test files will need updates:
- **test_prepare.py**: Update for new return type, verify packages dict contains correct PySourcePackage/PyWheelPackage entities. Add test for editable deps. Add test for non-editable local dep error.
- **test_wheels.py**: Update to work with PyWheelPackage entities instead of pylock.toml strings.
- **test_runner.py**: Update for new TaskBundle shape. Verify site_packages approach works.

## Changes to examples

- `hello_task` and `yaml_task` should still work, but the prepare flow changes.
- Consider adding an example with an editable local dep to demonstrate the new capability (e.g., a shared lib that the task depends on).

## Implementation steps

- [x] 1. Add `PySourcePackage` and `PyWheelPackage` entities to `entities.py`, update `TaskBundle`.
- [x] 2. Update `prepare.py`: parse pylock.toml packages, classify into source/wheel, build source packages from editable deps, construct new TaskBundle with EntityRefs.
- [x] 3. Update `wheels.py`: accept `PyWheelPackage` entities instead of pylock.toml string.
- [x] 4. Update `runner.py`: combine source packages into site_packages dir, extract wheels there too, use `SandboxFactory(site_packages=...)`, simplify _runner.py.
- [x] 5. Update `_runner.py`: remove sys.path manipulation.
- [x] 6. Update tests.
- [x] 7. Update examples (added shared_lib + lib_task editable dep example).
- [x] 8. Update CODE_OVERVIEW.md.
