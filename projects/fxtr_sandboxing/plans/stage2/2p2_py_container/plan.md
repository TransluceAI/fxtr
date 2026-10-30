# Stage 2p2 — `PyContainerTaskBundle` (py-bundle in a container)

A third sandbox mode that sits between the existing two:

| Mode | Bundle shape | Runtime | Wheel constraint | Image setup | Multi-arch |
| --- | --- | --- | --- | --- | --- |
| WASM (`TaskBundle`) | source pkgs + single-wheel pkgs, materialized site-packages | eryx CPython 3.14 in WASM | `py3-none-any` only | none — eryx is embedded | n/a (one VM) |
| **py_container (this plan)** | source pkgs + multi-wheel pkgs + base-image ref | Linux CPython in container | per-arch linux wheels (manylinux, etc.) | base image from whitelist; *no per-task Dockerfile or registry push* | **yes — one bundle, multiple arches** |
| Container (`DockerTaskBundle`) | OCI image digest + source mounts | Linux CPython in container | anything baked into the image | per-task Dockerfile + image build + registry push | bundle-time choice (per-image) |

The new mode keeps the prepare-from-pyproject ergonomics of WASM (no Dockerfile,
no `rebuild.sh`, no registry push per task) while gaining a real Linux
interpreter — so tasks can depend on packages like `numpy` / `pillow` /
`pydantic-core` that ship platform-specific wheels. The bundle itself stays
arch-agnostic at the type level: one `PyContainerTaskBundle` ref runs on any
of the arches the user declared.

## High-level shape

**Two key insights:**

1. **The runtime rides in the bundle, same as WASM.** The user's
   `pyproject.toml` declares `fxtr-sandbox-rpc-adapter[fxtr2-portable, http]`
   and `fxtr-service-runner` as direct deps; `uv export` resolves the full
   closure (user code + user deps + fxtr runtime + runtime's transitive deps)
   into the bundle; the materializer extracts everything into one flat
   tempdir per spawn. The container just bind-mounts that one dir and runs
   `python -m fxtr_service_runner <entry_point>` against it. No host-side
   site-packages spelunking, no custom blessed base image.

2. **One bundle works cross-arch.** Each `PyMultiWheelPackage` in the
   bundle carries a tuple of per-arch wheels (extracted from a single
   universal `uv lock`). The bundle's `base_image` points at a manifest-
   list digest (an OCI image index), which the container runtime
   resolves to a per-arch image at pull time. The spawner picks the env's
   target arch, passes `--platform <arch>` to `docker run`, and the
   materializer filters each `PyMultiWheelPackage` to the matching
   wheel. One content-addressed bundle, any supported arch.

```
[prepare-time]
    pyproject.toml
      ├─ dependencies = [
      │     "fxtr-sandbox-rpc-adapter[fxtr2-portable, http]",
      │     "fxtr-service-runner",
      │     <user's deps>,
      │  ]
      ├─ [tool.fxtr.sandboxing]
      │     base_image = "py312"
      │     target_arches = ["linux/amd64", "linux/arm64"]
      └─ [tool.uv]
           required-environments = [
               "sys_platform == 'linux' and platform_machine == 'x86_64'",
               "sys_platform == 'linux' and platform_machine == 'aarch64'",
           ]
        │
        │  prepare_py_container_task(task_dir)
        │   - reads only [tool.fxtr.sandboxing]; uv config is the
        │     user's responsibility
        │   - one `uv lock` against pyproject directly
        │   - one `uv export --format pylock.toml`
        │   - per-arch wheel filtering builds PyMultiWheelPackages;
        │     error with clear "did you set required-environments?"
        │     message if any target arch has no compatible wheel
        ▼
    PyContainerTaskBundle entity
        ├ base_image: EntityRef[DockerImage]
        │    └ digest of the multi-arch manifest list
        ├ target_arches: dict[str, ArchSpec]
        │    └ "linux/amd64" -> tags, "linux/arm64" -> tags
        ├ packages: dict[str, EntityRef[PySourcePackage | PyMultiWheelPackage]]
        ├ requires_python: str
        └ entry_point: str
      (one content-addressed bundle ref, runs on any supported arch)

[spawn-time]
    spawner.spawn(bundle_ref)        # env picks target_arch (auto or explicit)
        │   loaded -> PyContainerTaskBundle
        │   check: target_arch in bundle.target_arches
        ▼
    _spawn_runner_py_container(bundle, target_arch=...)
        │  - load DockerImage from bundle.base_image
        │  - build_site_packages_for_py_container_bundle(
        │       bundle, cache_dir, target_arch=...)
        │    -> flat tempdir with the matching per-arch wheels
        │  - open task-socket UDS + transport-socket UDS
        │  - launch:
        │       container run --rm --network none \
        │           --platform <target_arch> \
        │           -v <site-packages>:/app/site-packages:ro \
        │           -v <task.sock>:/run/fxtr/task.sock \
        │           [-v <transport.sock>:/run/fxtr/transport.sock] \
        │           -e PYTHONPATH=/app/site-packages \
        │           <manifest-list-digest> \
        │           python -m fxtr_service_runner <entry_point>
        │     # runtime resolves manifest list -> per-arch image
        ▼
    SocketStream -> SocketJSONStream -> open_rpc_scope (identical to container path)
```

Once the SocketStream is up, the spawner / RPC / transport / nested-spawn
plumbing is byte-for-byte the same as the existing container mode.

## Files added / changed

```
fxtr_sandbox_types/
  entities.py                      # + PyContainerTaskBundle entity
                                   #   (PySourcePackage / PyWheelPackage stay
                                   #    as-is; same entities reused)
  __init__.py                      # re-export PyContainerTaskBundle

fxtr_sandboxing/
  base_images.py            (NEW)  # BaseImageSpec + ArchSpec dataclasses;
                                   #   DEFAULT_BASE_IMAGES constants;
                                   #   whitelist resolution helpers.
  py_container_prepare.py   (NEW)  # prepare_py_container_task(task_dir)
  py_container_runner.py    (NEW)  # _run_py_container_internal CM
                                   #   (copy-then-refactor from container_runner.py)
  spawner.py                       # third dispatch arm: PyContainerTaskBundle
                                   #   -> _spawn_runner_py_container.
                                   # create_sandbox_environment grows a
                                   #   target_arch kwarg (None -> auto-detect).
  prepare.py                       # extract shared helpers used by both
                                   #   prepare paths (no behavior change here)
  runner.py                        # relocate `build_site_packages_for_bundle`
                                   #   out of the eryx-specific module — it's
                                   #   shared with py_container now. Either
                                   #   move to a new `_bundle_materialize.py`
                                   #   or just re-export from there. Decide
                                   #   during step 4. Signature gains a
                                   #   `target_arch` kwarg used to pick wheels
                                   #   from PyMultiWheelPackage entities.

fxtr_sandbox_types/entities.py     # New entities:
                                   #   PyWheelArtifact (inline DataclassDictMixin)
                                   #   PyMultiWheelPackage (DataclassEntity)
                                   #   PyContainerTaskBundle (DataclassEntity)
                                   # Tightened DockerImage docstring
                                   #   (single-arch vs manifest-list digest
                                   #   guidance).

examples/
  py_container_rpc_task/    (NEW)  # demo. pyproject.toml + my_rpc_task/__init__.py
                                   #   mirroring rpc_task / container_rpc_task,
                                   #   plus a `numpy_version()` method to
                                   #   exercise the "actually has linux wheels"
                                   #   point.

tests/
  test_py_container_prepare.py  (NEW)  # prepare unit tests
  test_py_container_spawner.py  (NEW)  # end-to-end spawn -> connect against base image
  test_py_container_transport.py (NEW) # one transport test confirming stage-3 works
```

A later refactor step (step 9) pulls common bits out of `container_runner.py`
+ `py_container_runner.py` into `_container_common.py`. Until then, the second
runner carries `# TODO(2p2 step 9): share with container_runner` markers next
to the duplicated blocks.

## Implementation order

The plan steps are numbered in dependency order, but for *testing* during
implementation it's easier to interleave them. Suggested concrete order:

1. **Step 1** — entities + whitelist constants. No I/O; runnable in isolation.
2. **Step 6 (skeleton)** — minimal `examples/py_container_rpc_task/` (just
   `pyproject.toml` + an empty `rpc_task/__init__.py`). Needed as a target for
   prepare. The full impl comes later in step 6.
3. **Step 2** — `prepare_py_container_task`. Test it against the
   step-6-skeleton demo + assertions on the resulting bundle entity.
4. **Step 4** — relocate + generalize `build_site_packages_for_bundle`.
   Test by materializing the bundle from step 2 and asserting the
   tempdir contains the right per-arch wheels.
5. **Step 5** — `_run_py_container_internal`. Test it standalone first
   (no spawner) by invoking with a manually-loaded bundle.
6. **Steps 3 + 8** — spawner dispatch arm + env-level `target_arch`.
   Now end-to-end spawn → connect should work.
7. **Step 6 (full)** — finish the demo: `GreeterProto`, `_Impl`,
   `markupsafe_version()`, etc.
8. **Step 7** — end-to-end + transport tests.
9. **Step 9** — refactor: extract shared scaffolding to
   `_container_common.py`, remove TODO markers.
10. **Step 10** — final CODE_OVERVIEW.md pass.

This sequence lets each step be tested before the next is written, instead
of stockpiling untested code through steps 1–5 and only validating at step 7.

## Steps

### 1. New entities + base-image whitelist constants

Multi-arch bundles are made possible by **three** new types in
`fxtr_sandbox_types/entities.py`: an inline artifact struct, a multi-wheel
package entity, and the bundle itself. The existing `PyWheelPackage`
(used by WASM) is untouched — single-wheel packages stay single-wheel.

- [x] Add `PyWheelArtifact` (inline, **not** a content-addressed entity)
  to `fxtr_sandbox_types/entities.py`:
  ```python
  @dataclasses.dataclass(frozen=True)
  class PyWheelArtifact(DataclassDictMixin):
      """One specific wheel file. Inline-stored as part of
      PyMultiWheelPackage — not a standalone entity (a wheel hash on
      its own is not interesting context).

      The three fields mirror what `packaging.pylock.PackageWheel`
      surfaces: the wheel filename (`name`), the download URL, and the
      sha256 hash. The wheel filename is canonical per PEP 427 — it
      carries the (python, abi, platform) tag positions. Tag
      extraction happens at use time via
      `packaging.utils.parse_wheel_filename(name)`; we don't pre-parse
      tags because the lockfile doesn't surface them as structured
      fields and parsing once at pick time is cheap. Storing `name`
      explicitly (rather than parsing tags from the URL) is the
      reason we don't need the URL to be a canonical wheel-filename
      path — mirror URLs / VCS URLs / etc. are all fine.
      """
      name: str       # canonical wheel filename, e.g. "markupsafe-2.1.5-cp312-cp312-manylinux_2_17_x86_64.whl"
      url: str
      sha256: str
  ```
- [x] Add `PyMultiWheelPackage` (entity) to the same file:
  ```python
  @dataclasses.dataclass(frozen=True)
  class PyMultiWheelPackage(
      DataclassEntity,
      fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "py_multi_wheel_package"),
  ):
      """A remotely-published package, with wheels for multiple
      (python, abi, platform) targets.

      Sibling of PyWheelPackage. WASM bundles keep using PyWheelPackage
      (always exactly one wheel per package). py_container bundles use
      PyMultiWheelPackage so a single bundle can carry wheels for
      several arches; the spawn-time materializer picks the wheel
      matching the env's target arch.
      """
      name: str
      version: str
      index: str
      wheels: tuple[PyWheelArtifact, ...]
  ```
  `tuple[...]` (frozen) keeps the entity hashable/comparable cleanly.
- [x] Add `ArchSpec` (inline, **not** an entity) to the same file —
  describes one supported arch's wheel-resolution tags. It lives in
  `fxtr_sandbox_types` because the bundle's contents depend on it
  (rather than in `fxtr_sandboxing`, which is host-side only).
  ```python
  @dataclasses.dataclass(frozen=True)
  class ArchSpec(DataclassDictMixin):
      """The per-arch wheel-matching tags. Inline on PyContainerTaskBundle
      so the bundle is fully self-describing at spawn time — the
      materializer needs no host-side config to pick wheels."""
      python_tag: str          # "cp312"
      abi_tag: str             # "cp312"
      platform_tag: str        # "manylinux_2_17_x86_64"
  ```
  Note: the host-side `uv_python_platform` field (used at prepare time
  to invoke uv) stays on the *host-side* `BaseImageSpec` in
  `fxtr_sandboxing/base_images.py`. It doesn't belong on the bundle.
- [x] Add `PyContainerTaskBundle` (entity) to the same file:
  ```python
  @dataclasses.dataclass(frozen=True)
  class PyContainerTaskBundle(
      DataclassEntity,
      fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "py_container_task_bundle"),
  ):
      """Bundle for the py_container mode. Multi-arch by construction:
      one bundle can run on any of the arches in target_arches."""
      base_image: EntityRef[DockerImage]              # typically a manifest-list digest
      target_arches: dict[str, ArchSpec]              # "linux/amd64" -> ArchSpec, ...
      packages: dict[str, EntityRef[PySourcePackage | PyMultiWheelPackage]]
      requires_python: str
      entry_point: str
  ```
  Notes on the fields:
  - `base_image` is an `EntityRef[DockerImage]`. In the multi-arch case
    its digest is a **manifest-list digest** (an OCI image index): the
    container runtime selects the right per-arch image at pull time.
    Bundles that pin only one arch can still use a single-arch image
    manifest digest if the caller prefers — the runtime accepts both.
  - `target_arches` is keyed by container-platform string
    (`"linux/amd64"` etc.) and carries the wheel-resolution tags inline.
    Spawn-time looks up `bundle.target_arches[env.target_arch]` to get
    the tags used to pick wheels from each `PyMultiWheelPackage`. The
    bundle is therefore self-describing: the spawner consults nothing
    else.
- [x] **Loosen** the existing `DockerImage` docstring (do not tighten as
  the previous plan version proposed) and add a one-liner in
  `container_runner.py` noting that the existing path does *not* pass
  `--platform`, so a manifest-list digest there will fall back to
  host-arch resolution — usually fine, but worth knowing. The digest
  may be either a single-arch image manifest digest or a manifest list
  / OCI image index digest — the choice depends on intent:
  ```python
  class DockerImage(...):
      """Reference to an OCI container image by registry and digest.

      The full OCI reference is `registry@digest`, e.g.
      `localhost:5000/my-task@sha256:abc123...`.

      The `digest` field may be either:

      - the digest of a **single-arch image manifest** — pins one
        specific image, regardless of host arch. Use this when the
        bundle's contents are arch-specific and you want spawn-time
        behavior to fail loudly on arch mismatch rather than silently
        pull a different arch. The existing DockerTaskBundle flow
        produces these by default (buildkit builds single-arch images
        unless told otherwise).
      - the digest of a **manifest list / OCI image index** — lets the
        container runtime pick the per-arch image at pull time, based
        on its --platform flag (or host arch as fallback). Use this
        when the *caller* (i.e. the spawner) chooses the arch and the
        bundle's other contents adapt accordingly. PyContainerTaskBundle
        uses this so one bundle works across multiple arches.

      Both forms are valid OCI references; the digest's *manifest media
      type* on the registry distinguishes them.
      """
  ```
- [x] Re-export new entities from `fxtr_sandbox_types/__init__.py`.
- [x] Add `fxtr_sandboxing/base_images.py`:
  ```python
  from fxtr_sandbox_types import ArchSpec  # bundle-side struct (tag triple)

  @dataclasses.dataclass(frozen=True)
  class BaseImageSpec:
      """One whitelist entry, multi-arch by construction. Consulted by
      prepare_py_container_task to translate the user's pyproject key
      into a DockerImage entity + per-arch wheel-matching tags. Not
      consulted at spawn time."""
      registry: str                       # "docker.io/library/python"
      digest: str                         # manifest-list digest (OCI image index)
      python_version: str                 # "3.12"
      arches: dict[str, ArchSpec]         # "linux/amd64" -> ArchSpec, ...
  ```
  Same shape as the bundle's `target_arches: dict[str, ArchSpec]` —
  the whitelist's per-arch payload is just the bundle's per-arch
  payload. No `HostArchInfo` / `pep508_marker` wrapping needed,
  because we don't translate between the user's PEP 508 markers and
  our arches at all (see step 2 — uv config is the user's
  responsibility).
  Plus `DEFAULT_BASE_IMAGES: dict[str, BaseImageSpec]` with at least:
  - `"py312"` (Python 3.12 slim, manifest-list digest from Docker Hub,
    arches = `{"linux/amd64": ..., "linux/arm64": ...}`).
  - `"py313"` (same, 3.13).
- [x] Each spec's `digest` is the **manifest-list digest** for the
  multi-arch tag. To seed:
  ```sh
  # The top-level digest of the multi-arch manifest list. For Docker Hub
  # tags like python:3.12-slim, `docker manifest inspect --verbose` shows
  # the list itself; the digest of the manifest list (one rung above
  # the per-arch image manifests) is what we want.
  docker buildx imagetools inspect python:3.12-slim --raw \
    | sha256sum  # or use `crane digest python:3.12-slim`
  ```
- [x] Helper `resolve_base_image(whitelist, key) -> BaseImageSpec` raising
  a clear error on unknown key. Used only by `prepare_py_container_task`.
- [x] Update `CODE_OVERVIEW.md` (three new entities + new module +
  loosened `DockerImage` semantics).

### 2. `prepare_py_container_task(task_dir, *, whitelist=None)`

Same shape as `prepare.py` but resolves cross-platform: prepare materializes
a temp `uv.toml` carrying `[tool.uv] required-environments` derived from the
bundle's `target_arches`, runs `uv lock --config-file <that>` (universal
lock across all requested platforms in one go), then per-arch wheel
filtering builds `PyMultiWheelPackage` entities. The local-package walker
is identical to the WASM path.

- [x] New file `fxtr_sandboxing/py_container_prepare.py`. Public function:
  ```python
  async def prepare_py_container_task(
      task_dir: Path,
      *,
      allowed_local_pkg_dirs: list[Path] | None = None,
      whitelist: dict[str, BaseImageSpec] | None = None,
  ) -> EntityRef[PyContainerTaskBundle]:
      ...
  ```
  `whitelist=None` falls back to `DEFAULT_BASE_IMAGES`. Consulted only
  at prepare time.
- [x] Read pyproject:
  ```toml
  [tool.fxtr.sandboxing]
  base_image = "py312"
  target_arches = ["linux/amd64", "linux/arm64"]

  [tool.uv]
  required-environments = [
      "sys_platform == 'linux' and platform_machine == 'x86_64'",
      "sys_platform == 'linux' and platform_machine == 'aarch64'",
  ]
  ```
  - `base_image` is required; validated against `whitelist`.
  - `target_arches` is required (non-empty list). Each entry must be a
    key in `spec.arches`. Empty list / unknown arch raises with a
    clear error listing the base image's supported arches.
  - We do *not* read `[tool.uv]`. That's between the user and uv.
- [x] **Cross-check `requires-python`.** The pyproject's
  `requires-python` constraint (e.g. `">=3.12"`) must accept the
  whitelist entry's `python_version` (e.g. `"3.12"`). Use
  `packaging.specifiers.SpecifierSet.contains(Version)` for the check.
  Fail fast with a clear error like:
  ```
  pyproject requires-python = ">=3.13" but base_image "py312" provides
  Python 3.12. Choose a base_image matching your requires-python, or
  relax requires-python.
  ```
  Cleaner than letting uv-resolution fail mid-lock with a less
  actionable error.
- [x] Build and store the `DockerImage` entity from the spec:
  ```python
  image = DockerImage(registry=spec.registry, digest=spec.digest)
  image_ref = image.store()
  ```
  Same pattern as `container_prepare.prepare_container_task`.
- [x] **Refactor `prepare.py`** to expose helpers usable by both flavors. Move
  shared logic into private helpers in `prepare.py` (or a new
  `_prepare_common.py`); call from both `prepare()` and
  `prepare_py_container_task()`. Pieces to share:
  - pyproject reading + entry-point validation
  - `_check_whitelist` / `_resolve_source_dirs` / `_validate_explicit_pkgs`
  - `_walk_package_dir` / `_read_into`
  - the local-package builder (`_build_source_package`) — source packages
    are still text-only here.

  **Done (post-checkpoint-3):** New `fxtr_sandboxing/_prepare_common.py`
  owns the shared helpers with non-underscored names: `read_pyproject`,
  `read_entry_point`, `resolve_deps`, `find_uv`, `check_whitelist`,
  `build_source_package`, `resolve_source_dirs`, `walk_package_dir`,
  `resolve_local_pkg_whitelist`. Module-private internals
  (`_read_into`, `_validate_explicit_pkgs`) stay underscored.
  `prepare.py` and `py_container_prepare.py` now both import from
  `_prepare_common`; the duplicated `_read_entry_point` is gone,
  and the cross-module underscored imports are replaced with proper
  non-underscored ones. The `_build_packages` functions in each
  flavor remain separate (they call common helpers but their wheel
  branches differ — single-wheel for WASM, multi-wheel-with-filter
  for py_container, which is the genuine difference between the
  flavors). Tests `test_prepare_file_walk.py` and
  `test_prepare_whitelist.py` updated to import the new names.
- [x] **`uv lock` runs against the user's pyproject directly.** No
  mutation, no workdir copying, no injection of `required-environments`.
  uv requires `required-environments` in the project's `pyproject.toml`
  (empirically verified that uv refuses the same key in a uv.toml
  config file), so we put the responsibility on the user: their
  pyproject's `[tool.uv] required-environments` tells uv which
  environments to ensure wheels for, and their
  `[tool.fxtr.sandboxing] target_arches` tells *us* which arches to
  ship. We don't try to keep them in sync — prepare's wheel-filter
  step (below) catches any mismatch with a clear actionable error.
  Same `uv lock` + `uv export --format pylock.toml` flow as WASM
  prepare:
  ```sh
  uv lock
  uv export --format pylock.toml --no-dev
  ```
- [x] **Empirically verified** against a tiny `markupsafe` project
  with `required-environments` for both linux arches — one `uv lock`
  + one `uv export --format pylock.toml` produced a pylock with one
  entry per package whose `wheels` list contains manylinux_2_28_x86_64
  + manylinux_2_28_aarch64 wheels alongside py3-none-any. Exactly
  the shape the materializer needs.
  *(`--python-platform` is NOT a `uv lock` flag; it's only for
  `uv pip install` / `uv export`'s output filtering.
  `required-environments` in pyproject is the lock-time knob.)*
- [x] **Per-arch wheel selection.** For each package in the exported
  pylock:
  - Source package (`directory`): build `PySourcePackage` via the
    file-walker, same as WASM path.
  - Remote package (`wheels`): for *each* arch in `target_arches`,
    find at least one compatible wheel from the pylock entry's
    `wheels = [...]` list via
    `packaging.utils.parse_wheel_filename(name)` against the arch's
    `ArchSpec`. Universal `py3-none-any` wheels match all arches.
    Deduplicate. Build a `PyMultiWheelPackage` whose `wheels` tuple
    contains a `PyWheelArtifact(name, url, sha256)` per match.
  - If no compatible wheel exists for some arch, raise with a clear
    actionable error that points the user at `required-environments`:
    ```
    package 'X' (version Y) has no wheel compatible with arch
    'linux/amd64' in the uv lock. This usually means
    [tool.uv] required-environments in your pyproject.toml doesn't
    include this arch. For your target_arches = [...], make sure
    your pyproject contains:

        [tool.uv]
        required-environments = [
            "sys_platform == 'linux' and platform_machine == 'x86_64'",
            ...
        ]
    ```
- [x] Build and store `PyContainerTaskBundle`:
  ```python
  bundle = PyContainerTaskBundle(
      base_image=image_ref,
      target_arches=tuple(target_arches),
      packages=packages,
      requires_python=requires_python,
      entry_point=entry_point,
  )
  return bundle.store()
  ```
- [x] Tests in `tests/test_py_container_prepare.py`:
  - happy path (a small project with one wheel-only dep that ships
    manylinux wheels for both amd64 and arm64 — e.g. `markupsafe`).
  - unknown base-image key raises.
  - missing `[tool.fxtr.sandboxing] base_image` raises.
  - missing `target_arches` raises.
  - empty `target_arches` raises.
  - `target_arches` containing an arch not in the spec raises.
  - dep with no wheel for one of the requested arches raises with a
    clear `(package, arch)` error.
  - `whitelist` override is honored.
  - the produced `PyMultiWheelPackage`s contain a `PyWheelArtifact`
    for each requested arch (verified by parsing each artifact's
    `name` and confirming the tag set covers the arch).
- [x] Update `CODE_OVERVIEW.md`.

### 3. `_spawn_runner_py_container` + spawner dispatch + env target_arch

The spawner needs one new piece of state: the env's `target_arch`. The
bundle's `DockerImage` ref + `target_arches` list tells us *which* arches
the bundle can run on; the env's `target_arch` picks which one we use.

- [x] `SandboxSpawnerImpl.__init__` gains
  `target_arch: str | None = None`. If `None`, the spawner detects at
  init time via container-runtime introspection (e.g. `docker version
  --format '{{.Server.Arch}}'`) or falls back to the host's
  `platform.machine()` translation. Forwarded from
  `create_sandbox_environment(target_arch=...)`.
- [x] Add the third bundle-type dispatch arm to
  `SandboxSpawnerImpl.spawn`:
  ```python
  if isinstance(loaded, TaskBundle):
      runner = self._spawn_runner_wasm
  elif isinstance(loaded, PyContainerTaskBundle):
      runner = self._spawn_runner_py_container
  elif isinstance(loaded, DockerTaskBundle):
      runner = self._spawn_runner_container
  ...
  ```
- [x] Validate compatibility before launching:
  ```python
  if self._target_arch not in loaded.target_arches:
      raise ValueError(
          f"This bundle supports arches {list(loaded.target_arches)}, "
          f"but the env's target_arch is {self._target_arch!r}."
      )
  ```
- [x] `_spawn_runner_py_container` body mirrors `_spawn_runner_container`:
  ```python
  async with _run_py_container_internal(
      bundle,
      target_arch=self._target_arch,
      cache_dir=self._cache_dir,
      insecure_registry=self._insecure_registry,
      transport_server=self._transport_server,
  ) as socket_stream:
      json_stream = SocketJSONStream(socket_stream)
      rpc_message_stream = wrap_json_stream(json_stream)
      async with open_rpc_scope(
          rpc_message_stream,
          remote_service_type=SandboxConnectProto[FxtrServiceData, UnknownService],
          name="host_rpc",
      ) as rpc_session:
          task_status.started((rpc_session.remote_service, rpc_session.scope))
          await teardown_event.wait()
  ```
- [x] Update `CODE_OVERVIEW.md` (third dispatch arm + target_arch).

### 4. Relocate + generalize `build_site_packages_for_bundle`

The existing helper in `fxtr_sandboxing/runner.py` materializes a
`TaskBundle`'s packages into a site-packages tempdir. We need a sibling
that does the same for `PyContainerTaskBundle`, which carries
`PyMultiWheelPackage` (multiple wheels per package, one of which must be
picked based on target arch).

- [x] Move `build_site_packages_for_bundle` into a new module
  `fxtr_sandboxing/_bundle_materialize.py`. Re-export from
  `runner.py` so `direct_launcher.py`'s warm-spawn path keeps working
  unchanged.
- [x] Generalize the helper. Two functions, or one polymorphic on bundle
  type — either works. Sketch with two:
  ```python
  async def build_site_packages_for_wasm_bundle(
      bundle: TaskBundle, cache_dir: Path,
  ) -> Path:
      """Unchanged behavior. Picks the single wheel from each
      PyWheelPackage (must be py3-none-any per existing constraint)."""

  async def build_site_packages_for_py_container_bundle(
      bundle: PyContainerTaskBundle,
      cache_dir: Path,
      *,
      target_arch: str,
  ) -> Path:
      """For each PyMultiWheelPackage, pick the PyWheelArtifact whose
      (python_tag, abi_tag, platform_tag) is compatible with the arch
      via `packaging.tags`. py3-none-any matches everything. Fail with
      a clear (package, arch) error if no compatible artifact."""
  ```
  Both share the source-package writing + tempdir-creation scaffolding.
- [x] The new wheel-picker uses `packaging.utils.parse_wheel_filename(artifact.name)`
  to get the tag set per artifact (no URL parsing involved — `name` is
  the canonical filename from the lockfile), then checks against the
  arch's `Tag(python_tag, abi_tag, platform_tag)` for compatibility.
  Picks the most-specific match; falls back to py3-none-any.
- [x] **`ArchSpec` already lives on the bundle** (see step 1). The
  materializer reads `bundle.target_arches[target_arch]` to get the
  tag triple, then matches against each `PyMultiWheelPackage`'s
  artifacts. No host-side config needed at spawn time.
- [x] No behavior change for WASM path. Run the existing WASM test
  suite to confirm.
- [x] Update `CODE_OVERVIEW.md`.

### 5. `_run_py_container_internal` low-level launcher

This is the new container launcher. **Copy `container_runner.py` first**, then
adapt. Mark copied blocks with `# TODO(2p2 step 9): share with container_runner`
so the refactor target is unambiguous.

- [x] New file `fxtr_sandboxing/py_container_runner.py`. Public surface
  (spawner-internal, like `_run_container_internal`):
  ```python
  @asynccontextmanager
  async def _run_py_container_internal(
      bundle: PyContainerTaskBundle,
      *,
      target_arch: str,
      cache_dir: Path,
      insecure_registry: bool = False,
      transport_server: TransportConnectionServer | None = None,
  ) -> AsyncIterator[anyio.abc.SocketStream]:
      ...
  ```
- [x] Body, step by step:
  1. **Load image**: `image: DockerImage = await bundle.base_image.load()`.
     Build the image-ref string `f"{image.registry}@{image.digest}"`.
     Same shape as `_run_container_internal`'s existing image-loading
     line. (The digest is typically a manifest-list digest in this
     mode; the runtime picks the per-arch image via `--platform`.)
  2. **Materialize site-packages**: call
     `build_site_packages_for_py_container_bundle(bundle, cache_dir,
     target_arch=target_arch)`. Returns a single flat tempdir
     containing the user's code, the user's deps (per-arch wheels
     filtered for `target_arch`), the fxtr runtime, and all transitive
     deps.
  3. **Open UDSes**: same as `_run_container_internal`. `socket_dir`
     tempdir, `task.sock` listener, optional `transport.sock` listener
     when `transport_server` is given.
  4. **Build the container-run command**:
     ```python
     cmd = [_get_runtime(), "run", "--rm", "--network", "none"]
     cmd.extend(["--platform", target_arch])   # tells the runtime which
                                               # per-arch image to pull from
                                               # the manifest list
     if insecure_registry:
         cmd.extend(["--scheme", "http"])
     cmd.extend(["-v", f"{site_packages}:/app/site-packages:ro"])
     cmd.extend(["-v", f"{task_socket_path}:/run/fxtr/task.sock"])
     if transport_socket_path is not None:
         cmd.extend(["-v", f"{transport_socket_path}:/run/fxtr/transport.sock"])
     cmd.extend(["-e", "PYTHONPATH=/app/site-packages"])
     cmd.append(image_ref)
     cmd.extend(["python", "-m", "fxtr_service_runner", bundle.entry_point])
     ```
     Note: we pass `entry_point` as a CLI arg rather than letting
     `fxtr_service_runner` discover it via `importlib.metadata`. The
     user's *task* package is a `PySourcePackage`, which only stores
     source files — no `.dist-info/entry_points.txt`. The
     `[project.entry-points."fxtr.service"]` declaration in the user's
     pyproject doesn't survive the file-walk into the bundle. The
     bundle pins the `module:function` string already (resolved from
     pyproject during `prepare_py_container_task`), so we forward it
     directly. (Existing container mode uses the discovery path
     because `uv sync` installs the user package with full
     dist-info; py_container's lighter delivery model can't.)
  5. **Launch + accept**: same `subprocess.Popen` / `_accept_or_exit` /
     transport accept loop / teardown handshake as `_run_container_internal`.
- [x] Re-use `_accept_or_exit`, `_handle_transport_conn`,
  `_transport_accept_loop` *verbatim* from `container_runner.py`. Either
  import them (cleanest), or copy with the step-9 TODO marker. Copy first,
  since import order between the two modules is otherwise irrelevant.
- [x] Update `CODE_OVERVIEW.md`.

### 6. Demo: `examples/py_container_rpc_task/`

Mirrors `examples/container_rpc_task` shape — same `GreeterProto` /
`GreetingConfig` — but **no Dockerfile, no rebuild.sh, no setup.sh**. Just
a pyproject + a Python package. Adds one new method so tests can confirm
linux-wheel resolution works.

- [x] Directory:
  ```
  examples/py_container_rpc_task/
    pyproject.toml
    rpc_task/
      __init__.py
  ```
- [x] `pyproject.toml`:
  ```toml
  [project]
  name = "py-container-rpc-task"
  requires-python = ">=3.12"
  dependencies = [
      # Use [fxtr2-portable] (pure-python flavor) instead of [fxtr2].
      # py_container's "no install step in container" model only works
      # with packages that ship as wheels; [fxtr2] pulls in cbrrr
      # (C-ext, sdist-only), the mashumaro git fork, and sqlitedict
      # (sdist-only), none of which work without a build step.
      # [fxtr2-portable] was designed for exactly this constraint:
      # pure-python deps + the patched mashumaro from a custom
      # prerelease index that ships a real py3-none-any wheel. User
      # tasks can still depend on arbitrary C-extension wheels
      # (markupsafe, numpy, etc.) — only the runtime layer is
      # constrained.
      "fxtr-sandbox-rpc-adapter[fxtr2-portable, http]",
      "fxtr-service-runner",
      "markupsafe",  # tiny; ships per-arch linux wheels (proof of multi-arch)
  ]

  [build-system]
  requires = ["hatchling"]
  build-backend = "hatchling.build"

  [tool.hatch.metadata]
  allow-direct-references = true

  [tool.hatch.build.targets.wheel]
  # Project name is py-container-rpc-task but the source dir is rpc_task
  # (matches the entry point). Tell hatch / fxtr's prepare walker.
  packages = ["rpc_task"]

  [project.entry-points."fxtr.service"]
  service = "rpc_task:run"

  [tool.fxtr.sandboxing]
  base_image = "py312"
  target_arches = ["linux/amd64", "linux/arm64"]

  [tool.uv]
  # Parallel to target_arches above — tells uv to ensure wheels resolve
  # for each. prepare's wheel-filter step fails with an actionable error
  # if these drift apart.
  required-environments = [
      "sys_platform == 'linux' and platform_machine == 'x86_64'",
      "sys_platform == 'linux' and platform_machine == 'aarch64'",
  ]

  [tool.uv.sources]
  # editable=true matches the source flag in fxtr-service-runner's own
  # pyproject (which depends on fxtr-sandbox-rpc-adapter via an editable
  # path). Without matching, uv reports a conflict.
  fxtr-sandbox-rpc-adapter = { path = "../../fxtr_sandbox_rpc_adapter", editable = true }
  fxtr-service-runner = { path = "../../fxtr_service_runner" }
  ```
  Mirrors `examples/rpc_task/pyproject.toml`'s shape (just the adapter
  + runner as direct deps; transitives resolve via path sources in
  those packages' own pyprojects) plus the py_container-specific
  `[tool.fxtr.sandboxing]` + `[tool.uv]` blocks.
- [x] `rpc_task/__init__.py`: mirror `examples/rpc_task/rpc_task/__init__.py`'s
  `GreeterProto` / `GreetingConfig` / `_Impl` / `run`, plus a
  `markupsafe_version()` method that returns
  `markupsafe.__version__` (proves the wheel actually installed and
  imports).
- [x] Update `CODE_OVERVIEW.md` (new example).

### 7. End-to-end tests

`tests/test_py_container_spawner.py` — mirror `test_container_spawner.py`'s
shape against the new demo. Skipped without a container runtime.

- [x] Fixtures: `_runtime_available()` (already exists in
  `test_container_spawner.py`'s pattern; lift or share). No image-pull
  guard needed for the default whitelist (the image is on Docker Hub),
  but the runtime needs to be able to pull it once. Document this.
- [x] Local `GreetingConfig` / `GreeterProto` mirrors of the demo's types,
  with `markupsafe_version() -> str` on the protocol.
- [x] Tests:
  - `test_py_container_spawn_then_connect_greet` — basic happy path
    against the host's native arch (auto-detected).
  - `test_py_container_markupsafe_version` — exercises a non-pure
    wheel; confirms the right arch's wheel was materialized.
  - `test_py_container_spawn_once_connect_twice` — multi-connect.
  - `test_py_container_teardown_basic` — slow marker.
  - `test_py_container_arch_mismatch_raises` — env's `target_arch` is
    not in `bundle.target_arches` → clear error before launching.
  (The "unknown base-image key" / "missing target_arches" errors are
  prepare-time checks — covered in `test_py_container_prepare.py`.)
- [ ] Optional: a cross-arch smoke test that launches with
  `target_arch="linux/arm64"` on an amd64 host (relying on the
  container runtime's emulation, qemu-user-static or Rosetta). Skip
  if emulation isn't available. Confirms the multi-arch promise —
  not just that the bundle resolved both arches at prepare time, but
  that an arm64 spawn actually picks arm64 wheels and an arm64
  image. Mark explicitly slow.
- [x] `tests/test_py_container_transport.py` — one stage-3 round-trip
  via `add_passthrough_transport` + `get_client`, confirming the
  transport multiplexer wiring works for this mode too. Modeled on
  `tests/test_transport_e2e_container.py`'s passthrough-HTTP test.
- [x] No new nested-spawn test for now; the existing nested-spawn
  paths route through `SandboxSpawnerImpl.spawn` and will pick up
  the new dispatch arm for free if a caller passes a
  `PyContainerTaskBundle` ref. (Sanity-check informally; don't add a
  full test in this stage.)
- [x] Update `CODE_OVERVIEW.md`.

### 8. Env-level surface

The env needs to know which arch py_container bundles should run as.
The whitelist itself is prepare-time-only — no kwarg needed for it.

- [x] `create_sandbox_environment(*, cache_dir, insecure_registry=False,
  target_arch: str | None = None)`. Forwards `target_arch` into
  `SandboxSpawnerImpl`. `None` triggers auto-detection (see step 3).
- [x] Auto-detection: query the container runtime for its default arch
  via something like `docker version --format '{{.Server.Arch}}'`
  (`{linux/amd64, linux/arm64}` are the two we care about), or — as a
  reasonable fallback — translate `platform.machine()` to the
  corresponding `linux/<arch>` string. Test both branches.
- [x] `direct_launcher.py`'s `create_direct_sandbox_environment` is
  WASM-only today. Designing a py_container warm-spawn analogue is
  out of scope here; note in CODE_OVERVIEW.md that `direct_launcher`
  remains WASM-only.
- [x] Update `CODE_OVERVIEW.md`.

### 9. Refactor: shared container scaffolding

**This step is part of "done", not optional.** The first pass duplicated
~150 lines between `container_runner.py` and `py_container_runner.py`. Pull
the shared scaffolding out before declaring the feature finished.

- [x] New module `fxtr_sandboxing/_container_common.py`. Lifts:
  - `_get_runtime()` / `_DEFAULT_RUNTIME` / `FXTR_CONTAINER_RUNTIME` env
  - `_accept_or_exit`
  - `_handle_transport_conn`
  - `_transport_accept_loop`
  - the teardown handshake (graceful wait → terminate → kill loop)
  - tempdir/listener boilerplate up to "cmd is built"
- [x] Both `container_runner.py` and `py_container_runner.py` import these.
  Each runner is left with only what's specific to it: the cmd-building
  block, plus how source mounts are wired.
- [x] Walk both files post-refactor and remove every
  `# TODO(2p2 step 9): share with container_runner` marker.
- [x] No new tests; existing container + py_container tests should still
  pass. Run both end-to-end suites.

  *If a tempting bigger refactor surfaces (e.g. one launcher class
  parameterized by bundle type), do it as a follow-up — out of scope
  for stage 2p2.*
- [x] Update `CODE_OVERVIEW.md`.

### 10. Update CODE_OVERVIEW.md

Final consolidation. Earlier steps should already have touched it; this
one is the catch-all.

Touchpoints:

- New mode summarized in the "Status of the rewrite" section
  (alongside Stage 2 / Stage 3 / Stage 4 — or as a Stage 2p2 entry).
- New entity type listed in `entities.py` description.
- `fxtr_sandboxing` package structure: new files (`base_images.py`,
  `py_container_prepare.py`, `py_container_runner.py`,
  `_container_common.py`), and the tightened `DockerImage` docstring
  in `fxtr_sandbox_types/entities.py`.
- New example + new tests.
- Tightened `DockerImage` docstring (single-arch invariant).
- A short sub-section under "Key concepts" describing the py_container
  mode and how it sits between WASM and Container.

## Decisions captured here for future reference

- **Three modes, one spawner.** `SandboxSpawnerImpl.spawn` dispatches on
  bundle entity type. Adding the third mode does not require new caller-
  side ergonomics; it's the same `spawn(bundle_ref).replace_service_type(
  ...).resolve().connect(config)` flow.
- **Bundle is multi-arch by construction.** One
  `PyContainerTaskBundle` runs on any arch in its `target_arches`.
  Two mechanisms make this work:
  - `PyMultiWheelPackage` holds per-arch wheels (one
    `PyWheelArtifact` per matching tag triple). The spawn-time
    materializer picks the artifact matching the env's target arch.
  - `base_image` is an `EntityRef[DockerImage]` whose digest is the
    **manifest-list digest** of a multi-arch tag. The container
    runtime resolves the manifest list to a per-arch image at pull
    time, driven by `--platform <target_arch>`.
  Both mechanisms align: the runtime selects the right per-arch
  image *and* the materializer selects the matching wheels. The
  bundle stores `ArchSpec`s for each supported arch inline, so it's
  fully self-describing — spawn-time consults nothing else.
- **`DockerImage` accepts both digest forms.** The same entity is used
  by `DockerTaskBundle` (typically single-arch image manifest digests,
  produced by buildkit's default single-arch builds) and
  `PyContainerTaskBundle` (typically manifest-list digests, so the
  runtime can pick per-arch). The docstring spells out when to use
  which. No new entity type, no behavior change for existing
  `DockerTaskBundle` callers.
- **Whitelist is prepare-time-only.** `BaseImageSpec` lives in
  `fxtr_sandboxing/base_images.py` and is consulted by
  `prepare_py_container_task` to translate the user's friendly
  pyproject key into a `DockerImage` entity + per-arch `ArchSpec`s.
  Spawn-time never reads from the whitelist; everything it needs is
  already in the bundle. (Defense-in-depth env-level allowlist is an
  open option; see "Open issues" — not needed for the spike.)
- **Runtime rides in the bundle, same as WASM.** The user's
  `pyproject.toml` declares `fxtr-sandbox-rpc-adapter[fxtr2-portable, http]`
  and `fxtr-service-runner` as direct deps; `uv export` resolves the
  full transitive closure (user code + user deps + fxtr runtime +
  runtime's pure-python transitive deps) into the bundle's `packages`
  dict; `build_site_packages_for_bundle` materializes the lot into one
  flat tempdir per spawn; that tempdir is the *only* thing bind-mounted
  into the container. No host-side site-packages spelunking, no separate
  runtime-mount module, no special handling of the runtime layer —
  the runtime is just another set of deps that uv resolves. The WASM
  path already proves this works (pure-python closures fit fine in one
  flat site-packages dir).
- **Entry-point passed explicitly on the cmdline.** Unlike the existing
  container mode (which prefers entry-point discovery via
  `importlib.metadata`), py_container has no installed user package to
  discover from — the user bundle is a bind-mount of source files,
  not a pip install. The bundle already pins the entry-point string at
  prepare time, so we forward it as the CLI arg to
  `fxtr_service_runner`.
- **Wheel tags pinned per `ArchSpec`.** The whitelist's `BaseImageSpec`
  carries one `ArchSpec(python_tag, abi_tag, platform_tag)` per
  supported container-platform string. The matching arches selected
  by `target_arches` get copied verbatim into the bundle's
  `target_arches` dict. The tags describe the *container's* Python,
  not the host's. Prepare doesn't generate or read PEP 508 markers —
  uv's `required-environments` is a separate concern owned by the
  user, who declares it in their pyproject's `[tool.uv]` block.
- **Per-spawn materialization, no cache yet.** Each spawn pays a small
  cost (~10s of ms for source-pkg writes plus wheel-extract). Content-
  addressed cache is a follow-up if profiling shows it matters; the
  primary motivation for caching is the WASM eryx factory's ~3s setup,
  which doesn't apply here.
- **Copy-then-refactor.** First version duplicates ~150 lines of
  `container_runner.py` (UDS setup, transport multiplexer, teardown
  handshake). Step 9 extracts to `_container_common.py`. Inline TODO
  markers identify the duplicated regions so the refactor is mechanical.

## Likely gotchas

- **Multi-arch base images: store the manifest-list digest.** For
  py_container we *want* the manifest-list digest, because the
  container runtime resolves it per-arch via `--platform` — and the
  bundle's `PyMultiWheelPackage`s have wheels for each arch ready to
  match. Pull the manifest-list digest with:
  ```sh
  crane digest python:3.12-slim
  # or:
  docker buildx imagetools inspect python:3.12-slim --raw | sha256sum
  ```
  Don't paste a per-arch image manifest digest by mistake — that'd
  pin one arch and defeat the multi-arch design.
- **`--platform` not optional.** We always pass `--platform <target_arch>`
  on `docker run` for py_container, even when it matches the host —
  it's how the runtime resolves the manifest list. Missing
  `--platform` would fall back to host-arch resolution, which is
  *usually* the same, but isn't guaranteed when running cross-arch
  via emulation.
- **User keeps `target_arches` and `required-environments` in sync
  manually.** Two parallel declarations in pyproject — they're not
  cross-validated by us. Wheel-filter failure at prepare time catches
  any drift, with a clear error pointing the user at the fix. We
  could later add a doctor / lint pass to nag earlier, but it's not
  blocking.
- **Manylinux generation in `ArchSpec.platform_tag` doesn't have to
  be an exact filename match.** Empirically verified against
  `markupsafe`: uv emits multi-tag wheels like
  `manylinux2014_x86_64.manylinux_2_17_x86_64...` and
  `manylinux2014_aarch64.manylinux_2_17_aarch64.manylinux_2_28_aarch64`
  in the pylock — `packaging.tags` compatibility matching handles
  the cross-product, so any reasonable manylinux choice in our
  whitelist will match wheels of any newer manylinux flavor.
- **Runtime closure must ship as wheels — `[fxtr2-portable]` is the
  default.** The "no install step in container" model means every dep
  in the runtime closure has to ship a wheel that we can extract.
  `[fxtr2]` doesn't fit: it pulls `cbrrr` (C-extension, sdist-only on
  PyPI), the mashumaro git fork (no PyPI wheel), and `sqlitedict`
  (sdist-only). `[fxtr2-portable]` was designed for exactly this
  constraint in the WASM context — pure-python deps + a patched
  mashumaro served from a custom prerelease index as a real
  py3-none-any wheel. Use `[fxtr2-portable]` for py_container demos
  and tasks. User tasks can still pull in *non-runtime* C-extension
  wheels (`markupsafe`, `numpy`, etc.) — anything that's published as
  a wheel on PyPI for the target arches works fine.
- **`fxtr-service-runner` reachable as a top-level package.** The
  in-container CMD is `python -m fxtr_service_runner <entry_point>`.
  This works as long as the bundle's site-packages tempdir is on
  `PYTHONPATH` and `fxtr_service_runner/` sits at its root. Both are
  true: it's a top-level package in its own dist, and
  `build_site_packages_for_bundle` writes everything flat.
- **`--network none` with bind-mounted runtime.** No network is fine —
  the runtime doesn't reach out to PyPI at container-run time (all
  wheels are pre-resolved + materialized host-side). Same as the
  existing container mode.
- **uv inside the container.** *Not used* in this mode. The container
  just runs `python -m fxtr_service_runner ...` against bind-mounted
  source dirs. No install step, no `uv sync`. This is the main
  ergonomic win over the existing container mode.
- **Stage-3 transport "should just work."** The transport multiplexer
  is mounted at the same `/run/fxtr/transport.sock` path; the runtime
  inside the container is the same `fxtr_sandbox_rpc_adapter` code.
  One explicit test (step 7) confirms; no other plumbing changes
  needed.
- **Nested spawn "should just work" too** — the inner-vs-outer
  dispatch is purely on bundle type at the host's `SandboxSpawnerImpl
  .spawn`. An outer WASM/container/py_container sandbox holding a
  `ServiceRef[SandboxSpawnerProto]` can spawn any of the three.
  Worth a manual smoke-test in step 7; full automated coverage is a
  follow-up.

## Open issues / things to revisit

- **Direct-launcher integration.** `direct_launcher.py` is currently
  WASM-only. Designing a py_container analogue (warm-spawn against a
  long-lived base-image container? or just a thinner wrapper?) is
  out of scope here. Note in CODE_OVERVIEW.md.
- **Linux-only host?** The plan assumes the host can run Linux
  containers natively (or via Apple Container's VM). Doc note: this
  mode does not work without a container runtime. Same constraint as
  the existing container mode.
- **Cross-arch at *run* time** (not just resolution). A bundle whose
  `target_arches` includes `linux/arm64` only actually *runs* on an
  amd64 host if the runtime can emulate (qemu-user-static via Docker,
  Rosetta via Apple Container, etc.). Cross-arch resolution at prepare
  time always works (uv just looks at indices); cross-arch *execution*
  is runtime-dependent. Documented as a known constraint; not blocking
  for the spike's primary use case (matching host arch).
- **`requires-python` enforcement.** The bundle's `requires_python`
  field exists but the whitelist's `python_version` is the actual
  python that runs the bundle. Add a prepare-time check that the
  whitelist entry's python satisfies the bundle's `requires_python`
  range (`packaging.specifiers`). Trivial; do it during step 2.
- **Whitelist source of truth.** Hardcoded default in `base_images.py`
  is fine for now. Users who want to pin their own internal base
  images pass a `whitelist=` arg to `prepare_py_container_task`; the
  env's `create_sandbox_environment` doesn't need a whitelist kwarg
  because spawn-time consults nothing about base images. Eventually
  the whitelist may want to live in a config file checked into each
  project. Defer.
- **Optional spawn-time allowlist for defense-in-depth.** Today, the
  env trusts whatever `DockerImage` ref the bundle carries — same as
  the existing container mode. If a deployment wants to restrict what
  bundles its env will spawn (say, "only images in this set of
  digests are allowed"), add an `allowed_base_images: set[FxtrID] |
  None` kwarg to `create_sandbox_environment` and check
  `bundle.base_image.fxtr_id in allowed` in
  `_spawn_runner_py_container` before launching. Default None means
  no check. Not needed for the spike; revisit when an operator asks
  for it.
- **What if a user package needs a system .so the slim base image
  doesn't ship?** They're stuck — same problem as the existing
  container mode if the Dockerfile didn't `apt-get install` the
  right `-dev` package. This mode trades flexibility for ergonomics
  by design. Document the failure mode in the new module's docstring.

## Update CODE_OVERVIEW.md

(Final catch-all bumped per CLAUDE.md guidance; also bumped after each
sub-feature above.)
