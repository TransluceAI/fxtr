"""Tests for the allowed_local_pkg_dirs whitelist enforcement.

Exercises ``check_whitelist`` directly (no uv subprocess needed) and
the force-include path's whitelist check via ``build_source_package``.
Both helpers live in :mod:`fxtr_sandboxing.deprecated._prepare_common`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fxtr_sandboxing.deprecated._prepare_common import build_source_package as _build_source_package
from fxtr_sandboxing.deprecated._prepare_common import check_whitelist as _check_whitelist


def test_check_whitelist_none_allows_anything():
    # When the whitelist is None, anything goes.
    _check_whitelist("foo", Path("/some/random/path"), None)


def test_check_whitelist_under_root_passes(tmp_path):
    root = tmp_path / "trusted"
    root.mkdir()
    inside = root / "sub"
    inside.mkdir()
    _check_whitelist("foo", inside, [root])
    _check_whitelist("foo", root, [root])  # equal-to-root counts


def test_check_whitelist_outside_raises(tmp_path):
    root = tmp_path / "trusted"
    root.mkdir()
    outside = tmp_path / "untrusted"
    outside.mkdir()
    with pytest.raises(ValueError, match="outside the allowed"):
        _check_whitelist("badpkg", outside, [root])


def test_check_whitelist_multi_root_any_match(tmp_path):
    r1 = tmp_path / "a"
    r2 = tmp_path / "b"
    r1.mkdir()
    r2.mkdir()
    (r1 / "x").mkdir()
    (r2 / "y").mkdir()
    _check_whitelist("p", r1 / "x", [r1, r2])
    _check_whitelist("p", r2 / "y", [r1, r2])


def test_force_include_source_subject_to_whitelist(tmp_path):
    """An attacker pyproject with force-include pointing outside the
    package's trust root should raise."""
    pkg_dir = tmp_path / "wrapper"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "wrapper"\nversion = "0.0.0"\n'
        '[project.entry-points."fxtr.service"]\nservice = "x:run"\n'
        "[tool.hatch.build.targets.wheel]\npackages = []\n"
        "[tool.hatch.build.targets.wheel.force-include]\n"
        '"../secret_dir" = "wrapper"\n',
    )
    secret = tmp_path / "secret_dir"
    secret.mkdir()
    (secret / "leak.py").write_text("leak\n")
    # Whitelist allows only the package dir, not its parent.
    with pytest.raises(ValueError, match="outside the allowed"):
        _build_source_package(pkg_dir, allowed_local_pkg_dirs=[pkg_dir.resolve()])


def test_force_include_source_under_whitelist_passes(tmp_path):
    """The trust root covers both the package and its force-include
    source (v2/portable layout)."""
    pkg_dir = tmp_path / "wrapper"
    pkg_dir.mkdir()
    (pkg_dir / "pyproject.toml").write_text(
        '[project]\nname = "wrapper"\nversion = "0.0.0"\n'
        '[project.entry-points."fxtr.service"]\nservice = "x:run"\n'
        "[tool.hatch.build.targets.wheel]\npackages = []\n"
        "[tool.hatch.build.targets.wheel.force-include]\n"
        '"../sibling" = "wrapper"\n',
    )
    sib = tmp_path / "sibling"
    sib.mkdir()
    (sib / "a.py").write_text("a = 1\n")
    # tmp_path covers both pkg_dir and the sibling dir.
    src = _build_source_package(pkg_dir, allowed_local_pkg_dirs=[tmp_path.resolve()])
    assert src.source_files == {"wrapper/a.py": "a = 1\n"}
