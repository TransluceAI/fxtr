"""`fod_lock.toml` read/write + sentinel hashes + hash-mismatch parsing.

Transport tier (**no fxtr2**): the pure, independently-testable pieces of the
FOD lock that the manager-tier engine (:mod:`fxtr_nix_runner.update_hashes`)
drives. The lock file pins every fixed-output derivation's `outputHash` by slot
key — exactly what the nix-side `lib.fxtr.fodLockReader` reads back.

File shape (TOML, pretty + sorted)::

    format_version = 1

    [hashes]
    greeting = "sha256-…"
    "data/models:encoder" = "sha256-…"
"""

from __future__ import annotations

import base64
import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

#: Lock file name, resolved relative to the flake root.
LOCK_FILENAME = "fod_lock.toml"
FORMAT_VERSION = 1

# Domain-separated sentinel prefix; bumped if the scheme ever changes.
_SENTINEL_DOMAIN = b"fxtr-fod-lock-sentinel:v1:"


@dataclass
class FodLock:
    """In-memory view of a ``fod_lock.toml`` (an empty one for a missing file)."""

    hashes: dict[str, str] = field(default_factory=dict)
    format_version: int = FORMAT_VERSION


def read_lock(path: Path) -> FodLock:
    """Read a lock file; a missing file is an empty lock (the first-run case)."""
    if not path.exists():
        return FodLock()
    data = tomllib.loads(path.read_text())
    return FodLock(
        hashes=dict(data.get("hashes", {})),
        format_version=int(data.get("format_version", FORMAT_VERSION)),
    )


def write_lock(path: Path, lock: FodLock) -> None:
    """Write a lock file: the ``[hashes]`` table, keys sorted."""
    doc = tomlkit.document()
    doc["format_version"] = lock.format_version

    hashes = tomlkit.table()
    for key in sorted(lock.hashes):
        hashes[key] = lock.hashes[key]
    doc["hashes"] = hashes

    path.write_text(tomlkit.dumps(doc))


def sentinel_for(key: str) -> str:
    """A distinct, valid sha256 SRI for a slot key.

    Any 32 bytes is a valid sha256, so we mint a per-key sentinel
    (domain-separated hash of the key) and write it as the slot's `outputHash`
    before building. Each resulting `hash mismatch` error's `specified:` hash is
    then this sentinel — the caller holds a ``{sentinel: key}`` map to recover
    which slot a mismatch (and its `got:` hash) belongs to. Distinct per key, so
    a single ``--keep-going`` build that mismatches every targeted slot is
    unambiguous.
    """
    digest = hashlib.sha256(_SENTINEL_DOMAIN + key.encode()).digest()
    return "sha256-" + base64.b64encode(digest).decode()


@dataclass(frozen=True)
class HashMismatch:
    """One FOD `hash mismatch` error: the declared (sentinel) vs produced hash."""

    specified: str
    got: str


_MISMATCH_MARKER = "hash mismatch in fixed-output derivation"
_HASH_RE = re.compile(r"(specified|got):\s*(sha256-[A-Za-z0-9+/=]+)")


def parse_mismatches(stderr: str) -> list[HashMismatch]:
    """Extract every FOD hash-mismatch (`specified:`/`got:`) pair from nix stderr.

    Splits on the per-error marker so a ``nix build --keep-going`` log carrying
    several mismatches yields one entry each; tolerant of label order within a
    block. Pins the current nix message shape — a fixture test guards against an
    upstream format change going silently unparsed.
    """
    out: list[HashMismatch] = []
    starts = [m.start() for m in re.finditer(re.escape(_MISMATCH_MARKER), stderr)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(stderr)
        found = dict(_HASH_RE.findall(stderr[start:end]))
        if "specified" in found and "got" in found:
            out.append(HashMismatch(specified=found["specified"], got=found["got"]))
    return out
