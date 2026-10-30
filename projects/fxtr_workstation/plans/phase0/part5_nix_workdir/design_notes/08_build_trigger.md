# Build trigger

When and how the MCP shells out to `nix` to keep its picture of
the agent's flake fresh.

> **Revised (2026-05-29):** the eager top-of-`run_script` eval + sandbox
> build is **gone**. The workstation sandbox is host-built and doesn't
> go through nix, so a plain `run_script` shells out to nix **not at
> all**. Nix is invoked purely *lazily* — when a script calls
> `retrieve_nix_entity` (Area 04), and when the dispatcher loads a
> preset (Area 06). The "no client-side caching / no stat shortcut /
> trust nix's eval cache" stance below still holds for those lazy
> calls; the cadence section is updated to match. Rationale:
> `../stage2_sandbox_runtime.md` "Revised direction".

> **Revised (2026-05-30):** this note is about the **MCP's** nix calls
> (DATA entities, resolved via the bare workdir path — auto-git/`path:`,
> non-frozen). Two *other* consumers now
> shell out to nix on their own: the **spawner** (`spawn` →
> `nix eval`/`nix build` of a `fxtr.services.<name>` attr) and the
> **dispatcher** (`set_preset` → eval+build of a preset attr). Those
> resolve **by git commit** (`git+file://<repo>?rev=<c>&dir=<path>#attr`),
> not `path:`, because they consume *committed* code (Areas 05/06/12).
> Each has its own nix wrapper module; the "trust nix's cache" stance
> applies to all three.

## Cadence: lazy, on first nix-builtin use

`run_script` itself runs **no** nix preflight. Nix work happens only
when a script reaches for nix-backed data:

1. On a `retrieve_nix_entity(name)` call, the MCP does a **`nix eval`**
   of the flake's `fxtr` attr to refresh the structure map (entity
   names → `(layerKey, layer-attr-path)`), then a lazy
   **`nix build .#fxtr.entities.<name>.layer`** to materialize that
   one layer (see Area 04). A missing `flake.nix` is a friendly error
   raised *here*, not at the top of `run_script`.
2. On dispatcher preset load (Area 06), `nix build` of the preset's
   layers.

There is **no client-side caching of the eval / build results**.
We always shell out and let nix's own eval cache + content-addressed
store make repeat calls cheap.

### Why no stat-based shortcut

The agent could change anything under the working dir that affects
the flake's outputs — not just `flake.nix` and `flake.lock`, but
any `import`-reachable file, anything pulled in via `src = ./...`,
anything in a sub-directory the helper functions walk. We can't
match nix's own change-detection logic without re-running nix
itself, and any cheaper heuristic risks false-negatives where the
agent edits something and the next script silently uses stale
state. Nix's eval cache is already designed for this; we trust it.

Cost in the steady-state cached case is on the order of an exec +
a few RPC roundtrips — small relative to running a sandboxed
script. If it turns out to be too slow in practice, we revisit
with an opt-in shortcut, but v0 is "trust nix."

## No explicit refresh tool

Agents don't need to call any "rebuild" tool; the implicit cadence
handles it. They *can* shell out to `nix build` themselves if
they want to inspect a build's behavior outside `run_script`
(useful for debugging build failures), but that's incidental and
not part of the MCP surface.

## Code structure: `fxtr_workstation_mcp/nix.py`

MCP-side nix calls live in a new module
`fxtr_workstation_mcp/nix.py`. Surface (sketch):

- `async eval_flake_structure(working_dir) -> FlakeStructure` —
  runs `nix eval --json .#fxtr --apply <filter>` and parses the
  result into a small typed dataclass capturing the maps the MCP
  needs.
- `async build_attr(working_dir, attr_path) -> StorePath` —
  runs `nix build --json .#<attr>` and returns the resolved
  `outputs.out` store path.
- Helpers for stderr surfacing (see "Failure modes" below).

> **Revised (2026-05-30):** the flake ref is the **bare `working_dir`
> path** (no `path:` prefix), so nix auto-selects `git+file://` inside a
> git repo (inferring `dir=`) and `path:` otherwise — the same fetcher
> as the spawner/dispatcher, but **non-frozen** (the data loop must
> accept a dirty tree + soft lock). The landed Stage-2 `nix.py` used an
> explicit `path:`; Stage 4 switches `build_attr`/the eval to the bare
> form. New untracked files are invisible under git-form → the MCP emits
> the untracked-files notice (Area 04). `--no-link --print-out-paths`
> still applies (no `result*` symlinks).

This is **parallel to**, not shared with, the spawner-side nix
wrapper flagged as Q12.E (`fxtr_sandboxing/nix.py`). The two have
different concerns: the MCP-side wrapper deals with flake-attr
paths + working-dir context, while the spawner-side wrapper deals
with raw `.drv` realization + no working-dir context. Keep them
separate.

All calls use `anyio.run_process` (or `open_process` for streaming
stderr) so the MCP's main task is never blocked on nix work. The
worker task in `WorkstationConnection` is what awaits the build;
other connection traffic (foreign events, channel notifications
piggybacked on the OutgoingBuffer) keeps flowing because the
reader / supervisor split is preserved.

## Failure modes

Three buckets, all with deterministic handling. Details on the
agent-facing error format live in Area 11; this section pins the
contract.

- **No `flake.nix` in working dir** — dedicated friendly error
  raised when a script calls `retrieve_nix_entity` (or a preset is
  requested) in a workdir without a flake:
  `"No flake.nix in working dir, so nix-backed entities are
  unavailable. See <link/example>."` It surfaces as a
  `NixEntityBuildFailed`-style in-script error, not a `run_script`
  preflight tool-error (there is no preflight). Scripts that use no
  nix builtins run fine without a flake.
- **`nix eval` failure** — flake exists but doesn't evaluate
  (syntax error, eval-time exception, etc.), hit during a
  `retrieve_nix_entity` / preset call. Stderr from nix is captured
  and surfaced to the agent as a fenced `--- nix eval error ---`
  section (same `_fenced` mechanism `_format_script_error` uses).
- **`nix build` failure** — eval succeeded but building an entity
  layer (via `retrieve_nix_entity`) or a preset layer failed.
  Raises `NixEntityBuildFailed` inside the script (see Area 04); the
  compressed traceback + full text via `get_full_exception` mechanism
  applies.

In all build-failure cases the full nix stderr is included
verbatim. Agents can re-run `nix build` themselves to debug; the
fxtr surface is a thin pass-through, not a wrapper that
reinterprets nix errors.

### Stderr handling during long builds

`nix build` can take seconds to minutes on a cold cache. The MCP
streams nix's stderr to the MCP's own stderr (log file / parent
process), so the operator running the MCP can watch progress. For
v0, **the agent doesn't get streaming progress notifications**;
their `run_script` call just blocks (asynchronously — no main-task
stall) until the build finishes. Worth revisiting in Area 11 once
we have telemetry on how often long builds actually happen in
practice.

## Output discovery

`nix build --json` returns each requested attr's resolved output
paths. We read `outputs.out` (the single-output convention from
Area 12) and use that as the store path. Nothing subtler.

## Eager vs lazy

There is no eager phase anymore — it's lazy all the way down:

- **Lazy (on demand):** `nix eval` of the structure + `nix build` of
  individual `fxtr.entities.<name>.layer` derivations, triggered by
  `retrieve_nix_entity` calls inside the script (Area 04); preset
  layers on dispatcher preset load (Area 06).

The agent can still warm caches manually with `nix build .#fxtr.all`
(Area 03) if they want.

## No prefetch / no `set_working_dir` warming

The MCP does **not** invoke nix during `set_working_dir`. The
first `retrieve_nix_entity` after a working-dir change pays the full
cold-cache cost. Trade: small first-retrieval latency vs. always
running nix at session start (which is wasted if the agent never
touches a nix entity).

Reconsider when we have actual usage telemetry.

## Open questions

- Q8.A: Concrete shape of the `--apply <filter>` expression for
  the structure-eval call. It should pull out exactly what the MCP
  needs (entity names + layerKeys; presence of `sandbox`) without
  forcing evaluation of derivations the MCP doesn't care about
  yet. To pin in implementation.
- Q8.B: Behavior when the agent edits `flake.nix` *during* a
  `run_script` call (between the eval and the build, or between
  build and script start). Unlikely in practice but possible.
  Lean: nix's atomicity guarantees mean each call sees a consistent
  view; not worth special-casing.
