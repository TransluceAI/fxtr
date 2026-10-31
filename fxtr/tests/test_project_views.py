"""A project's view bundle from the Python side: reading a manifest, building a renderer package
with its own build script (into a scratch directory, then into place), the sources fingerprint and
staleness, `fxtr views build`, vendoring the fxtr packages (`fxtr views vendor`), and the constants
mirrored from fxtr-view and fxtr-view-bundle."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from fxtr.project import cli
from fxtr.project import views as views_module
from fxtr.project.config import ViewsConfig
from fxtr.project.ts_packages import PROJECT_PACKAGES, stage
from fxtr.project.views import (
    VENDOR_DIR,
    VIEW_API_VERSION,
    VIEW_BUNDLE_MANIFEST_FILENAME,
    VIEW_BUNDLE_OUT_DIR,
    VIEW_BUNDLE_OUT_DIR_ENV,
    VIEW_BUNDLE_SOURCES_FILENAME,
    ViewBundleManifest,
    ViewsError,
    build_views,
    bundle_dir,
    read_manifest,
    sources_fingerprint,
    stale_reason,
    vendor_packages,
)

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parents[2]


def a_bundle(
    dist: Path, manifest: Any, files: tuple[str, ...] = ("bundle.js", "bundle.css")
) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    for name in files:
        (dist / name).write_text("")
    text = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (dist / VIEW_BUNDLE_MANIFEST_FILENAME).write_text(text)
    return dist


# ---- reading a manifest ----


def test_a_manifest_is_read_with_its_directory(tmp_path: Path) -> None:
    dist = a_bundle(
        tmp_path,
        {"apiVersion": VIEW_API_VERSION, "module": "bundle.js", "stylesheet": "bundle.css"},
    )
    assert read_manifest(dist) == ViewBundleManifest(
        dist=dist, api_version=VIEW_API_VERSION, module="bundle.js", stylesheet="bundle.css"
    )


def test_a_bundle_without_styles_has_no_stylesheet(tmp_path: Path) -> None:
    dist = a_bundle(
        tmp_path, {"apiVersion": VIEW_API_VERSION, "module": "bundle.js"}, ("bundle.js",)
    )
    assert read_manifest(dist).stylesheet is None


@pytest.mark.parametrize(
    ("manifest", "files", "message"),
    [
        (None, (), "holds no view bundle: run `fxtr views build`"),
        ("{not json", ("bundle.js",), "not a readable manifest"),
        ({"module": "bundle.js"}, ("bundle.js",), "not a view bundle manifest"),
        (
            {"apiVersion": VIEW_API_VERSION, "module": 3},
            ("bundle.js",),
            "not a view bundle manifest",
        ),
        ({"apiVersion": VIEW_API_VERSION + 1, "module": "bundle.js"}, ("bundle.js",), "Rebuild"),
        (
            {"apiVersion": VIEW_API_VERSION, "module": "bundle.js"},
            (),
            "names bundle.js, which is not",
        ),
        (
            {"apiVersion": VIEW_API_VERSION, "module": "bundle.js", "stylesheet": "bundle.css"},
            ("bundle.js",),
            "names bundle.css, which is not",
        ),
    ],
)
def test_a_bad_bundle_is_refused(
    tmp_path: Path, manifest: Any, files: tuple[str, ...], message: str
) -> None:
    if manifest is None:
        tmp_path.mkdir(exist_ok=True)
    else:
        a_bundle(tmp_path, manifest, files)
    with pytest.raises(ViewsError, match=re.escape(message)):
        read_manifest(tmp_path)


def test_the_bundle_is_the_packages_dist(tmp_path: Path) -> None:
    assert bundle_dir(ViewsConfig(package=tmp_path)) == tmp_path / "dist"


# ---- building ----

needs_npm = pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed")

WRITE_BUNDLE = f"""
const fs = require("fs");
const out = process.env.{VIEW_BUNDLE_OUT_DIR_ENV} || "dist";
fs.mkdirSync(out, {{ recursive: true }});
fs.writeFileSync(out + "/bundle.js", "export default () => ({{ renderers: [] }});");
const manifest = {{ apiVersion: Number(process.argv[2]), module: "bundle.js" }};
fs.writeFileSync(out + "/{VIEW_BUNDLE_MANIFEST_FILENAME}", JSON.stringify(manifest));
"""


def a_package(root: Path, build: str, dependencies: dict[str, str] | None = None) -> ViewsConfig:
    """A renderer package whose `build` script is ``build``, with a script that writes a bundle
    where the preset would."""
    root.mkdir(parents=True)
    package: dict[str, Any] = {"name": "p", "private": True, "scripts": {"build": build}}
    if dependencies is not None:
        package["dependencies"] = dependencies
    (root / "package.json").write_text(json.dumps(package))
    (root / "write_bundle.js").write_text(WRITE_BUNDLE)
    return ViewsConfig(package=root)


def set_build(views: ViewsConfig, build: str) -> None:
    path = views.package / "package.json"
    package = json.loads(path.read_text())
    package["scripts"]["build"] = build
    path.write_text(json.dumps(package))


@needs_npm
async def test_build_runs_the_packages_build_script(tmp_path: Path) -> None:
    views = a_package(tmp_path / "views", f"node write_bundle.js {VIEW_API_VERSION}")
    manifest = (await build_views(views)).manifest
    assert (manifest.dist, manifest.module, manifest.stylesheet) == (
        tmp_path / "views" / "dist",
        "bundle.js",
        None,
    )


@needs_npm
async def test_build_captures_its_bytes_before_publishing_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    views = a_package(tmp_path / "views", f"node write_bundle.js {VIEW_API_VERSION}")
    replace = views_module.replace_directory

    def another_build_arrives(built: Path, dist: Path, scratch: Path) -> None:
        replace(built, dist, scratch)
        (dist / "bundle.js").write_text("another build")

    monkeypatch.setattr(views_module, "replace_directory", another_build_arrives)
    snapshot = await build_views(views)
    assert snapshot.files["bundle.js"] != b"another build"
    assert (snapshot.manifest.dist / "bundle.js").read_bytes() == b"another build"


@needs_npm
@pytest.mark.parametrize(
    ("build", "message"),
    [
        ("exit 3", "failed (exit"),
        ("true", "wrote no view bundle"),
        (f"node write_bundle.js {VIEW_API_VERSION + 1}", "Rebuild"),
    ],
)
async def test_a_bad_build_is_refused(tmp_path: Path, build: str, message: str) -> None:
    views = a_package(tmp_path / "views", build)
    with pytest.raises(ViewsError, match=re.escape(message)):
        await build_views(views)


async def test_a_package_needs_a_package_json(tmp_path: Path) -> None:
    with pytest.raises(ViewsError, match="has no package.json"):
        await build_views(ViewsConfig(package=tmp_path))


async def test_a_package_with_dependencies_needs_them_installed(tmp_path: Path) -> None:
    views = a_package(tmp_path / "views", "true", {"react": "^18.3.1"})
    with pytest.raises(ViewsError, match=re.escape("not installed: run `pnpm install` there")):
        await build_views(views)


@needs_npm
async def test_a_build_records_its_sources_and_leaves_no_scratch(tmp_path: Path) -> None:
    views = a_package(tmp_path / "views", f"node write_bundle.js {VIEW_API_VERSION}")
    assert stale_reason(views) is not None
    await build_views(views)
    recorded = json.loads((bundle_dir(views) / VIEW_BUNDLE_SOURCES_FILENAME).read_text())
    assert recorded == {"sources": sources_fingerprint(views)}
    assert stale_reason(views) is None
    assert list((views.package / "node_modules" / ".cache" / "fxtr-views").iterdir()) == []


@needs_npm
async def test_a_failed_build_keeps_the_previous_bundle(tmp_path: Path) -> None:
    views = a_package(tmp_path / "views", f"node write_bundle.js {VIEW_API_VERSION}")
    await build_views(views)
    before = {p.name: p.read_bytes() for p in bundle_dir(views).iterdir()}
    for build in ("exit 3", "true", f"node write_bundle.js {VIEW_API_VERSION + 1}"):
        set_build(views, build)
        with pytest.raises(ViewsError):
            await build_views(views)
        assert {p.name: p.read_bytes() for p in bundle_dir(views).iterdir()} == before
    # The edited package.json is a source, so the kept bundle is now stale.
    assert stale_reason(views) == "its sources changed since it was built"


@needs_npm
async def test_a_build_replaces_the_previous_bundle(tmp_path: Path) -> None:
    views = a_package(tmp_path / "views", f"node write_bundle.js {VIEW_API_VERSION}")
    await build_views(views)
    (bundle_dir(views) / "leftover.js").write_text("")
    (views.package / "src.ts").write_text("changed")
    await build_views(views)
    assert not (bundle_dir(views) / "leftover.js").exists()
    assert stale_reason(views) is None


# ---- the sources fingerprint and staleness ----


def test_staleness_says_why(tmp_path: Path) -> None:
    views = ViewsConfig(package=tmp_path / "views")
    (tmp_path / "views").mkdir()
    assert "holds no view bundle" in (stale_reason(views) or "")
    a_bundle(bundle_dir(views), {"apiVersion": VIEW_API_VERSION, "module": "bundle.js"})
    assert "built without `fxtr views build`" in (stale_reason(views) or "")
    stamp = bundle_dir(views) / VIEW_BUNDLE_SOURCES_FILENAME
    stamp.write_text(json.dumps({"sources": sources_fingerprint(views)}))
    assert stale_reason(views) is None
    (tmp_path / "views" / "index.ts").write_text("new")
    assert stale_reason(views) == "its sources changed since it was built"


def test_the_fingerprint_covers_linked_packages_and_skips_the_rest(tmp_path: Path) -> None:
    views = ViewsConfig(package=tmp_path / "views")
    package = {"name": "p", "dependencies": {"lib": "link:../lib", "react": "^18"}}
    (tmp_path / "views" / "src").mkdir(parents=True)
    (tmp_path / "views" / "package.json").write_text(json.dumps(package))
    (tmp_path / "views" / "src" / "index.ts").write_text("a")
    (tmp_path / "lib" / "src").mkdir(parents=True)
    (tmp_path / "lib" / "package.json").write_text(json.dumps({"name": "lib"}))
    (tmp_path / "lib" / "src" / "kit.ts").write_text("a")
    modules = tmp_path / "views" / "node_modules"
    store = modules / ".pnpm" / "react@18" / "node_modules" / "react"
    store.mkdir(parents=True)
    (modules / "lib").symlink_to(tmp_path / "lib")
    (modules / "react").symlink_to(store)
    before = sources_fingerprint(views)

    # Not sources: installed packages, built output, dot-entries.
    (store / "index.js").write_text("b")
    (tmp_path / "lib" / "dist").mkdir()
    (tmp_path / "lib" / "dist" / "out.js").write_text("b")
    (tmp_path / "views" / ".cache").mkdir()
    (tmp_path / "views" / ".cache" / "x").write_text("b")
    assert sources_fingerprint(views) == before

    # Sources: the package's own files and a linked package's.
    (tmp_path / "lib" / "src" / "kit.ts").write_text("b")
    after_lib = sources_fingerprint(views)
    assert after_lib != before
    (tmp_path / "views" / "src" / "index.ts").write_text("b")
    assert sources_fingerprint(views) != after_lib


# ---- fxtr views build ----


def no_client(project: Path | None, *, verify_environment: bool) -> Any:
    raise AssertionError("fxtr views build opens no client")


@needs_npm
async def test_the_command_builds_the_projects_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\n\n[tool.fxtr]\n\n[tool.fxtr.views]\npackage = "views"\n'
    )
    a_package(tmp_path / "views", f"node write_bundle.js {VIEW_API_VERSION}")
    status = await cli.main(["--project", str(tmp_path), "views", "build"], open_client=no_client)
    out = capsys.readouterr().out
    assert status == cli.EXIT_OK
    assert out.strip().endswith(
        f"built the view bundle in {tmp_path.resolve() / 'views' / 'dist'} (bundle.js)"
    )


async def test_the_command_needs_a_views_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\n\n[tool.fxtr]\n')
    status = await cli.main(["--project", str(tmp_path), "views", "build"], open_client=no_client)
    assert status == cli.EXIT_REFUSED
    assert "add a [tool.fxtr.views] table" in capsys.readouterr().err


# ---- vendoring the fxtr packages ----


def a_renderer_package(root: Path, *dependencies: str) -> Path:
    root.mkdir(parents=True)
    manifest = {"name": "p-views", "dependencies": {d: "link:x" for d in dependencies}}
    (root / "package.json").write_text(json.dumps(manifest))
    return root


def test_vendoring_copies_the_checkouts_packages_staged(tmp_path: Path) -> None:
    package = a_renderer_package(tmp_path / "views", "fxtr-view", "fxtr-view-kit")

    vendored = vendor_packages(package)

    target = package.resolve() / VENDOR_DIR
    assert vendored.directory == target
    assert vendored.missing_dependencies == {}
    staged = tmp_path / "staged"
    stage(REPO / "ts", staged)
    copied = sorted(p.relative_to(target) for p in target.rglob("*") if p.name != "README.md")
    assert copied == sorted(p.relative_to(staged) for p in staged.rglob("*"))
    assert f"fxtr {vendored.fxtr_version}" in (target / "README.md").read_text()
    assert sorted(p.name for p in (target / "packages").iterdir()) == sorted(PROJECT_PACKAGES)
    assert [p.name for p in target.parent.iterdir()] == ["fxtr"]


def test_vendoring_again_replaces_the_copy(tmp_path: Path) -> None:
    package = a_renderer_package(tmp_path / "views", "fxtr-view")
    vendor_packages(package)
    (package / VENDOR_DIR / "packages" / "fxtr-view" / "stale.ts").write_text("")

    vendor_packages(package)

    assert not (package / VENDOR_DIR / "packages" / "fxtr-view" / "stale.ts").exists()
    assert (package / VENDOR_DIR / "packages" / "fxtr-view" / "package.json").is_file()


def test_vendoring_changes_the_packages_sources(tmp_path: Path) -> None:
    package = a_renderer_package(tmp_path / "views", "fxtr-view")
    before = sources_fingerprint(ViewsConfig(package=package))
    vendor_packages(package)
    assert sources_fingerprint(ViewsConfig(package=package)) != before


def test_vendoring_reports_registry_packages_the_package_must_add(tmp_path: Path) -> None:
    package = a_renderer_package(tmp_path / "views", "fxtr-view", "fxtr-view-shell-util")
    assert vendor_packages(package).missing_dependencies == {
        "@ipld/dag-cbor": "^9.2.1",
        "multiformats": "^13.3.1",
    }


def test_an_installed_fxtrs_packaged_copy_is_preferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / "_ts"
    stage(REPO / "ts", packaged)
    (packaged / "packages" / "fxtr-view" / "from-the-wheel.ts").write_text("")
    monkeypatch.setattr(views_module, "PACKAGED_TS", packaged)

    vendored = vendor_packages(a_renderer_package(tmp_path / "views", "fxtr-view"))

    assert (vendored.directory / "packages" / "fxtr-view" / "from-the-wheel.ts").is_file()


def test_a_package_inside_the_checkout_is_not_vendored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "checkout" / "ts").mkdir(parents=True)
    monkeypatch.setattr(views_module, "CHECKOUT_TS", tmp_path / "checkout" / "ts")
    package = a_renderer_package(tmp_path / "checkout" / "personal" / "views", "fxtr-view")
    with pytest.raises(ViewsError, match="inside the fxtr checkout"):
        vendor_packages(package)
    assert not (package / "vendor").exists()


def test_an_fxtr_without_packages_has_nothing_to_vendor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views_module, "PACKAGED_TS", tmp_path / "none")
    monkeypatch.setattr(views_module, "CHECKOUT_TS", tmp_path / "no-checkout" / "ts")
    package = a_renderer_package(tmp_path / "views", "fxtr-view")
    with pytest.raises(ViewsError, match="carries no TypeScript packages"):
        vendor_packages(package)
    assert list((package / "vendor").iterdir()) == []


async def test_fxtr_views_vendor_copies_them_into_the_projects_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\n\n[tool.fxtr]\n\n[tool.fxtr.views]\npackage = "views"\n'
    )
    a_renderer_package(tmp_path / "views", "fxtr-view", "fxtr-view-shell-util")

    status = await cli.main(["--project", str(tmp_path), "views", "vendor"], open_client=no_client)

    out = capsys.readouterr().out
    package = (tmp_path / "views").resolve()
    assert status == cli.EXIT_OK
    assert f"TypeScript packages to {package / VENDOR_DIR}" in out
    assert "@ipld/dag-cbor@^9.2.1, multiformats@^13.3.1" in out
    assert out.endswith(f"next: `pnpm install` in {package}, then `fxtr views build`\n")
    assert (package / VENDOR_DIR / "tsconfig.base.json").is_file()


# ---- the constants mirrored from fxtr-view ----


def test_the_mirrored_constants_match_fxtr_view() -> None:
    source = (REPO / "ts" / "packages" / "fxtr-view" / "src" / "view_bundle.ts").read_text()

    def constant(name: str) -> str:
        found = re.search(rf"export const {name} = (.+);", source)
        assert found is not None, f"view_bundle.ts declares no {name}"
        return found.group(1)

    assert constant("VIEW_API_VERSION") == str(VIEW_API_VERSION)
    assert constant("VIEW_BUNDLE_MANIFEST_FILENAME") == json.dumps(VIEW_BUNDLE_MANIFEST_FILENAME)
    assert constant("VIEW_BUNDLE_OUT_DIR") == json.dumps(VIEW_BUNDLE_OUT_DIR)


def test_the_out_dir_variable_matches_the_preset() -> None:
    source = (REPO / "ts" / "packages" / "fxtr-view-bundle" / "src" / "index.ts").read_text()
    found = re.search(r"export const VIEW_BUNDLE_OUT_DIR_ENV = (.+);", source)
    assert found is not None, "fxtr-view-bundle declares no VIEW_BUNDLE_OUT_DIR_ENV"
    assert found.group(1) == json.dumps(VIEW_BUNDLE_OUT_DIR_ENV)
