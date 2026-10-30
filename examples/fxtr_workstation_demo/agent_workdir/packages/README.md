# `packages/` — how a package here gets consumed

A directory under `packages/` can be consumed in **three different
venv contexts**, and which ones apply determines what its
`pyproject.toml` is allowed to declare. Read this before adding
dependencies to a package — the failure modes are confusing uv
resolution errors far from the cause.

## The three contexts

1. **The `run_script` sandbox (code mount, no resolution).** Every
   immediate subdirectory of `packages/` with a `pyproject.toml` +
   `src/` is volume-mounted into the eryx sandbox and added to
   `sys.path`. Dependencies are **not installed** — the sandbox
   runtime closure already provides fxtr2 + the bridge. Here the
   pyproject is just a marker; its `dependencies` are never read.

2. **Host venvs (resolved by an aggregator root).** The
   `type_entities/` aggregator (so the flake can build typeEntity
   layers) and the demo backend's `sys.path` inserts both import
   packages from here into a **full host fxtr2** environment. The
   aggregator's own pyproject declares `fxtr2` and the package list;
   uv only honors the *root's* `[tool.uv.sources]`, so member
   packages don't need (and shouldn't carry) their own.

3. **Spawn environments (the only context that installs a package's
   deps).** A `fxtr.services.<name>` flake attr points
   `mkWasmService` / `mkContainerService` at a directory whose
   pyproject **is** the dependency declaration uv2nix resolves and
   installs into the guest.

## The rules that fall out

- **Packages consumed in contexts 1 + 2 are "marker-style": declare
  NO dependencies.** fxtr2 is supplied by the embedding venv — full
  fxtr2 on the host, the runtime closure in the sandbox. If such a
  package declared a wasi-pinned dep set it would break the host
  aggregator; a host-only dep set would break a wasm spawn env that
  depends on it. (`presentables/`, `transcripts/`,
  `docent_fxtr_proxy/`, `simple_tasks/`, and the `delayed_echo/` /
  `simple_scheduler/` package roots are all this shape.)

- **A wasm service that also needs host-importable types uses the
  split shape** (`delayed_echo/`): the package root stays
  marker-style (so it can join `type_entities/` — e.g. for a
  renderer's typeEntity), and a `sandbox_spawn/` subfolder holds a
  *virtual* uv project (`package = false`) that depends on the parent
  by path, pins the wasi resolution, and declares the `fxtr.service`
  entry point. The flake's `mkWasmService` points at `sandbox_spawn/`.
  (`sandbox_spawn/` is not an immediate subdir of `packages/`, so
  context 1's walk never sees it.)

- **A wasm service with no host-import need uses the single-pyproject
  shape** (`haiku_service_wasm/`, `simple_task_lib/`, `prompt_task/`):
  the package itself carries the
  wasi pin + entry point + deps. Simpler, but its module can then only
  be imported where the wasm dep set resolves (and in context 1, where
  deps are ignored).

- **Container services declare full deps** (`haiku_service_oci/`,
  `streaming_prompt_task_oci/`): the image venv is real Linux
  CPython, so the pyproject lists everything — full `fxtr2`,
  `fxtr-service-runner`, native-wheel deps like `anthropic` —
  following the conventions in the `container_greeter` /
  `container_transporter` templates. `streaming_prompt_task_oci/`
  also keeps its heavy, container-only imports out of the package
  `__init__` (they live in `service.py`, imported lazily by `run`)
  so the package stays importable agent-side for its shipped
  `VIVID_DESCRIPTION_PROMPT` example template.

- **Spawn-env pyprojects use the comment-flip `[tool.uv.sources]`
  convention** (`# fxtr-lib-sub:` markers): relative paths active in
  the template, pinned git sources activated in the scaffolded
  workdir copy. No `uv.lock` is committed in the template; the
  scaffold generates one per substituted package. See
  `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage3_bundle_and_spawner.md`
  ("Test & scaffold workdir setup").
