"""Fetch wheel files by (url, sha256).

The cache is keyed on ``sha256[:16] + filename`` so two wheels with the
same URL but different content (shouldn't happen for honest publishers)
still get separate cache entries. Hashes are verified on both first
download and cache hit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

import httpx


def fetch_wheels(wheel_refs: Iterable[tuple[str, str]], cache_dir: Path) -> list[Path]:
    """Download and cache wheels.

    ``wheel_refs`` is an iterable of ``(url, sha256)`` pairs.
    :class:`fxtr_sandbox_types.PyWheelPackage` (one wheel per entity,
    fields ``wheel_url`` + ``wheel_sha256``) translates into this
    shape trivially; callers do the conversion.

    Returns paths to all fetched wheel files (in ``cache_dir``).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    wheel_paths: list[Path] = []

    for url, sha256 in wheel_refs:
        path = _ensure_cached(url, sha256, cache_dir)
        wheel_paths.append(path)

    return wheel_paths


def _wheel_filename(url: str) -> str:
    """Extract the wheel filename from its URL."""
    from urllib.parse import urlparse

    return urlparse(url).path.rsplit("/", 1)[-1]


def _ensure_cached(url: str, sha256: str, cache_dir: Path) -> Path:
    """Download a wheel if not already cached. Returns path to cached file."""
    filename = _wheel_filename(url)
    cached_path = cache_dir / f"{sha256[:16]}_{filename}"

    if cached_path.exists():
        if _sha256_file(cached_path) == sha256:
            return cached_path
        cached_path.unlink()

    resp = httpx.get(url, follow_redirects=True)
    resp.raise_for_status()
    content = resp.content

    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != sha256:
        raise ValueError(f"Hash mismatch for {filename}: expected {sha256}, got {actual_hash}")

    cached_path.write_bytes(content)
    return cached_path


def _sha256_file(path: Path) -> str:
    """Compute sha256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
