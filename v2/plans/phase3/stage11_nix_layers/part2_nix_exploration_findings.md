# Stage 11 — Part 2 exploration findings

Captured while attempting to implement the nix prototype. Implementation
paused; this doc records what we built, what we hit, and the paths
forward we identified. See `part2_nix_exploration.md` for the original
plan. The work-in-progress lives at
`/Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/` (a
separate spike directory outside the fxtr repo, per the most recent
restructuring attempt).

## Status

- **Python side is working end-to-end** without nix. The spike has a
  uv workspace with `nix_prototype_lib` (entity types + dataset),
  `fake_remote.py` (Hypercorn server), and `preload_flake/` (the
  preload workload). `uv run` against the spike's venv successfully:
  - Serves the 5-node linked-list dataset from `fake_remote.py`.
  - Runs `preload <url> <out_dir>` against that server and produces
    a valid layered file store with `data/a.cbor`, `data/b.cbor`,
    `precomp/c.json` + the inevitable precomp entries for the
    builtin `.` ctx marker and the `LinkedNode` type id.
- **The nix flake doesn't build yet.** All attempts hit uv2nix's
  inability to handle path-deps that reach outside the workspace
  root (which fxtr2 and fxtr v1 inherently do).

## What we tried, in order

### 1. Original layout: `examples/nix_prototype/` inside the fxtr repo

Standard uv2nix flake at `preload_flake/flake.nix`, modeled on the
existing spike `/Users/daniel/extgit/spikes/2026_05_15_nix_spike/sample-app/`.

```nix
workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
```

`preload_flake/pyproject.toml` had `fxtr2 = { path = "../../..", editable = true }`
and `fxtr = { path = "../../../..", editable = true }`. `uv sync` worked
fine (uv handles cross-directory path deps natively).

First `nix build` attempt failed:

```
error: opening Git repository "/Users/daniel/fxtr-worktrees/fxtr-bg1":
       could not find repository at '...'
```

— nix's flake-source detection saw the `.git/` marker at the repo
root (which is just a stub `README.md`, not a real git repo, because
this tree is actually managed by jj) and tried to use it. Switched
the build URL to `path:$(pwd)` instead of `$(pwd)`, which bypasses
git detection.

Next error (pure-eval):

```
error: access to absolute path '/nix/store/lib' is forbidden in
       pure evaluation mode (use '--impure' to override)
```

— uv2nix tried to resolve fxtr v1's path, ended up with an absolute
path outside the flake's source. With `--impure`:

```
error: opening file '/.file': Permission denied
```

— `--show-trace` revealed the failing call is `src = project.projectRoot`
for `fxtr-0.0.1a0`, and tracing uv2nix's `lock1.nix:507` shows the
issue. The computation is:

```nix
projectRoot = if localPath == "."
              then workspaceRoot
              else workspaceRoot + "/${localPath}";
```

When the flake source is copied to `/nix/store/<hash>-source/`,
workspaceRoot becomes that path. For fxtr (v1), uv.lock records
`editable = "../../../../"` (4 levels up from preload_flake). Then
`projectRoot = /nix/store/<hash>-source/../../../../` which
over-normalizes past `/` (can't go higher) and lands at `/`. uv2nix
then falls through to its "missing pyproject.toml" branch and
projectRoot ends up as `/`. When nix later tries to use `/` as a
derivation `src`, it scans the filesystem root, hits `/.file`
(permission denied at the OS level), errors.

### 2. Non-flake `path:` input pointing at the fxtr tree

Tried adding the fxtr tree as a separate non-flake input so uv2nix's
workspaceRoot could live INSIDE that captured tree, making all path
math resolve to siblings of preload_flake within the captured tree:

```nix
inputs.fxtr-tree = {
  url = "path:../../../..";
  flake = false;
};
# ...
workspace = uv2nix.lib.workspace.loadWorkspace {
  workspaceRoot = "${fxtr-tree-src}/v2/examples/nix_prototype/preload_flake";
};
```

Nix rejected this even with `--impure`:

```
error: relative path '../../../..' points outside of its parent's
       store path '/nix/store/.../source'
```

— nix's flake-input fetcher refuses relative `path:` inputs that
reach outside the flake's source dir. The "outside parent" check
is independent of `--impure`.

### 3. Move the spike outside the fxtr repo + use absolute paths

Moved the whole thing to
`/Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/` and
switched `[tool.uv.sources]` to absolute paths:

```toml
fxtr2 = { path = "/Users/daniel/fxtr-worktrees/fxtr-bg1/v2", editable = true }
fxtr = { path = "/Users/daniel/fxtr-worktrees/fxtr-bg1", editable = true }
```

`uv sync` worked. uv.lock recorded fxtr2 with the absolute path but
**recorded fxtr v1 with a *relative* path** (`../../../../fxtr-worktrees/fxtr-bg1`)
— evidently uv converts to relative when it can compute one,
regardless of how the source was declared in pyproject. (This was
surprising and worth flagging — see "Open uv-side questions" below.)

`nix build --impure` then failed at:

```
error: path '/fxtr-worktrees/fxtr-bg1' does not exist
```

— same root cause as attempt 1 (workspaceRoot + "/" + ".."-relative
path over-normalizes past `/`), but at the new location the
arithmetic resolves to `/fxtr-worktrees/fxtr-bg1` instead of `/`.

For the absolute fxtr2 entry, the breakage is different: uv2nix
computes `workspaceRoot + "/" + "/Users/daniel/..."`, which nix
normalizes by collapsing the double `//` and producing
`<workspaceRoot>/Users/daniel/...` — a nested path that doesn't
exist either. We didn't actually see this error message because
fxtr v1 broke first; we'd hit it next if fxtr v1 was fixed.

## Root cause

uv2nix is architecturally designed for self-contained uv workspaces.
Its `projectRoot` computation is hard-coded to:

```nix
projectRoot = workspaceRoot + "/${localPath}";
```

This produces sane paths only when `localPath` is **relative and
downward** from `workspaceRoot`. Specifically it breaks for:

- Relative paths going up (`../foo/bar`) → over-normalize past `/`.
- Absolute paths (`/Users/...`) → double-slash that collapses to a
  nested non-existent path.

The fxtr2 + fxtr v1 case requires *both* the workspace and the deps
to live under one common ancestor that's the uv2nix workspace root.

## Options forward

### A. `pkgs.uv` inside the derivation, skip uv2nix entirely

Have the flake declare `pkgs.uv + pkgs.python312` as `nativeBuildInputs`
and the build phase runs `uv sync --frozen` then the script.

Pros:
- Sidesteps all the path-resolution issues.
- Works regardless of where the spike lives.
- FOD has network access, so `uv sync` (which fetches PyPI deps + reads
  local path-deps) works.

Cons:
- Pure derivations can't reach the network, so the augment step would
  also need to be an FOD (sacrificing the "augment is sandboxed"
  demonstration).
- Less idiomatic nix; consumers can't easily reuse the python set.
- Each derivation builds the whole venv from scratch (no nix
  caching of intermediate wheels — unless we cache the uv cache, which
  is hairy).

### B. Hybrid: uv2nix for self-contained deps + hand-written derivations for fxtr2 / fxtr v1

The spike's `pyproject.toml` drops the path-deps to fxtr2 / fxtr v1.
uv2nix only sees the spike's own packages + PyPI deps. We write
`buildPythonPackage` derivations for fxtr2 and fxtr v1 (taking their
sources from `/Users/daniel/.../v2` and `/Users/daniel/.../` as
absolute paths under `--impure`), and inject them into the python set
via `overrideScope`.

Pros:
- Keeps uv2nix for the bulk of the dep graph.
- Pure-evaluation-clean for the spike side; only the fxtr2/fxtr v1
  paths require `--impure`.
- Reasonably idiomatic — buildPythonPackage is the canonical nix
  way to package Python.

Cons:
- ~50–100 lines of hand-written nix per external dep. fxtr v1 has its
  own dep tree (msgspec, anyio, etc.) that we'd need to enumerate
  or pull from existing nixpkgs.
- The spike's pyproject would lie about its deps (uv would refuse to
  install without fxtr2 listed). Probably needs a conditional or
  separate "nix-build" pyproject.

### C. Single flake at a level containing everything

Put one flake at `v2/` or the repo root. Single uv workspace. Path-deps
all "downward" from the flake.

Pros:
- Uses uv2nix idiomatically. Path-resolution Just Works.
- Easiest to reason about.

Cons:
- Sacrifices the spike-outside-repo structure we wanted.
- The "two separate flakes (preload + augment)" aesthetic collapses
  to "one flake, multiple outputs" — augment depends on preload's
  output via a regular nix dep rather than a flake-input dep.
- Adds a flake.nix to the fxtr repo (mild repo-structure impact).

### D. Git URL-based path deps

Depend on fxtr2 and fxtr v1 via `fxtr = { git = "file:///abs/path", ... }`
or similar. uv treats git URLs as non-path sources and (presumably)
clones them into its cache; uv2nix would see them as wheel-or-sdist
deps. Nix's flake input system has explicit git support, so the
clone would happen inside the pure-eval boundary.

Open questions:
- Does uv work with `git = "file://..."` URLs? Probably, but the
  `.git/` here is a fake stub, not a real git repo, so the clone
  would fail. Would need to set up a real git repo for fxtr first
  (or use jj-git interop).
- How does uv2nix consume git-source deps? It might just treat them
  as PyPI-equivalent and build from the git-fetched source. Would
  need to verify.
- This makes iteration harder — every edit to fxtr2 needs a git
  commit before the spike sees it. Could use a worktree or
  symlink-y workaround.

### E. Build fxtr2 + fxtr v1 wheels separately, consume via a local find-links URL

Build wheels manually (e.g., `uv build --wheel` inside v2/ and the
repo root), put them in a known location, configure uv to use that
location as a find-links source. uv would treat them as PyPI-style
deps; uv2nix would build them normally.

Pros: works around all path-related issues.
Cons: every fxtr2/fxtr v1 edit needs a wheel rebuild. Worse iteration
loop than option D.

### F. Publish fxtr2 / fxtr v1 to a real package index

Not appropriate at this stage.

## Open uv-side questions

- **Why does uv lock fxtr v1 with a relative path** when our pyproject
  explicitly specified an absolute path? In the moved-out spike, our
  pyproject had `fxtr = { path = "/Users/daniel/...", editable = true }`
  but the lock recorded `editable = "../../../../fxtr-worktrees/fxtr-bg1"`.
  This is uv being "helpful" by relativizing — but the lock-file
  representation is what uv2nix consumes, and the relativization is
  what breaks uv2nix's path math.
- Is there a uv option to keep paths absolute in the lock? Worth a
  short investigation before committing to option B/D/E.

## Where state lives right now

- `/Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/`:
  the moved-out spike. Python parts work via plain `uv run`. flake.nix
  is currently configured for option A (uv-in-derivation) but unused
  — we backed out before running it.
- `/Users/daniel/fxtr-worktrees/fxtr-bg1/v2/examples/nix_prototype/`:
  removed (was the original location).
- `part2_nix_exploration.md`: the original plan, still describing
  the never-implemented uv2nix-based flow. Status note added at the
  top pointing here.

## Suggested next move

Given how much time the nix wrangling has cost relative to its value
as a prototype, my lean is:

1. **Investigate the uv lock-relativization question first** — if
   there's a uv setting (or a small upstream patch) that keeps
   absolute paths in the lock, the original `examples/nix_prototype`
   layout might Just Work with `--impure`. That's the smallest
   possible fix.
2. **If not**, switch to **option A (uv-in-derivation)** for the
   prototype. It's the pragmatic path to a working demo, even if
   not maximally idiomatic. Plan to revisit toward option B or
   option C once the demo's value is established and we know what
   nix patterns we actually want long-term.
