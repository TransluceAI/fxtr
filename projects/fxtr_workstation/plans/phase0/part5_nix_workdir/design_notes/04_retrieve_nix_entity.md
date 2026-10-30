# `retrieve_nix_entity` semantics

The sandbox-side builtin that pulls a nix-declared **DATA entity**
(`fxtr.entities.<name>`) into a running script. Mirrors the existing
`lookup_entity` / `retrieve_result` shape.

> **Revised (2026-05-30):** `prepare_package_bundle` is still removed
> (Stage 4), but `retrieve_nix_entity` is no longer how a service
> bundle reaches the spawner — services are git-referenced code, not
> entities (Areas 05/12). This builtin is now purely a DATA affordance.
> It resolves against the bare workdir path (auto-git/`path:`,
> non-frozen — uncommitted edits to tracked files count); code resolves
> by git commit elsewhere.

## Signature

```python
def retrieve_nix_entity(name: str, result_type=None, nix_location="fxtr.entities") -> Any: ...
```

- `name`: the dotted path under `fxtr.entities`. For v0 we
  only resolve top-level entries (`name="foo"` → `entities.foo`).
  Whether to allow `name="some.sub.path"` (for nested manifest
  organization) is open — see Q4.A.
- `result_type`: same as `lookup_entity`'s — either `None` (return
  raw `EntityRef[Any]`), a concrete type / `_ResultTypeSentinel` to
  drive mashumaro decode, or any custom annotation.
- `nix_location`: where to find the entity. Defaults to "fxtr.entities",
  the generic top-level DATA collection — and for v0 that's the **only**
  whitelisted location. (Presets and services are no longer entities, so
  `fxtr.workstation.uiPresets` is off the whitelist; the parameter stays
  so future DATA sub-collections can be added.)

## Lifetime

**Per-script-call readers**, not per-cycle. Each `retrieve_nix_entity`
call:

1. Re-reads the cached structure-of-everything (from the cheap nix
   eval done at script start — see "Per-script flow" below).
2. Looks up the entity name; raises `LookupError` if absent.
3. Runs `nix build --json .#fxtr.entities.<name>.layer`
   to materialize the layer's store path. (Idempotent + fast if
   already built.)
4. Opens a `LayeredFileStoreReader` against the store path,
   resolves `layerKey` to an FxtrID, bridges that ID into
   `script_scope`, closes the reader.

The reader is opened *per call*, not cached across calls. Layered
file store reads are lightweight fs operations; the simplicity of
"open / read / close per call" outweighs any caching gain. If the
agent retrieves twice in one script, that's two opens; we accept it.

`script_scope` is the natural destination because the per-script
lifecycle already exists in `host_impl.py`. Once bridged in, the
returned ref behaves like any other entity ref — `export_entity`,
passing-as-invoke-arg, etc. all work without special-casing.

## Per-script flow (lazy; no build trigger)

> **Revised (2026-05-29):** `run_script` has no nix preflight (the
> workstation sandbox is host-built — see `../stage2_sandbox_runtime.md`
> "Revised direction"). Nix runs lazily, *inside* `retrieve_nix_entity`.

On the **first** `retrieve_nix_entity` call within a `run_script`
(before resolving that entity):

1. The MCP runs `nix eval --json .#fxtr` (with the `--apply` filter)
   to build its in-memory map of `{entity_name → (layerKey,
   layer-attr-path)}`. Friendly error here if there's no `flake.nix`.
   The map can be cached for the rest of the script's lifetime
   (subsequent retrievals reuse it).
2. For the named entity, the MCP builds
   `nix build .#fxtr.entities.<name>.layer` and opens a reader over
   the result.

Scripts that never call `retrieve_nix_entity` never invoke nix at
all. The "lots of declared entities, few actually used" case stays
fast: only the touched layers build.

## Resolution form (bare path / auto-git) + untracked-files notice

> **Revised (2026-05-30):** the MCP resolves the workdir by handing nix
> the **bare path with no `path:` prefix** (Area 01), so nix uses git-
> tree semantics inside a repo and `path:` semantics otherwise — the
> same fetcher as the code path, but **non-frozen**. Uncommitted
> *modifications to tracked files* are picked up (no commit needed for
> the data loop); **new untracked files are invisible** until `git add`.

To keep that invisibility from being silent, the MCP surfaces a short
**notice** on `retrieve_nix_entity` when the workdir is a git repo and
`git ls-files --others --exclude-standard` reports any untracked files —
just the **count**, not the list (the agent can `git status` if it
cares; listing every file would be noisy):

```
note: N untracked file(s) are not visible to nix (it builds from
tracked files only) — `git add` them if an entity depends on them.
```

It's a non-fatal notice (appended to the result / script output, not an
exception). Gitignored files are excluded (intentional, no noise);
tracked-file edits need no notice (they're seen). No notice when the
workdir isn't a git repo (the `path:` fallback sees everything).

## Error surface (resolved)

These failures are raised *at the `retrieve_nix_entity` call site*
(there is no top-of-`run_script` failure path anymore):

- **No `flake.nix`** — friendly `NixEntityBuildFailed`-style error
  saying nix-backed entities are unavailable without a flake.
- **Eval failure** (`flake.nix` doesn't evaluate): the MCP can't
  enumerate entities; the call raises with "flake eval failed:
  <stderr>".
- **Layer build failure**: the call raises `NixEntityBuildFailed`
  with the build stderr (see `11_failure_and_ux.md`).
- **Entity lookup or build failure** (step 3, inside the script):
  surfaces as an exception raised at the `retrieve_nix_entity` call
  site:
  - Unknown name → `LookupError(name)` (caught by the eval-time
    structure map).
  - Build failure → a custom exception (e.g.
    `NixEntityBuildFailed`) whose message includes the nix stderr.
    The exception goes through the same `_format_script_error`
    compression as any other script exception; the full text is
    accessible via `get_full_exception(exc_id)`.

## Open questions

- Q4.A: Allow nested name paths (`retrieve_nix_entity("foo.bar")`
  → `entities.foo.bar`)? Useful for namespacing in big flakes, but
  the flake author can already namespace via Nix attrs and pass
  whatever name the flake exposes. **Leaning no** for v0.
- Q4.B: Should `retrieve_nix_entity` also be exposed as a
  non-sandbox MCP tool (so the agent can grab an entity from
  outside a script)? Probably not — everything funnels through
  `run_script` already. => Decision: no.
- Q4.C: For very large layers, we may want a "load only the keys
  you need" mode rather than opening a reader over the whole layer
  dir. The layered-file-store reader is already lazy on entity-data
  loads, but the reader itself walks the parent chain at open
  time. Probably fine for v0.
- Q4.D: What does `present_to_user` do with an entity ref returned
  by `retrieve_nix_entity`? Same as any ref — bridged into the log
  via the normal substitution walker. No special case.
