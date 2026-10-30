"""Download a fixed set of pure-Python wheels for benchmarking.

Pulls py3-none-any wheels from PyPI's JSON API for a pinned set of
packages. Idempotent (per-version filename) and skipped if the wheel
already exists in `wheels_cache/`.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

# Pinned (name, version). Each is known to publish a py3-none-any wheel,
# so we don't need a platform spec.
PINNED = [
    ("six", "1.16.0"),
    ("attrs", "24.2.0"),
    ("click", "8.1.7"),
]

_DEST = Path(__file__).parent / "wheels_cache"


def _pypi_meta(name: str, version: str) -> dict:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())


def _pick_pure_wheel(meta: dict) -> dict:
    for u in meta["urls"]:
        if u["packagetype"] == "bdist_wheel" and u["filename"].endswith("-py3-none-any.whl"):
            return u
    for u in meta["urls"]:
        if u["packagetype"] == "bdist_wheel" and u["filename"].endswith("-py2.py3-none-any.whl"):
            return u
    raise RuntimeError(f"no py3-none-any wheel found in PyPI metadata: {meta['info']['name']}")


def main() -> int:
    _DEST.mkdir(exist_ok=True)
    for name, version in PINNED:
        meta = _pypi_meta(name, version)
        info = _pick_pure_wheel(meta)
        filename = info["filename"]
        dest = _DEST / filename
        if dest.exists():
            print(f"[ok ] {filename} (cached)")
            continue
        print(f"[get] {filename}  ({info['url']})")
        with urllib.request.urlopen(info["url"]) as resp:
            content = resp.read()
        want = info["digests"]["sha256"]
        got = hashlib.sha256(content).hexdigest()
        if got != want:
            print(f"  hash mismatch: got {got}, want {want}", file=sys.stderr)
            return 1
        dest.write_bytes(content)
        print(f"  wrote {dest} ({len(content):_} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
