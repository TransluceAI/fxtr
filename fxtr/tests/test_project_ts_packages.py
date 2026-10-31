"""The staged TypeScript packages fxtr ships for renderer packages: the files staged, the manifests'
dependencies, and that the staged packages import only one another."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fxtr.project.ts_packages import (
    BASE_FILES,
    PROJECT_PACKAGES,
    registry_dependencies,
    stage,
    staged_manifest,
)

REPO = Path(__file__).resolve().parents[2]

_IMPORT = re.compile(r"""(?:from|import)\s*["']([^"'.][^"']*)["']""")
"""A bare module specifier in an import or export statement."""


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    stage(REPO / "ts", tmp_path / "_ts")
    return tmp_path / "_ts"


def test_the_staged_tree_has_the_base_files_and_each_projects_package(staged: Path) -> None:
    assert sorted(p.name for p in staged.iterdir()) == sorted([*BASE_FILES, "packages"])
    assert sorted(p.name for p in (staged / "packages").iterdir()) == sorted(PROJECT_PACKAGES)
    for name in BASE_FILES:
        assert (staged / name).read_bytes() == (REPO / "ts" / name).read_bytes()


def test_a_staged_package_has_its_exported_directories_and_nothing_else(staged: Path) -> None:
    shell_util = staged / "packages" / "fxtr-view-shell-util"
    assert sorted(p.name for p in shell_util.iterdir()) == ["package.json", "src", "testing"]
    for name in PROJECT_PACKAGES:
        package = staged / "packages" / name
        exports = json.loads((package / "package.json").read_text())["exports"]
        for target in exports.values():
            assert (package / target).is_file(), f"{name} exports {target}"
        assert not list(package.rglob("node_modules"))


def test_a_staged_manifest_drops_workspace_and_development_dependencies() -> None:
    manifest = {
        "name": "fxtr-view-x",
        "scripts": {"test": "vitest run"},
        "exports": {".": "./src/index.ts"},
        "dependencies": {"fxtr-view": "workspace:*", "multiformats": "^13.3.1"},
        "peerDependencies": {"react": "^18.3.1"},
        "devDependencies": {"vitest": "^2.1.3"},
    }
    assert staged_manifest(manifest) == {
        "name": "fxtr-view-x",
        "exports": {".": "./src/index.ts"},
        "dependencies": {"multiformats": "^13.3.1"},
        "peerDependencies": {"react": "^18.3.1"},
    }


def test_registry_dependencies_are_the_ones_a_project_must_install(staged: Path) -> None:
    assert registry_dependencies(staged) == {
        "fxtr-view-shell-util": {"@ipld/dag-cbor": "^9.2.1", "multiformats": "^13.3.1"}
    }


def test_the_staged_packages_import_only_one_another_and_registry_packages(staged: Path) -> None:
    declared = {
        name: json.loads((staged / "packages" / name / "package.json").read_text())
        for name in PROJECT_PACKAGES
    }
    for name, manifest in declared.items():
        allowed = set(manifest.get("dependencies", {})) | set(manifest.get("peerDependencies", {}))
        for source in (staged / "packages" / name).rglob("*.ts*"):
            for specifier in _IMPORT.findall(source.read_text()):
                package = "/".join(specifier.split("/")[: 2 if specifier.startswith("@") else 1])
                if package.startswith("fxtr-view"):
                    assert package in PROJECT_PACKAGES, f"{source} imports {specifier}"
                else:
                    assert package in allowed or package in {"vite", "react", "jotai"}, (
                        f"{source} imports {specifier}, which {name} does not declare"
                    )
