"""`fxtr_nix update-hashes` — the runner-owned FOD relock engine (manager tier).

Keeps a flake's ``fod_lock.toml`` current. A flake declares its hash-locked
fixed-output derivations under ``fxtr.preloads.<lockKey> = <drv>;`` and reads
each one's pinned ``outputHash`` via ``lib.fxtr.fodLockReader``. This engine
decides which slots need (re)locking, drives the builds, captures the real
hashes from the ``hash mismatch`` errors, and rewrites the lock.

**Incremental by default.** The default pass relocks only *new* slots (declared
in ``fxtr.preloads`` but absent from the lock) and drops *stale* ones (in the
lock but no longer declared); already-locked slots are left untouched. It does
**not** try to detect that an existing slot's inputs changed in place — use
``--all`` to re-lock every slot, ``-k/--key`` to re-lock a chosen subset, or
``nix build --check`` to verify a slot's content reproduces. ``--dry-run``
reports the plan (new / stale) without building or writing.

This module imports fxtr2 (via :mod:`.manager`) — manager tier; import it
explicitly, not from the package root.
"""

from __future__ import annotations

import enum
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from fxtr_nix_runner import _fod_lock
from fxtr_nix_runner._backend import Backend
from fxtr_nix_runner._config import NixRuntimeConfig
from fxtr_nix_runner._fod_lock import LOCK_FILENAME, parse_mismatches, sentinel_for
from fxtr_nix_runner.manager import BuildSpan, FlakeManager

#: Flake attribute listing hash-locked slots: ``fxtr.preloads.<lockKey> = <drv>``.
PRELOADS_ATTR = "fxtr.preloads"
# All targeted FODs are independent leaves, so one --keep-going round normally
# captures every mismatch; the loop is just belt-and-suspenders.
_MAX_ROUNDS = 5


class Mode(enum.Enum):
    """Which slots ``update-hashes`` considers for relocking."""

    DEFAULT = "default"  # new slots only (+ drop stale)
    ALL = "all"  # every declared preload slot
    KEYS = "keys"  # an explicit subset (must be declared in fxtr.preloads)


@dataclass
class UpdatePlan:
    """What an update pass did (or, for ``--dry-run``, would do)."""

    relock: list[str] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    #: key -> why it's being relocked ("new" / "--all" / "requested").
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.relock or self.drop)


def plan_relock(
    mode: Mode,
    requested: set[str],
    preload_keys: set[str],
    locked_keys: set[str],
) -> UpdatePlan:
    """Pure target-set decision (no nix) — which slots relock, which drop, why."""
    drop = sorted(locked_keys - preload_keys)

    reasons: dict[str, str] = {}
    if mode is Mode.ALL:
        for k in sorted(preload_keys):
            reasons[k] = "--all"
    elif mode is Mode.KEYS:
        bad = requested - preload_keys
        if bad:
            raise ValueError(f"keys not declared in {PRELOADS_ATTR}: {sorted(bad)}")
        for k in sorted(requested):
            reasons[k] = "requested"
    else:  # DEFAULT — new slots only
        for k in sorted(preload_keys - locked_keys):
            reasons[k] = "new"

    relock = sorted(reasons)
    return UpdatePlan(
        relock=relock,
        drop=drop,
        unchanged=sorted(preload_keys - set(relock)),
        reasons=reasons,
    )


def _nix_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def _ensure_lock_tracked(repo: Path, lock_path: Path) -> None:
    """Stage ``fod_lock.toml`` if it isn't git-tracked yet, so nix's dirty git
    tree sees it (nix only reads git-tracked files for a git flake — just like
    ``flake.lock``, which nix auto-stages). One-time: once tracked, later in-place
    rewrites are picked up without re-staging. No-op outside a git work tree (a
    ``path:`` flake already sees untracked files) or if already tracked."""
    if not lock_path.exists():
        return
    try:
        tracked = await anyio.run_process(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", LOCK_FILENAME],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if tracked.returncode == 0:
            return
        await anyio.run_process(
            ["git", "-C", str(repo), "add", "--", LOCK_FILENAME],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        pass  # no git / not a git repo — path:-style flakes don't need staging


async def _eval_preload_keys(span: BuildSpan, ref: str) -> list[str]:
    """The slot keys under ``fxtr.preloads`` (``attrNames`` — values not forced,
    so a missing/incomplete lock never throws here)."""
    res = await span.run_nix(
        ["eval", f"{ref}#{PRELOADS_ATTR}", "--apply", "builtins.attrNames", "--json"]
    )
    if res.returncode != 0:
        raise RuntimeError(f"failed to list {PRELOADS_ATTR} (is it declared?):\n{res.stderr}")
    return list(json.loads(res.stdout))


async def update_hashes(
    repo: Path | str,
    *,
    mode: Mode = Mode.DEFAULT,
    keys: list[str] | None = None,
    dry_run: bool = False,
    backend: Backend | None = None,
    config: NixRuntimeConfig | None = None,
) -> UpdatePlan:
    """Relock a flake's ``fod_lock.toml`` (see module docstring). Returns the plan.

    Operates on the **live workdir** (``rev=None``) so it reads and rewrites the
    lock in place between build rounds.
    """
    repo = Path(repo)
    lock_path = repo / LOCK_FILENAME
    lock = _fod_lock.read_lock(lock_path)
    requested = set(keys or [])

    async with FlakeManager(repo, backend=backend, config=config) as mgr:
        async with mgr.build_span() as span:
            ref = span.handle.flake_ref_root()

            preload_keys = set(await _eval_preload_keys(span, ref))
            plan = plan_relock(mode, requested, preload_keys, set(lock.hashes))
            if dry_run:
                return plan

            for k in plan.drop:
                lock.hashes.pop(k, None)

            if plan.relock:
                await _relock(span, ref, lock, lock_path, plan.relock)

            _fod_lock.write_lock(lock_path, lock)
            return plan


async def _relock(
    span: BuildSpan, ref: str, lock: _fod_lock.FodLock, lock_path: Path, relock: list[str]
) -> None:
    """Sentinel the targeted slots, build them, and write back the real hashes.

    Writes a distinct sentinel per slot so a single ``--keep-going`` build
    mismatches them all in one round; maps each mismatch back to its slot via the
    sentinel and records the produced (`got:`) hash.
    """
    sentinel_to_key: dict[str, str] = {}
    for k in relock:
        s = sentinel_for(k)
        lock.hashes[k] = s
        sentinel_to_key[s] = k
    _fod_lock.write_lock(lock_path, lock)  # the build reads hashes from here
    await _ensure_lock_tracked(lock_path.parent, lock_path)

    remaining = set(relock)
    for _ in range(_MAX_ROUNDS):
        targets = [f"{ref}#{PRELOADS_ATTR}.{_nix_str(k)}" for k in sorted(remaining)]
        res = await span.run_nix(["build", "--no-link", "--keep-going", *targets])
        if res.returncode == 0:
            break  # nothing mismatched — already current (unexpected under sentinels)
        got = {
            sentinel_to_key[m.specified]: m.got
            for m in parse_mismatches(res.stderr)
            if m.specified in sentinel_to_key
        }
        if not got:
            raise RuntimeError(f"build failed without a parseable hash mismatch:\n{res.stderr}")
        for k, h in got.items():
            lock.hashes[k] = h
            remaining.discard(k)
        _fod_lock.write_lock(lock_path, lock)
        if not remaining:
            break
    if remaining:
        raise RuntimeError(f"could not relock {sorted(remaining)} after {_MAX_ROUNDS} build rounds")
