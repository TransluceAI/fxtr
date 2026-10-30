# Package Entities - Scratch Notes

## How to create EntityRef from a DataclassEntity

Need to figure out:
- How does a DataclassEntity get its ID? (content-addressed, so computed from fields)
- How to construct an EntityRef pointing to it without a store?

From the codebase: `EntityRef(entity_id=..., target_type=...)`. The entity_id is `entity.fxtr_id` (or however the DataclassEntity exposes its content-addressed ID). target_type is the class (or union type).

## SandboxFactory: site_packages + packages interaction

The eryx API has both `site_packages` (a directory) and `packages` (list of wheel paths). From the Rust source, `packages` are extracted to a temp dir and then treated similarly to site_packages. Using both might merge them or might conflict.

Safest approach: do everything ourselves. Extract wheels + write source files into a single temp site_packages dir. Pass only `site_packages` to SandboxFactory. This avoids any interaction issues and gives us full control.

## pylock.toml directory entry format

From packaging.pylock:
```python
class PackageDirectory:
    path: str
    editable: bool  # optional, defaults to False
    subdirectory: str  # optional
```

Detection logic:
- `pkg.directory is not None` → local package
- `pkg.directory.editable == True` → editable, build into PySourcePackage
- `pkg.directory is not None and not pkg.directory.editable` → error

## uv build output

`uv build --wheel` in a directory produces a .whl in `dist/`. We need to:
1. Run `uv build --wheel`
2. Find the .whl in dist/
3. Unzip it
4. Walk contents, skip .dist-info/

## What about the task package's own pylock entry?

When we remove `--no-emit-project`, the task package itself appears in the pylock.toml. It should appear as a directory entry with editable=true (since it's the project itself). We treat it the same as any other editable dep.

Wait — does uv export actually emit the project as a directory entry? It might just emit it as a regular package with no distribution info. Need to test this.

Actually, looking at `--no-emit-project` vs `--no-emit-package`:
- `--no-emit-project` omits the project entirely
- Without it, the project is included

The project in pylock would likely appear as a directory with path=".". We should test this.

## Binary files in wheels

Pure Python wheels shouldn't have binary files, but they might have data files. For now, we read everything as text and error on binary. This is fine for the spike.
