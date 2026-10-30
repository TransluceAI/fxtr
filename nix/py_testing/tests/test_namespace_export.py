"""Tests for the ``namespace_export`` writer kind (``fxtr_nix_writer``).

Two halves:
- In-process unit tests of ``write_namespace_export`` over a tmpdir namespace
  (no nix, no ``.fxtr/``, no network): happy path + full-key prefix shape +
  the validation failures ``read_tsv`` enforces.
- A nix-built round-trip (``test_namespace_export_nix_round_trip``) that builds
  the repo-root flake check target and reads the manifest back, via the
  ``nix_build`` fixture (run from the colocated jj↔git worktree).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest

from fxtr2.data.tsv_namespace.tsv_file import FxtrTSVError
from fxtr_nix_writer.writers import (
    NAMESPACE_EXPORT_FILENAME,
    NAMESPACE_EXPORT_FORMAT_VERSION,
    write_namespace_export,
)

# A few syntactically-valid, content-stable id strings (lifted from
# nix/lib/entities.nix, where the dispatcher validates them on every build).
ID_A = "du7aezbzspqg4fsl3x5txk6q2vl3zkow6xc3yqofygkz2h5u4zwua"
ID_B = "d4nqychcnfoxwdou2ayifrx3w4rcjmmhgmsfmdkwr5zbe3rgezkdq"
ID_C = "dor36ohjfhtt4ggskqtwqvy4mowpax4nnsihr4xvrohpdbqkwwxja"


def _make_namespace(root: Path, *, reserved_prefix: str = "root") -> None:
    """Write a minimal namespace marker at ``root`` (no ``.fxtr/``)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "fxtr_namespace.toml").write_text(
        f'reserved_prefix = "{reserved_prefix}"\nname = "demo"\n'
    )


def _run_export(root: Path, out: Path, name: str = "demo") -> dict:
    anyio.run(
        write_namespace_export,
        str(out),
        {"kind": "namespace_export", "namespaceRoot": str(root), "namespaceName": name},
    )
    return json.loads((out / NAMESPACE_EXPORT_FILENAME).read_text())


def test_happy_path_full_keys_and_ids(tmp_path: Path) -> None:
    """Reserved-prefix keys stay colon-less; prefixed files keep ``prefix:``."""
    root = tmp_path / "ns"
    _make_namespace(root, reserved_prefix="root")
    # Reserved-prefix TSV: colon-less keys (must be sorted).
    (root / "root.fxtr.tsv").write_text(f"alpha\t{ID_A}\nbeta\t{ID_B}\n")
    # A nested, prefixed TSV.
    (root / "data").mkdir()
    (root / "data" / "models.fxtr.tsv").write_text(f"encoder\t{ID_C}\n")

    out = tmp_path / "out"
    manifest = _run_export(root, out, name="published")

    assert manifest["format_version"] == NAMESPACE_EXPORT_FORMAT_VERSION
    assert manifest["name"] == "published"
    assert manifest["pins"] == {
        "alpha": ID_A,
        "beta": ID_B,
        "data/models:encoder": ID_C,
    }


def test_output_is_sorted_and_byte_stable(tmp_path: Path) -> None:
    """Pins are emitted sorted, so a fixed input gives byte-identical output."""
    root = tmp_path / "ns"
    _make_namespace(root)
    (root / "root.fxtr.tsv").write_text(f"alpha\t{ID_A}\nbeta\t{ID_B}\n")

    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    _run_export(root, out1)
    _run_export(root, out2)

    bytes1 = (out1 / NAMESPACE_EXPORT_FILENAME).read_bytes()
    bytes2 = (out2 / NAMESPACE_EXPORT_FILENAME).read_bytes()
    assert bytes1 == bytes2
    # Keys appear in sorted order in the serialized form.
    text = bytes1.decode()
    assert text.index('"alpha"') < text.index('"beta"')


def test_empty_namespace_yields_empty_pins(tmp_path: Path) -> None:
    root = tmp_path / "ns"
    _make_namespace(root)
    out = tmp_path / "out"
    manifest = _run_export(root, out)
    assert manifest["pins"] == {}


def test_unsorted_tsv_fails(tmp_path: Path) -> None:
    root = tmp_path / "ns"
    _make_namespace(root)
    (root / "root.fxtr.tsv").write_text(f"beta\t{ID_B}\nalpha\t{ID_A}\n")
    out = tmp_path / "out"
    with pytest.raises(FxtrTSVError, match="not sorted"):
        _run_export(root, out)


def test_duplicate_key_fails(tmp_path: Path) -> None:
    root = tmp_path / "ns"
    _make_namespace(root)
    (root / "root.fxtr.tsv").write_text(f"alpha\t{ID_A}\nalpha\t{ID_B}\n")
    out = tmp_path / "out"
    with pytest.raises(FxtrTSVError, match="[Dd]uplicate"):
        _run_export(root, out)


def test_colon_in_tsv_key_fails(tmp_path: Path) -> None:
    root = tmp_path / "ns"
    _make_namespace(root)
    (root / "root.fxtr.tsv").write_text(f"a:b\t{ID_A}\n")
    out = tmp_path / "out"
    with pytest.raises(FxtrTSVError, match="colon"):
        _run_export(root, out)


def test_unparseable_id_fails(tmp_path: Path) -> None:
    root = tmp_path / "ns"
    _make_namespace(root)
    (root / "root.fxtr.tsv").write_text("alpha\tnot-a-valid-id\n")
    out = tmp_path / "out"
    with pytest.raises(FxtrTSVError, match="[Ii]nvalid FxtrID"):
        _run_export(root, out)


def test_namespace_export_nix_round_trip(nix_build: Callable[[str], str]) -> None:
    """Build the repo-root flake check target and read the manifest back."""
    out = nix_build("packages.x86_64-linux.namespace-export-check")
    manifest = json.loads((Path(out) / NAMESPACE_EXPORT_FILENAME).read_text())
    assert manifest["format_version"] == NAMESPACE_EXPORT_FORMAT_VERSION
    assert isinstance(manifest["pins"], dict)
