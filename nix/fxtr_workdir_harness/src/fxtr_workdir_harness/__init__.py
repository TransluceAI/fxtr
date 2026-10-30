"""Build a throwaway workdir repo from an in-repo template, pinned to a
library rev — the shared test/scaffold harness for the git-ref code path.

Why this exists
---------------
The git-referenced service/preset path builds **`--no-update-lock-file`** against a
*committed* workdir repo whose `flake.lock` pins its `fxtr` input to a
*committed* library rev. nix's git fetcher only sees committed tree
content at a rev, so the library under test must be reachable as a git
object — intrinsic to "shipped code is pinned." Design + rationale:
``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage3_bundle_and_spawner.md``
("Test & scaffold workdir setup").

`prepare_workdir_repo` makes that ergonomic for tests (and, later, the
workdir scaffold script) without baking machine-specific paths into the
source tree, assuming a particular VCS, or mutating any source history:

1. Copy an in-repo template directory into a fresh `dest`.
2. **Substitute** the library ref across marked lines (see "Markers").
3. `uv lock` each substituted guest package (uv runs on the host — uv isn't
   nix), then `git init` + `git add -A` so the copy is a git repo.
4. `nix flake lock` each substituted workdir flake **through the configured
   `fxtr_nix_runner` backend** (host → host nix; podman → in-podman, *no host
   nix*), then stage the freshly written `flake.lock`(s) and commit.
5. Return the path + captured HEAD.

The flake is locked **after** `git init` + `git add -A` on purpose: that
mirrors production, where `fxtr_nix_runner`'s `Handle.lock` (the `nix_lock`
flow) always runs against a git workdir, never a plain directory. nix reads
the git index, so the staged-but-not-yet-committed `flake.nix` is visible
(it warns "Git tree is dirty" and locks anyway). Because the lock goes
through `resolve_backend()` (auto-configured from `FXTR_NIX_BACKEND` /
`FXTR_NIX_RUNNER_CONFIG`), a podman-backed run produces the lock with no
host nix at all — which is what makes the downstream E2E suites host-nix-free.
This is why :func:`prepare_workdir_repo` is **async**.

The library repo is only ever *read* (as a flake input / uv git source);
this helper never touches its history.

Resolving the library ref
--------------------------
The library is identified as ``(lib_repo, lib_rev)`` — a local git repo at
a rev — and *both* the flake input URL and the uv git-source derive from
it, so they pin the identical rev with no fragile flake-ref parsing.

- ``lib_repo`` default: the fxtr repo root (walk up for ``flake.nix``);
  override via the ``lib_repo`` arg or the ``FXTR_TEST_LIB_REPO`` env var.
- ``lib_rev`` default: ``git rev-parse HEAD`` of ``lib_repo`` — so in CI
  (plain git checkout) it's the commit under test, nothing jj-specific;
  override via the ``lib_rev`` arg or the ``FXTR_TEST_LIB_REV`` env var
  (e.g. to iterate against a specific uncommitted-work rev locally).

When the rev is resolved from HEAD (no explicit rev) and the repo has
uncommitted changes, a stderr warning notes that the build uses committed
HEAD and ignores working-tree edits.

Working from a jj workspace
---------------------------
Whenever the resolved repo root has a ``.jj`` directory, resolution
takes the jj path — jj wins over git even in colocated checkouts
(where it upgrades HEAD-plus-dirty-warning to true working-copy
semantics) and is not fooled by decoy ``.git`` dirs kept around for
gitignore-respecting tools. In a **non-colocated jj workspace** (a
``jj workspace add`` checkout with no real ``.git`` of its own) two
things would otherwise bite:

- Plain git commands fail at the workspace root (there is no ``.git``)
  — the harness instead resolves the backing git object store through
  ``.jj/repo`` (a redirect file in secondary workspaces) and the
  store's ``git_target``.
- The rev must be **reachable from a git ref**, not just
  object-present: uv git sources and nix git flake refs fetch by
  walking advertised refs (branches / tags / HEAD), and jj-workspace
  commits hang off none of them until a bookmark is exported. Instead
  of requiring the manual ``jj bookmark create`` + ``jj git export``
  dance, the harness pushes the rev from the backing store into a
  harness-owned bare **mirror** repo
  (``~/.cache/fxtr-workdir-harness/lib-mirror.git``; override with
  ``FXTR_TEST_LIB_MIRROR``) under ``refs/heads/r-<rev>``, and pins
  ``LibRef`` to the mirror. The push is incremental (the mirror
  accumulates one branch per tested rev) and never mutates the jj
  repo, its backing store, or any bookmark.

The rev in this mode defaults to the **workspace's working-copy
commit** (``@``): resolving it through ``jj log`` snapshots the working
copy first, so current edits are included — strictly better than the
git path's "committed HEAD + dirty warning". ``FXTR_TEST_LIB_REV`` /
``lib_rev`` still override the rev (it must be object-present in the
backing store; the mirror push transports it the same way).

:func:`resolve_lib_ref` validates rev reachability up front and raises
a ``RuntimeError`` naming the fix, instead of letting the failure
surface as an opaque ``uv lock`` error three layers down.

Markers
-------
Templates stay valid/lockable in place; copies rewrite themselves. Both
nix and TOML use ``#`` comments, so the same toggling works for each.

- ``# fxtr-lib-sub: flake-input`` — the line carries ``@FXTR_LIB_FLAKE@``,
  replaced with the flake URL. (A placeholder is fine: we never lock the
  template's flake in place, only the copy.)
- ``# fxtr-lib-sub: active-in-template`` — an *uncommented* line (the
  relative-path uv source, which lets the template lock against local
  working code); the substitutor **comments it out**.
- ``# fxtr-lib-sub: active-in-workdir`` — a *commented-out* line (the git
  source); the substitutor **uncomments it** and replaces
  ``@FXTR_LIB_URL@`` / ``@FXTR_LIB_REV@``.

In-place repin
--------------
The markers persist into the workdir copy, which is what makes an
existing workdir **re-pinnable in place**: :func:`repin_workdir`
re-resolves the library ref (jj ``@`` → mirror push, exactly like the
scaffold), rewrites the already-substituted values on the marked lines
(the flake input always; the uv git sources only with ``uv=True``, since
renderer-only iteration doesn't need the venvs re-pinned and skipping
them avoids ``uv lock`` + venv rebuilds), relocks what changed, and
commits. The commit sweeps the whole tree (``git add -A``) on purpose —
the gesture is "publish the workdir's current state with the lib pinned
at the current working copy", so pending workdir-local edits (e.g. a
renderer tweak) ride the same commit. CLI:
``python -m fxtr_workdir_harness.repin <workdir> [--uv]``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Generator
from pathlib import Path

from fxtr_nix_runner import resolve_backend

__all__ = [
    "LibRef",
    "PreparedWorkdir",
    "RepinnedWorkdir",
    "prepare_workdir_repo",
    "repin_workdir",
    "resolve_lib_ref",
]

# Marker the substitutor keys on. Kind suffix disambiguates the rewrite.
_MARKER = "fxtr-lib-sub:"
_KIND_FLAKE_INPUT = "flake-input"
_KIND_ACTIVE_IN_TEMPLATE = "active-in-template"
_KIND_ACTIVE_IN_WORKDIR = "active-in-workdir"

# Substitution tokens.
_TOKEN_FLAKE = "@FXTR_LIB_FLAKE@"
_TOKEN_URL = "@FXTR_LIB_URL@"
_TOKEN_REV = "@FXTR_LIB_REV@"

# Files scanned for markers (everything else is copied verbatim). Lockfiles
# are excluded — they're regenerated by the lock step.
_SUBSTITUTABLE_NAMES = ("flake.nix", "pyproject.toml")

# Directory names skipped when copying a template (build junk / VCS state
# that must never leak into the fresh workdir repo).
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".jj", ".venv", "__pycache__", "result", "result-*", ".tmp_outputs"
)

_GIT_IDENTITY = (
    "-c",
    "user.name=fxtr-test",
    "-c",
    "user.email=fxtr-test@example.com",
)


@dataclasses.dataclass(frozen=True)
class LibRef:
    """Resolved fxtr library source: a local git repo at a specific rev."""

    repo_path: Path
    rev: str

    @property
    def flake_url(self) -> str:
        """Nix git flake ref pinning the whole flake (source + its lock)."""
        return f"git+file://{self.repo_path}?rev={self.rev}"

    @property
    def uv_git_url(self) -> str:
        """Git URL for a uv ``[tool.uv.sources]`` entry (rev carried separately)."""
        return f"file://{self.repo_path}"


@dataclasses.dataclass(frozen=True)
class PreparedWorkdir:
    """Result of :func:`prepare_workdir_repo`."""

    path: Path
    head_commit: str
    lib_ref: LibRef


def _git(repo: Path, *args: str, capture: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=capture,
        text=True,
        check=True,
    )
    return (result.stdout or "").strip()


def _find_repo_root(start: Path) -> Path:
    """Nearest ancestor of ``start`` (inclusive) containing a ``flake.nix``."""
    for parent in [start, *start.parents]:
        if (parent / "flake.nix").is_file():
            return parent
    raise RuntimeError(f"no flake.nix found at or above {start}")


def _require_rev_reachable(repo: Path, rev: str) -> None:
    """Fail early, with an actionable message, when ``rev`` can't be
    fetched by the ref-walking consumers downstream (uv git sources +
    nix git flake refs both fetch by walking advertised refs — a
    commit that exists in the object store but hangs off no ref looks
    like "failed to find branch, tag, or commit" to them, three layers
    away from the actual cause).

    The canonical way to hit this: commits made in a non-colocated jj
    workspace, which land in the main repo's object store but aren't
    reachable until a bookmark is exported. See the module docstring's
    "Working from a jj workspace" section.
    """
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    except subprocess.CalledProcessError:
        raise RuntimeError(
            f"prepare_workdir_repo: rev {rev!r} is not a commit in {repo}. "
            "Check FXTR_TEST_LIB_REV, and that FXTR_TEST_LIB_REPO points at "
            "the repo that actually has it."
        ) from None
    # Reachability from what a fetch/clone actually transfers:
    # branches + tags + HEAD. Deliberately NOT `--all` — a colocated jj
    # repo keeps every commit alive via `refs/jj/keep/*`, which makes
    # `--all` a false pass while the consumers' clones (refs/heads/* +
    # tags + HEAD) still miss the objects. Empty output iff reachable.
    unreachable = _git(
        repo, "rev-list", "--max-count=1", rev, "--not", "--branches", "--tags", "HEAD"
    )
    if unreachable:
        raise RuntimeError(
            f"prepare_workdir_repo: rev {rev[:12]} exists in {repo} but is "
            "not reachable from any branch / tag / HEAD, so uv + nix git "
            "fetches of it will fail (they only transfer those refs — "
            "jj's refs/jj/keep/* don't count). If it was committed from a "
            "jj workspace, export a branch ref first:\n"
            f"  jj bookmark create <name> -r {rev[:12]} && jj git export"
        )


def _is_rev_ref_reachable(repo: Path, rev: str) -> bool:
    """Quiet boolean twin of :func:`_require_rev_reachable` (used to skip
    redundant mirror pushes; the loud variant stays the validation)."""
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        unreachable = _git(
            repo, "rev-list", "--max-count=1", rev, "--not", "--branches", "--tags", "HEAD"
        )
    except subprocess.CalledProcessError:
        return False
    return not unreachable


def _jj_backing_git_store(workspace_root: Path) -> Path:
    """Resolve the git object store backing a jj workspace.

    ``.jj/repo`` is a directory in the primary workspace and a redirect
    *file* (containing the primary's ``.jj/repo`` path) in secondary
    workspaces; ``store/git_target`` then locates the git store —
    relative paths are relative to the ``store`` directory (e.g. a
    colocated primary's ``../../../.git``). Repos without a
    ``git_target`` keep their store at ``store/git``.
    """
    jj_repo = workspace_root / ".jj" / "repo"
    if jj_repo.is_file():
        # The redirect path may be absolute or relative to the .jj dir.
        repo_dir = Path(jj_repo.read_text().strip())
        if not repo_dir.is_absolute():
            repo_dir = (workspace_root / ".jj" / repo_dir).resolve()
    elif jj_repo.is_dir():
        repo_dir = jj_repo
    else:
        raise RuntimeError(
            f"prepare_workdir_repo: {workspace_root} has a .jj directory but no "
            ".jj/repo; is it really a jj workspace?"
        )
    store_dir = repo_dir / "store"
    git_target = store_dir / "git_target"
    if git_target.is_file():
        target = Path(git_target.read_text().strip())
        if not target.is_absolute():
            target = (store_dir / target).resolve()
    else:
        target = store_dir / "git"
    if not target.exists():
        raise RuntimeError(
            f"prepare_workdir_repo: resolved jj backing git store {target} "
            f"(via {jj_repo}) does not exist."
        )
    return target


def _jj_working_copy_commit(workspace_root: Path) -> str:
    """The workspace's ``@`` commit id. ``jj log`` snapshots the working
    copy first, so the returned commit includes current edits."""
    result = subprocess.run(
        ["jj", "log", "-r", "@", "--no-graph", "-T", "commit_id"],
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
    )
    rev = (result.stdout or "").strip()
    if result.returncode != 0 or not rev:
        raise RuntimeError(
            f"prepare_workdir_repo: failed to resolve the working-copy commit "
            f"of jj workspace {workspace_root} (is `jj` installed? does the "
            "workspace need `jj workspace update-stale`?):\n"
            f"{(result.stderr or '').strip()}"
        )
    return rev


def _default_mirror_path() -> Path:
    env = os.environ.get("FXTR_TEST_LIB_MIRROR")
    if env:
        return Path(env).resolve()
    return Path.home() / ".cache" / "fxtr-workdir-harness" / "lib-mirror.git"


# Lock paths currently held by this thread (see _flocked). threading.local:
# each thread checks/updates only its own set, so no extra lock is needed
# around the bookkeeping — and asyncio tasks share their thread's set, which
# is exactly what makes the re-entry guard catch task interleaving too.
_held_flocks = threading.local()


@contextlib.contextmanager
def _flocked(lock_path: Path) -> Generator[None]:
    """Exclusive advisory lock on ``lock_path``, safe across processes,
    threads, and asyncio tasks. Load-bearing invariants:

    - **``flock``, not ``fcntl.lockf``.** BSD ``flock`` locks the *open
      file description*; each call here opens a fresh fd, so two threads
      of one process genuinely serialize. POSIX record locks (``lockf``)
      are per-process — a second same-process acquire silently "succeeds"
      with no mutual exclusion, and closing *any* fd to the file drops
      the lock. (Caveat: ``flock`` semantics are murky on NFS; the
      default mirror lives under ``~/.cache``, but keep this in mind if
      ``FXTR_TEST_LIB_MIRROR`` points somewhere exotic.)
    - **Keep the guarded region synchronous** (no ``await`` points): an
      asyncio task inside it blocks its event loop until exit, which is
      what prevents two tasks on one loop from interleaving inside.
    - **Re-entry from the same thread raises** instead of self-deadlocking
      on the fresh-fd ``flock``. Because asyncio tasks share their
      thread's bookkeeping, this also loudly catches the
      accidental-interleave case where a future edit introduces an
      ``await`` inside the region and a second task enters. Genuine
      cross-thread / cross-process contention still blocks until the
      holder releases (wanted under ``pytest -n auto``).
    """
    held: set[Path] = getattr(_held_flocks, "paths", None) or set()
    _held_flocks.paths = held
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = lock_path.resolve()
    if lock_path in held:
        raise RuntimeError(
            f"_flocked: re-entered lock {lock_path} while this thread already "
            "holds it — the nested fresh-fd flock would self-deadlock. If an "
            "await crept into the guarded region, two tasks on one event loop "
            "are interleaving; keep the region synchronous."
        )
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        held.add(lock_path)
        try:
            yield
        finally:
            held.discard(lock_path)
            fcntl.flock(fh, fcntl.LOCK_UN)


def _ensure_rev_in_mirror(store: Path, rev: str, mirror: Path, *, required: bool) -> None:
    """Make ``rev`` ref-reachable in the bare ``mirror`` repo by pushing it
    from ``store`` (works for object-present-but-unreachable commits: push
    packs by walking from the SHA, no source ref needed).

    Incremental: one ``refs/heads/r-<rev>`` branch per rev; already-pushed
    history is negotiated away. The mirror's HEAD is pointed at the new
    branch so it always resolves (an unborn HEAD would trip the
    reachability checks and plain clones). flock-guarded — test runs are
    routinely parallel (`pytest -n auto`).

    With ``required=False`` (mechanics-only callers that never fetch), a
    failed push degrades to a stderr warning.
    """
    with _flocked(mirror.parent / (mirror.name + ".lock")):
        if not (mirror / "HEAD").exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "--bare", "--quiet", str(mirror)], check=True)
        if _is_rev_ref_reachable(mirror, rev):
            return
        try:
            _git(store, "push", "--quiet", str(mirror), f"{rev}:refs/heads/r-{rev}")
            _git(mirror, "symbolic-ref", "HEAD", f"refs/heads/r-{rev}")
        except subprocess.CalledProcessError as exc:
            if required:
                raise RuntimeError(
                    f"prepare_workdir_repo: failed to push rev {rev!r} from the "
                    f"jj backing git store {store} into the lib mirror {mirror}. "
                    "Check FXTR_TEST_LIB_REV (the rev must be object-present in "
                    f"the backing store):\n{(exc.stderr or '').strip()}"
                ) from None
            print(
                f"[prepare_workdir_repo] WARNING: could not push rev {rev[:12]} "
                f"into the lib mirror {mirror}; continuing because this caller "
                "never fetches (validate=False).",
                file=sys.stderr,
            )


def _resolve_jj_workspace_ref(
    workspace_root: Path, *, rev_override: str | None, validate: bool
) -> LibRef:
    """jj-workspace mode of :func:`resolve_lib_ref` — see the module
    docstring's "Working from a jj workspace" section."""
    store = _jj_backing_git_store(workspace_root)
    rev = rev_override or _jj_working_copy_commit(workspace_root)
    mirror = _default_mirror_path()
    _ensure_rev_in_mirror(store, rev, mirror, required=validate)
    print(
        f"[prepare_workdir_repo] jj workspace {workspace_root}: using "
        f"{'working-copy commit' if rev_override is None else 'rev'} {rev[:12]} "
        f"via lib mirror {mirror}",
        file=sys.stderr,
    )
    if validate:
        _require_rev_reachable(mirror, rev)
    return LibRef(repo_path=mirror, rev=rev)


def resolve_lib_ref(
    *,
    lib_repo: Path | None = None,
    lib_rev: str | None = None,
    validate: bool = True,
) -> LibRef:
    """Resolve the ``(repo, rev)`` library reference.

    Precedence per field: explicit arg > env var > default. The default
    repo is the fxtr repo root (walked up from this module); the default
    rev is that repo's current ``HEAD``. Emits a stderr warning when the
    rev is taken from HEAD and the repo has uncommitted changes.

    When the resolved repo (explicit or default) has a ``.jj`` directory,
    resolution switches to jj-workspace mode (jj wins over git): the rev
    defaults to the workspace's working-copy commit (``@``), and the
    returned ``LibRef`` points at a harness-owned bare mirror the rev was
    pushed into — see the module docstring's "Working from a jj
    workspace" section.

    With ``validate=True`` (the default), checks up front that the
    resolved rev exists in ``repo`` and is reachable from a
    **branch / tag / HEAD** there (raising ``RuntimeError`` with the fix
    otherwise) — see the module docstring's "Working from a jj
    workspace" section for why object-present isn't enough.
    ``validate=False`` skips the rev checks for mechanics-only callers
    that never lock or build (paired with ``prepare_workdir_repo``'s
    ``relock=False``).
    """
    repo_override = lib_repo if lib_repo is not None else os.environ.get("FXTR_TEST_LIB_REPO")
    repo = (
        Path(repo_override).resolve()
        if repo_override
        else _find_repo_root(Path(__file__).resolve())
    )

    rev_override = lib_rev if lib_rev is not None else os.environ.get("FXTR_TEST_LIB_REV")

    # jj wins over git when both are present: colocated checkouts get the
    # better @-semantics too, and a decoy `.git` (kept around to make
    # gitignore-respecting tools behave in non-colocated workspaces)
    # doesn't fool the dispatch.
    if (repo / ".jj").exists():
        return _resolve_jj_workspace_ref(repo, rev_override=rev_override, validate=validate)

    if rev_override:
        rev = rev_override
    else:
        try:
            rev = _git(repo, "rev-parse", "HEAD")
        except subprocess.CalledProcessError:
            raise RuntimeError(
                f"prepare_workdir_repo: {repo} is not a git repository (and has "
                "no .jj for the jj-workspace fallback), so the library rev "
                "can't default to its HEAD. Point FXTR_TEST_LIB_REPO at a git "
                "repo (or a jj workspace) that has the code under test."
            ) from None
        dirty = _git(repo, "status", "--porcelain")
        if dirty:
            print(
                f"[prepare_workdir_repo] WARNING: {repo} has uncommitted changes; "
                f"the build uses committed HEAD ({rev[:12]}) and will ignore "
                "working-tree edits. Commit them, or set FXTR_TEST_LIB_REV / "
                "FXTR_TEST_LIB_REPO to pin a different source.",
                file=sys.stderr,
            )
    if validate:
        _require_rev_reachable(repo, rev)
    return LibRef(repo_path=repo, rev=rev)


def _toggle_comment(line: str, *, comment: bool) -> str:
    """Comment or uncomment a line, preserving leading indentation.

    Both nix and TOML use ``#``. Idempotent: commenting an already-commented
    line (or uncommenting an already-uncommented one) is a no-op.
    """
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    is_commented = stripped.startswith("#")
    if comment:
        return line if is_commented else f"{indent}# {stripped}"
    if not is_commented:
        return line
    # Drop the leading "#" plus one optional space.
    body = stripped[1:]
    if body.startswith(" "):
        body = body[1:]
    return f"{indent}{body}"


def _substitute_line(line: str, lib_ref: LibRef) -> str:
    if f"{_MARKER} {_KIND_FLAKE_INPUT}" in line:
        return line.replace(_TOKEN_FLAKE, lib_ref.flake_url)
    if f"{_MARKER} {_KIND_ACTIVE_IN_TEMPLATE}" in line:
        return _toggle_comment(line, comment=True)
    if f"{_MARKER} {_KIND_ACTIVE_IN_WORKDIR}" in line:
        line = _toggle_comment(line, comment=False)
        return line.replace(_TOKEN_URL, lib_ref.uv_git_url).replace(_TOKEN_REV, lib_ref.rev)
    return line


def _substitute_text(text: str, lib_ref: LibRef) -> tuple[str, bool]:
    """Return ``(rewritten_text, changed)``."""
    out_lines = [_substitute_line(line, lib_ref) for line in text.splitlines(keepends=True)]
    rewritten = "".join(out_lines)
    return rewritten, rewritten != text


def _substitute_tree(root: Path, lib_ref: LibRef) -> list[Path]:
    """Substitute markers in every ``flake.nix`` / ``pyproject.toml`` under
    ``root``. Returns the files that changed."""
    changed: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name not in _SUBSTITUTABLE_NAMES:
            continue
        text = path.read_text()
        rewritten, did_change = _substitute_text(text, lib_ref)
        if did_change:
            path.write_text(rewritten)
            changed.append(path)
    return changed


async def prepare_workdir_repo(
    template_dir: Path,
    dest: Path,
    *,
    lib_repo: Path | None = None,
    lib_rev: str | None = None,
    on_branch: bool = True,
    relock: bool = True,
) -> PreparedWorkdir:
    """Materialize a committed workdir repo from ``template_dir``.

    Async because the flake lock runs through ``fxtr_nix_runner``'s backend
    (see the module docstring's lock-step notes) — ``await`` it.

    Args:
        template_dir: In-repo template (e.g. an ``agent_workdir/`` or
            ``sandbox_smoke/``). Copied verbatim except build/VCS junk.
        dest: Target directory; must not already exist. Caller owns its
            lifetime (pass a pytest ``tmp_path`` subdir).
        lib_repo / lib_rev: Override the resolved library source; see
            :func:`resolve_lib_ref`.
        on_branch: When ``False``, leave the workdir commit reachable by no
            branch (detached HEAD, branch ref dropped) so a consumer must
            resolve ``?rev=`` for an off-branch object.
        relock: Test-only escape. When ``False``, skip ``uv lock`` /
            ``nix flake lock`` (the result is **not** buildable, but the
            copy + substitution + git mechanics are exercised), and also
            skip the rev-reachability validation (mechanics tests pass
            placeholder revs). Real use always locks — and validates.

    Returns:
        :class:`PreparedWorkdir` with the workdir path + committed HEAD.
    """
    template_dir = Path(template_dir)
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f"dest already exists: {dest}")

    lib_ref = resolve_lib_ref(lib_repo=lib_repo, lib_rev=lib_rev, validate=relock)

    shutil.copytree(template_dir, dest, ignore=_COPY_IGNORE)
    changed = _substitute_tree(dest, lib_ref)

    # `uv lock` (host-side; uv isn't nix) before `git init` so the lockfiles
    # are part of the first `add`.
    if relock:
        for pkg_dir in sorted({p.parent for p in changed if p.name == "pyproject.toml"}):
            subprocess.run(["uv", "lock"], cwd=str(pkg_dir), check=True)

    # Make the workdir a git repo *before* locking the flake — see the module
    # docstring. `git add -A` stages the substituted flake.nix so nix sees it
    # (via the index) even though nothing is committed yet.
    _git(dest, "init", "-b", "main", capture=False)
    _git(dest, "add", "-A", capture=False)

    if relock:
        flake_dirs = sorted({p.parent for p in changed if p.name == "flake.nix"})
        await _lock_flakes(flake_dirs)
        _git(dest, "add", "-A", capture=False)  # stage the freshly written flake.lock(s)

    _git(dest, *_GIT_IDENTITY, "commit", "-m", "workdir snapshot", capture=False)
    head = _git(dest, "rev-parse", "HEAD")

    if not on_branch:
        # Detach HEAD at the commit, then drop the branch ref so the commit
        # is reachable by no branch (only HEAD / reflog keep it alive).
        _git(dest, "checkout", "--detach", capture=False)
        _git(dest, "branch", "-D", "main", capture=False)

    return PreparedWorkdir(path=dest, head_commit=head, lib_ref=lib_ref)


@dataclasses.dataclass(frozen=True)
class RepinnedWorkdir:
    """Result of :func:`repin_workdir`."""

    path: Path
    head_commit: str
    lib_ref: LibRef
    #: False when the workdir was already pinned to the resolved rev AND
    #: the tree had nothing else to commit — no commit was made.
    changed: bool


def _repin_line(line: str, lib_ref: LibRef, *, uv: bool) -> str:
    """The in-place analog of :func:`_substitute_line`.

    Handles both forms a marked line can be in: the template's token
    form (delegated to the scaffold substitution — so repinning a
    freshly copied template degenerates to the scaffold rewrite) and
    the workdir's already-substituted form, whose values are replaced
    by position (the first quoted string of a flake-input line; the
    ``git = "…"`` / ``rev = "…"`` values of an active-in-workdir
    line). ``active-in-template`` lines stay commented out. With
    ``uv=False`` the uv-source lines are left untouched entirely.
    """
    if f"{_MARKER} {_KIND_FLAKE_INPUT}" in line:
        if _TOKEN_FLAKE in line:
            return _substitute_line(line, lib_ref)
        return re.sub(r'"[^"]*"', f'"{lib_ref.flake_url}"', line, count=1)
    if f"{_MARKER} {_KIND_ACTIVE_IN_WORKDIR}" in line and uv:
        if _TOKEN_URL in line or _TOKEN_REV in line:
            return _substitute_line(line, lib_ref)
        line = re.sub(r'git = "[^"]*"', f'git = "{lib_ref.uv_git_url}"', line, count=1)
        return re.sub(r'rev = "[^"]*"', f'rev = "{lib_ref.rev}"', line, count=1)
    return line


async def repin_workdir(
    workdir: Path,
    *,
    lib_repo: Path | None = None,
    lib_rev: str | None = None,
    uv: bool = False,
    relock: bool = True,
) -> RepinnedWorkdir:
    """Re-pin an existing (scaffolded) workdir's fxtr lib ref in place.

    The dev-loop half of the module docstring's "In-place repin"
    section: resolves the library ref exactly like
    :func:`prepare_workdir_repo` (in a jj workspace, the working-copy
    commit ``@`` via the mirror — current edits included), rewrites the
    marked lines, relocks what changed, and commits **the whole tree**
    (pending workdir-local edits deliberately ride along).

    Args:
        workdir: A scaffolded workdir git repo (as produced by
            :func:`prepare_workdir_repo`). Must contain a marked
            ``flake.nix``.
        lib_repo / lib_rev: Override the resolved library source; see
            :func:`resolve_lib_ref`.
        uv: Also re-pin the ``active-in-workdir`` uv git sources and
            re-run ``uv lock`` for their packages. Off by default —
            renderer iteration only needs the flake input, and skipping
            the uv side avoids venv rebuilds. Turn it on after editing
            library *Python* the workdir venvs consume.
        relock: Test-only escape, as in :func:`prepare_workdir_repo`:
            ``False`` skips ``uv lock`` / ``nix flake lock`` and the
            rev-reachability validation (the result is not buildable;
            the rewrite + git mechanics are exercised).

    Returns:
        :class:`RepinnedWorkdir` with the (possibly new) HEAD. When
        nothing changed — already pinned to the resolved rev, clean
        tree — no commit is made and ``changed`` is False.

    Raises:
        RuntimeError: ``workdir`` is not a git repo, or has no marked
            ``flake.nix`` anywhere (not a scaffolded workdir).
    """
    workdir = Path(workdir)
    try:
        _git(workdir, "rev-parse", "--git-dir")
    except subprocess.CalledProcessError:
        raise RuntimeError(f"repin_workdir: {workdir} is not a git repository") from None

    lib_ref = resolve_lib_ref(lib_repo=lib_repo, lib_rev=lib_rev, validate=relock)

    changed: list[Path] = []
    saw_flake_marker = False
    for path in sorted(workdir.rglob("*")):
        if not path.is_file() or path.name not in _SUBSTITUTABLE_NAMES:
            continue
        if ".git" in path.parts:
            continue
        text = path.read_text()
        if path.name == "flake.nix" and f"{_MARKER} {_KIND_FLAKE_INPUT}" in text:
            saw_flake_marker = True
        out_lines = [_repin_line(line, lib_ref, uv=uv) for line in text.splitlines(keepends=True)]
        rewritten = "".join(out_lines)
        if rewritten != text:
            path.write_text(rewritten)
            changed.append(path)
    if not saw_flake_marker:
        raise RuntimeError(
            f"repin_workdir: no marked flake.nix under {workdir} — is this a "
            "scaffolded workdir (prepare_workdir_repo output)?"
        )

    if relock:
        for pkg_dir in sorted({p.parent for p in changed if p.name == "pyproject.toml"}):
            subprocess.run(["uv", "lock"], cwd=str(pkg_dir), check=True)

    _git(workdir, "add", "-A", capture=False)
    if relock:
        flake_dirs = sorted({p.parent for p in changed if p.name == "flake.nix"})
        if flake_dirs:
            await _lock_flakes(flake_dirs)
            _git(workdir, "add", "-A", capture=False)

    # Anything staged? (Covers both the rewrite and pending local edits.)
    staged = _git(workdir, "status", "--porcelain")
    if staged:
        _git(
            workdir,
            *_GIT_IDENTITY,
            "commit",
            "-m",
            f"repin fxtr lib to {lib_ref.rev[:12]}",
            capture=False,
        )
    head = _git(workdir, "rev-parse", "HEAD")
    return RepinnedWorkdir(path=workdir, head_commit=head, lib_ref=lib_ref, changed=bool(staged))


async def _lock_flakes(flake_dirs: list[Path]) -> None:
    """``nix flake lock`` each substituted workdir flake through the configured
    ``fxtr_nix_runner`` backend (host → host nix; podman → in-podman, no host
    nix). Auto-configured from the environment (``FXTR_NIX_BACKEND`` /
    ``FXTR_NIX_RUNNER_CONFIG``)."""
    backend = resolve_backend()
    for flake_dir in flake_dirs:
        async with backend.builder_for(flake_dir) as handle:
            await handle.lock()
