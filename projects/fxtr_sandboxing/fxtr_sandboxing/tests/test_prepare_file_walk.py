"""Tests for the file-walk source resolution in
``_prepare_common.build_source_package``.

These tests exercise the pure-Python helpers directly (without invoking
``uv lock`` / ``uv export``) so they're fast and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fxtr_sandboxing.deprecated._prepare_common import build_source_package as _build_source_package
from fxtr_sandboxing.deprecated._prepare_common import resolve_source_dirs as _resolve_source_dirs
from fxtr_sandboxing.deprecated._prepare_common import walk_package_dir as _walk_package_dir


def _write_pyproject(pkg_dir: Path, body: str) -> None:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "pyproject.toml").write_text(body)


def _make_pkg(pkg_dir: Path, name: str, files: dict[str, str]) -> None:
    """Write a default pyproject and a top-level package dir with the
    given relative files inside it."""
    _write_pyproject(
        pkg_dir,
        f'[project]\nname = "{name}"\nversion = "0.0.0"\n'
        f'[project.entry-points."fxtr.service"]\nservice = "x:run"\n',
    )
    pkg_src = pkg_dir / name.replace("-", "_").lower()
    pkg_src.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = pkg_src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def test_autodiscover_flat(tmp_path):
    _make_pkg(tmp_path, "my-pkg", {"__init__.py": "x = 1\n", "util.py": "y = 2\n"})
    src = _build_source_package(tmp_path, allowed_local_pkg_dirs=None)
    assert src.source_files == {
        "my_pkg/__init__.py": "x = 1\n",
        "my_pkg/util.py": "y = 2\n",
    }


def test_autodiscover_src_layout(tmp_path):
    _write_pyproject(
        tmp_path,
        '[project]\nname = "my-pkg"\nversion = "0.0.0"\n'
        '[project.entry-points."fxtr.service"]\nservice = "x:run"\n',
    )
    pkg = tmp_path / "src" / "my_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("z = 3\n")
    src = _build_source_package(tmp_path, allowed_local_pkg_dirs=None)
    assert src.source_files == {"my_pkg/__init__.py": "z = 3\n"}


def test_autodiscover_neither_present_raises(tmp_path):
    _write_pyproject(
        tmp_path,
        '[project]\nname = "noplace"\nversion = "0.0.0"\n'
        '[project.entry-points."fxtr.service"]\nservice = "x:run"\n',
    )
    with pytest.raises(ValueError, match="no source dir found"):
        _build_source_package(tmp_path, allowed_local_pkg_dirs=None)


def test_fxtr_sandboxing_packages_override(tmp_path):
    _write_pyproject(
        tmp_path,
        '[project]\nname = "my-pkg"\nversion = "0.0.0"\n'
        '[project.entry-points."fxtr.service"]\nservice = "x:run"\n'
        '[tool.fxtr.sandboxing]\npackages = ["alt_name"]\n',
    )
    # Auto-discover would have looked at `my_pkg/`, but we override to
    # `alt_name/`.
    (tmp_path / "alt_name").mkdir()
    (tmp_path / "alt_name" / "__init__.py").write_text("a = 1\n")
    # Also create the auto-discover dir to confirm it's ignored.
    (tmp_path / "my_pkg").mkdir()
    (tmp_path / "my_pkg" / "__init__.py").write_text("wrong\n")

    src = _build_source_package(tmp_path, allowed_local_pkg_dirs=None)
    assert src.source_files == {"alt_name/__init__.py": "a = 1\n"}


def test_hatch_packages_override(tmp_path):
    _write_pyproject(
        tmp_path,
        '[project]\nname = "my-pkg"\nversion = "0.0.0"\n'
        '[project.entry-points."fxtr.service"]\nservice = "x:run"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/my_pkg"]\n',
    )
    pkg = tmp_path / "src" / "my_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("h = 1\n")
    src = _build_source_package(tmp_path, allowed_local_pkg_dirs=None)
    assert src.source_files == {"my_pkg/__init__.py": "h = 1\n"}


def test_fxtr_sandboxing_takes_priority_over_hatch(tmp_path):
    _write_pyproject(
        tmp_path,
        '[project]\nname = "my-pkg"\nversion = "0.0.0"\n'
        '[project.entry-points."fxtr.service"]\nservice = "x:run"\n'
        '[tool.fxtr.sandboxing]\npackages = ["winner"]\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["loser"]\n',
    )
    (tmp_path / "winner").mkdir()
    (tmp_path / "winner" / "__init__.py").write_text("w = 1\n")
    (tmp_path / "loser").mkdir()
    (tmp_path / "loser" / "__init__.py").write_text("l = 1\n")
    src = _build_source_package(tmp_path, allowed_local_pkg_dirs=None)
    assert src.source_files == {"winner/__init__.py": "w = 1\n"}


def test_skips_pycache_and_pyc(tmp_path):
    _make_pkg(tmp_path, "my-pkg", {"__init__.py": "x = 1\n"})
    pkg = tmp_path / "my_pkg"
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "foo.cpython-314.pyc").write_text("junk")
    (pkg / "sub").mkdir()
    (pkg / "sub" / "__pycache__").mkdir()
    (pkg / "sub" / "__pycache__" / "bar.pyc").write_text("junk")
    (pkg / "sub" / "real.py").write_text("r = 1\n")
    (pkg / "stale.pyc").write_text("junk")
    src = _build_source_package(tmp_path, allowed_local_pkg_dirs=None)
    assert src.source_files == {
        "my_pkg/__init__.py": "x = 1\n",
        "my_pkg/sub/real.py": "r = 1\n",
    }


def test_skips_dot_prefixed_files_and_dirs(tmp_path):
    _make_pkg(tmp_path, "my-pkg", {"__init__.py": "x = 1\n"})
    pkg = tmp_path / "my_pkg"
    (pkg / ".gitignore").write_text("*.tmp\n")
    (pkg / ".hidden").mkdir()
    (pkg / ".hidden" / "leak.py").write_text("nope\n")
    src = _build_source_package(tmp_path, allowed_local_pkg_dirs=None)
    assert src.source_files == {"my_pkg/__init__.py": "x = 1\n"}


def test_binary_file_raises(tmp_path):
    _make_pkg(tmp_path, "my-pkg", {"__init__.py": "x = 1\n"})
    (tmp_path / "my_pkg" / "blob.bin").write_bytes(b"\xff\xfe\xfd\x00")
    with pytest.raises(ValueError, match="Binary file"):
        _build_source_package(tmp_path, allowed_local_pkg_dirs=None)


def test_force_include_directory(tmp_path):
    """v2/portable style: empty packages + force-include from a sibling dir."""
    pkg_dir = tmp_path / "wrapper"
    pkg_dir.mkdir()
    _write_pyproject(
        pkg_dir,
        '[project]\nname = "wrapper"\nversion = "0.0.0"\n'
        '[project.entry-points."fxtr.service"]\nservice = "x:run"\n'
        "[tool.hatch.build.targets.wheel]\npackages = []\n"
        "[tool.hatch.build.targets.wheel.force-include]\n"
        '"../src_root" = "wrapper"\n',
    )
    src_root = tmp_path / "src_root"
    src_root.mkdir()
    (src_root / "a.py").write_text("a = 1\n")
    (src_root / "sub").mkdir()
    (src_root / "sub" / "b.py").write_text("b = 2\n")
    src = _build_source_package(pkg_dir, allowed_local_pkg_dirs=None)
    assert src.source_files == {
        "wrapper/a.py": "a = 1\n",
        "wrapper/sub/b.py": "b = 2\n",
    }


def test_force_include_single_file(tmp_path):
    pkg_dir = tmp_path / "wrapper"
    _write_pyproject(
        pkg_dir,
        '[project]\nname = "wrapper"\nversion = "0.0.0"\n'
        '[project.entry-points."fxtr.service"]\nservice = "x:run"\n'
        "[tool.hatch.build.targets.wheel]\npackages = []\n"
        "[tool.hatch.build.targets.wheel.force-include]\n"
        '"../foo.py" = "wrapper/foo.py"\n',
    )
    (tmp_path / "foo.py").write_text("f = 1\n")
    src = _build_source_package(pkg_dir, allowed_local_pkg_dirs=None)
    assert src.source_files == {"wrapper/foo.py": "f = 1\n"}


def test_resolve_source_dirs_empty_packages_returns_empty(tmp_path):
    """Explicit ``packages = []`` is allowed (force-include is
    expected to fill in)."""
    _write_pyproject(
        tmp_path,
        '[project]\nname = "p"\nversion = "0.0.0"\n'
        "[tool.hatch.build.targets.wheel]\npackages = []\n",
    )
    import tomllib

    with open(tmp_path / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    assert _resolve_source_dirs(tmp_path, pyproject) == []


def test_walk_package_dir_orders_consistently(tmp_path):
    (tmp_path / "z.py").write_text("")
    (tmp_path / "a.py").write_text("")
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "n.py").write_text("")
    found = [p.relative_to(tmp_path).as_posix() for p in _walk_package_dir(tmp_path)]
    assert found == sorted(found)
