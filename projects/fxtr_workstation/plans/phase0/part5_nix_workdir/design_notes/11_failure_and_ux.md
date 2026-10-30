# Failure surfaces & UX

How nix-driven failures and the few nix-touched success-path
decorations surface to the agent. Most of the contract was pinned
in Areas 02 / 04 / 08; this note consolidates and fills the gaps.

> **Revised (2026-05-29):** the workstation sandbox is host-built and
> `run_script` runs no nix preflight (see
> `../stage2_sandbox_runtime.md` "Revised direction"). So the
> per-`run_script` `--- nix ---` setup header and the two
> sandbox-specific up-front failures (no-flake / missing-`sandbox` /
> up-front sandbox build) **no longer apply**. What survives is the
> *in-script* surface: nix runs lazily inside `retrieve_nix_entity`
> (Area 04) and the preset path (Area 06), and its eval/build errors
> surface there. The sections below are updated accordingly.

> **Revised (2026-05-30):** there are now **three** nix failure
> surfaces, at three consumers: the **MCP** (`retrieve_nix_entity`,
> `path:` data — the MCP-side `NixEntityBuildFailed` + `.tmp_outputs/`
> dump documented below); the **spawner** (`spawn` of a git-referenced
> service); and the **dispatcher** (`set_preset` of a git-referenced
> preset). The latter two add a git-fetch failure mode (bad commit /
> unreachable repo / `flakePath` doesn't contain a flake) on top of the
> usual eval/build failures, and each surfaces as an `invoke_error` on
> its own service call rather than in a `run_script`. See the new
> section below.

## Successful-run header

There is **no** `--- nix ---` setup header on `run_script` responses
— `run_script` does no nix work to report. (A given script *may*
trigger nix lazily via `retrieve_nix_entity`; per-retrieval build
timing is not surfaced in v0. If we later want it, a
`--- nix entity builds ---` trailer block could slot in cleanly,
using the existing `_fenced` helper.)

## Failure surfaces

> These all surface at the `retrieve_nix_entity` (or preset) call
> site, not as a `run_script` preflight. There is no "no `flake.nix`"
> or "missing `fxtr.workstation.sandbox`" preflight error anymore — a
> flake-less workdir runs scripts fine; only nix-builtin calls need a
> flake.

### No `flake.nix`, hit inside `retrieve_nix_entity`

When a script calls `retrieve_nix_entity` in a workdir with no
`flake.nix`, the call raises a friendly error:

```
No flake.nix in working dir <path>, so nix-backed entities are
unavailable. See <link/example>.
```

It flows through the in-script error path below (not a preflight
tool-error).

### `nix eval` failure

Stderr surfaces in a fenced section at the call site:

```
--- nix eval error ---
<full nix stderr verbatim>
--- end nix eval error ---
```

### In-script `retrieve_nix_entity` build failure

Surfaces as an exception raised at the `retrieve_nix_entity`
call site:

```python
class NixEntityBuildFailed(Exception):
    """Raised when nix-building an entity layer fails."""
```

The exception's message includes:

- The entity name and the full attr path (`fxtr.entities.<name>.layer`).
- The last N lines of nix stderr (N ≈ 20).
- The absolute path to a file containing the full stderr (see
  "Build-output dump files" below).

The exception flows through the same `_format_script_error`
two-frame compression as any other script exception (script
runner → builtin → raise). Compressed message is what the agent
sees inline; full traceback isn't usually interesting (the
informative part is the nix stderr, not the Python call chain).

#### Distinct sub-causes inside `retrieve_nix_entity`

The MCP-side code path naturally separates a few cases — keep the
messages distinct:

- **Unknown entity name** (`name` not in the structure-eval map)
  → `LookupError(name)`. Pre-existing semantics from Area 04.
- **Entity attr exists but the `.layer` derivation can't be
  evaluated** (e.g. eval-time exception specific to that
  derivation) → `NixEntityBuildFailed` with the eval stderr; the
  attr-path included makes it clear the problem is the entity, not
  the flake.
- **Eval works, build fails** → `NixEntityBuildFailed` with the
  build stderr.
- **Build succeeds, layered file store reader can't find
  `layerKey`** → `LookupError(layerKey)` (pre-existing layered
  store error). Distinct from build failure because the agent
  fixes it by editing their flake's `layerKey` value, not their
  build inputs.

We don't enumerate every possible flavor of nix-build error;
those all surface verbatim through the stderr.

## Spawner / dispatcher git-ref failures

The spawner and dispatcher are **workstation services**, so they
**just raise a normal exception** with the relevant nix/git stderr in
the message — they do **not** write any dump file themselves. (They
may not even have access to the agent's workdir, especially when
remote, so a `.tmp_outputs/` write would be wrong here.) The raised
error travels back as an `invoke_error` on the `spawn` / `set_preset`
call and surfaces in the agent's script through the **normal in-script
exception path** — the same path that already feeds the MCP's existing
exception capture (`ExceptionLog` / `get_full_exception`; we're
considering also logging it out somewhere in the future). No new
file-writing machinery on the service side.

The distinct buckets (each just shapes the exception message):

- **Git-fetch failure** — the `(commit, flakePath)` doesn't resolve in
  the consumer's repo: unknown commit, unreachable remote, or
  `flakePath` points at a dir with no flake. Message names the repo +
  commit + flakePath and includes the git/nix stderr. (Common operator
  cause: the agent forgot to `git push` to a remote consumer.)
- **Stale/missing lock** — the committed `flake.lock` is out of date or
  absent and the build is run `--no-update-lock-file` (see Area 12), so nix errors
  rather than re-locking. Message points the agent at committing an
  up-to-date lock.
- **Eval failure** — the flake evaluates but the requested attr
  (`fxtr.services.<name>` / `fxtr.workstation.uiPresets.<name>`) is
  missing or malformed. Message names the attr.
- **Build failure** — `sitePackages` / a renderer ESM fails to build;
  stderr surfaced in the message.

The consumer is a thin pass-through over nix/git stderr, not a
reinterpreter.

## Build-output dump files

Long nix stderr is too large to inline in every exception
message. Instead, the MCP writes it to a file in the agent's
working directory and references the file in the error message:

```
<workdir>/.tmp_outputs/<timestamp>_<short_id>.log
```

- `.tmp_outputs/` is a conventional folder in the working dir.
  Created lazily on first write. Expected to be `.gitignore`'d
  (the workdir template / docs include this in the boilerplate).
- One file per failed nix invocation; older files are not
  auto-cleaned in v0 (the dir is the agent's, they can prune as
  they wish; we revisit if it bloats in practice).
- The agent reads files via their normal file-access tools (they
  have direct shell access to the workdir); no MCP builtin needed.

This mechanism replaces *only* the nix-error case. The existing
`ExceptionLog` + `get_full_exception` builtin (for compressed
script tracebacks) stays as-is for now.

**Open follow-up (not blocking v0):** the user observed that the
file-on-disk approach could also replace `get_full_exception`
across the board, simplifying away the in-memory LRU and unifying
"how do I get the full text" for both nix errors and Python
tracebacks. Worth doing as a small refactor once the v0 nix path
lands. Captured here so we don't lose track of it (and see
`09_migration.md` / `09_migration.md`-adjacent cleanup).

## Long builds

Builds happen via `anyio.run_process` / `anyio.open_process`; the
MCP's main task is never blocked. Nix's stderr streams to the
MCP's own stderr (operator log) during the build — the operator
running the MCP can watch progress there.

**No in-band agent progress notifications in v0.** A
`retrieve_nix_entity` call simply takes as long as the layer build
takes; the agent sees no progress, only the eventual result (or a
`NixEntityBuildFailed`). Per-retrieval timing is not surfaced in v0.

**Human-operator UX in the frontend** (someone watching the
workstation UI while the agent is mid-build) — out of scope for
v0. The agent-MCP service registered on the workstation (see
`projects/fxtr_workstation_agent_plugin`) could eventually expose a
"currently building" status marker, but v0 just waits.

## Mid-script edits

Edge case from Area 08 (Q8.B): the agent edits `flake.nix` *during*
a `run_script` call (e.g. between the eval and the build, or
between an early `retrieve_nix_entity` and a later one). Nix's
own atomicity guarantees mean each invocation sees a consistent
view; we don't special-case this. If it produces a confusing
error, the agent can re-run.

## Open questions

- Q11.A: Exact line count for the inline nix-stderr tail in
  `NixEntityBuildFailed`. Tentative N=20.
- Q11.B: Long-term cleanup policy for `.tmp_outputs/`. v0 = no
  cleanup; revisit if it bloats.
- Q11.C: Migration of `get_full_exception` to the file-based
  mechanism. Follow-up refactor; tracked as a candidate
  simplification, not a v0 blocker.
