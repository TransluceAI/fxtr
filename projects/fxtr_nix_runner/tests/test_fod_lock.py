"""Unit tests for the pure FOD-lock helpers (`_fod_lock.py`, no nix/fxtr2)."""

from __future__ import annotations

import base64
import tomllib
from pathlib import Path

from fxtr_nix_runner._fod_lock import (
    FodLock,
    HashMismatch,
    parse_mismatches,
    read_lock,
    sentinel_for,
    write_lock,
)


def test_read_missing_file_is_empty_lock(tmp_path: Path) -> None:
    lock = read_lock(tmp_path / "fod_lock.toml")
    assert lock == FodLock(hashes={}, format_version=1)


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "fod_lock.toml"
    lock = FodLock(hashes={"greeting": "sha256-aaa", "data/models:encoder": "sha256-bbb"})
    write_lock(path, lock)
    assert read_lock(path) == lock


def test_write_is_sorted_and_quotes_special_keys(tmp_path: Path) -> None:
    path = tmp_path / "fod_lock.toml"
    write_lock(
        path,
        FodLock(hashes={"zeta": "sha256-z", "alpha": "sha256-a", "a/b:c": "sha256-c"}),
    )
    text = path.read_text()
    # Sorted by key within [hashes].
    assert text.index('"a/b:c"') < text.index("alpha") < text.index("zeta")
    # Special chars force a quoted key; tomllib reads it back unquoted.
    assert tomllib.loads(text)["hashes"]["a/b:c"] == "sha256-c"


def test_sentinel_is_deterministic_distinct_and_valid_sri() -> None:
    a, b = sentinel_for("thePin"), sentinel_for("other")
    assert a == sentinel_for("thePin")  # deterministic
    assert a != b  # distinct per key
    for s in (a, b):
        algo, _, payload = s.partition("-")
        assert algo == "sha256"
        assert len(base64.b64decode(payload)) == 32  # a real 32-byte sha256 value


_ONE_MISMATCH = """\
error: hash mismatch in fixed-output derivation '/nix/store/aaa-fxtr-entity-from-tsv-pin.drv':
         specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
            got:    sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=
"""


def test_parse_single_mismatch() -> None:
    [m] = parse_mismatches(_ONE_MISMATCH)
    assert m == HashMismatch(
        specified="sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        got="sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=",
    )


def test_parse_multiple_mismatches_keep_going() -> None:
    two = _ONE_MISMATCH + (
        "error: hash mismatch in fixed-output derivation '/nix/store/ccc-other.drv':\n"
        "         specified: sha256-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC=\n"
        "            got:    sha256-DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD=\n"
    )
    specs = {m.specified for m in parse_mismatches(two)}
    assert specs == {
        "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "sha256-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC=",
    }


def test_parse_tolerates_got_before_specified() -> None:
    flipped = (
        "error: hash mismatch in fixed-output derivation '/nix/store/aaa.drv':\n"
        "            got:    sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=\n"
        "         specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
    )
    [m] = parse_mismatches(flipped)
    assert m.specified == "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    assert m.got == "sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="


def test_parse_no_mismatch_is_empty() -> None:
    assert parse_mismatches("error: build of '/nix/store/x.drv' failed: oom\n") == []
