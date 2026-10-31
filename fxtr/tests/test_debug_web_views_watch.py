"""The project's view bundle while the viewer runs (``debug_web/views_watch.py``): rebuilt as its
sources change and not as its tests do, its state and version, its events on the ``/api/events``
WebSocket and ``/api/views/status``; and the build's side of it in ``fxtr.project.views``: the Node
check, the failed build's output, test files out of the fingerprint, and the bundle's version."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import pytest

pytest.importorskip("starlette")

from asgi_testing import a_page_socket  # noqa: E402
from postgres_testing import a_client  # noqa: E402
from starlette.types import Message  # noqa: E402

from fxtr.project.config import Project, ViewsConfig  # noqa: E402
from fxtr.project.views import (  # noqa: E402
    VIEW_API_VERSION,
    VIEW_BUNDLE_MANIFEST_FILENAME,
    VIEW_BUNDLE_OUT_DIR_ENV,
    VIEW_BUNDLE_SOURCES_FILENAME,
    ViewBundle,
    ViewsBuildError,
    ViewsError,
    build_views,
    bundle_dir,
    bundle_version,
    is_source_path,
    read_bundle,
    sources_fingerprint,
)
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.debug_web.app import (  # noqa: E402
    VIEWS_VERSION_HEADER,
    create_app,
    views_response,
)
from fxtr.runner.postgres.debug_web.events import EventHub, follow_views  # noqa: E402
from fxtr.runner.postgres.debug_web.views_watch import (  # noqa: E402
    ViewsState,
    ViewsWatcher,
)

pytestmark = pytest.mark.anyio

needs_npm = pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed")

# Writes a bundle whose module embeds src/index.js, so an edit to it changes the bundle.
BUILD = f"""
const fs = require("fs");
const out = process.env.{VIEW_BUNDLE_OUT_DIR_ENV} || "dist";
fs.mkdirSync(out, {{ recursive: true }});
const source = fs.readFileSync("src/index.js", "utf8");
fs.writeFileSync(out + "/bundle.js", source);
const manifest = {{ apiVersion: {VIEW_API_VERSION}, module: "bundle.js" }};
fs.writeFileSync(out + "/{VIEW_BUNDLE_MANIFEST_FILENAME}", JSON.stringify(manifest));
if (source.includes("syntax error")) {{
  console.error("src/index.js:1 syntax error");
  console.error("    at failureErrorWithLog (node_modules/esbuild/lib/main.js:1467:15)");
  process.exit(1);
}}
"""


def a_package(root: Path) -> ViewsConfig:
    """A renderer package whose build copies ``src/index.js`` into its bundle."""
    (root / "src").mkdir(parents=True)
    (root / "test").mkdir()
    package = {"name": "p", "private": True, "scripts": {"build": "node build.js"}}
    (root / "package.json").write_text(json.dumps(package))
    (root / "build.js").write_text(BUILD)
    (root / "src" / "index.js").write_text("export default () => ({ renderers: [] });\n")
    (root / "test" / "bundle.test.ts").write_text("// a test\n")
    return ViewsConfig(package=root)


async def next_state(updates: AsyncIterator[ViewsState | None], timeout: float = 20) -> ViewsState:
    with anyio.fail_after(timeout):
        while (state := await anext(updates)) is None:
            pass
    return state


# ---- the build's side ----


def test_tests_are_not_sources(tmp_path: Path) -> None:
    package = tmp_path / "views"
    for path, source in [
        ("src/index.ts", True),
        ("src/chart.module.css", True),
        ("package.json", True),
        ("test/bundle.test.ts", False),
        ("tests/x.ts", False),
        ("src/__tests__/x.ts", False),
        ("src/chart.test.tsx", False),
        ("src/chart.spec.ts", False),
        ("vitest.config.ts", False),
        ("dist/bundle.js", False),
        ("node_modules/react/index.js", False),
        (".cache/x", False),
        ("tsconfig.tsbuildinfo", False),
    ]:
        assert is_source_path(package, package / path) is source, path


def test_editing_a_test_leaves_the_fingerprint(tmp_path: Path) -> None:
    views = a_package(tmp_path / "views")
    before = sources_fingerprint(views)
    (tmp_path / "views" / "test" / "bundle.test.ts").write_text("// another test\n")
    (tmp_path / "views" / "vitest.config.ts").write_text("export default {};\n")
    assert sources_fingerprint(views) == before
    (tmp_path / "views" / "src" / "index.js").write_text("changed\n")
    assert sources_fingerprint(views) != before


def test_the_version_is_the_built_files(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    assert bundle_version(dist) is None
    dist.mkdir()
    (dist / "bundle.js").write_text("a")
    manifest = {"apiVersion": VIEW_API_VERSION, "module": "bundle.js"}
    (dist / VIEW_BUNDLE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    first = bundle_version(dist)
    (dist / "unrelated.txt").write_text("x")
    assert bundle_version(dist) == first
    (dist / "bundle.js").write_text("b")
    assert bundle_version(dist) not in (None, first)


def a_fake_node(directory: Path, version: str) -> None:
    directory.mkdir()
    node = directory / "node"
    node.write_text(f"#!/bin/sh\necho {version}\n")
    node.chmod(node.stat().st_mode | stat.S_IEXEC)


async def test_an_old_node_is_refused_with_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    views = a_package(tmp_path / "views")
    a_fake_node(tmp_path / "bin", "v20.17.0")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(ViewsError, match=r"Node 22 or later.*is v20\.17\.0.*nvm install 22"):
        await build_views(views)


async def test_a_missing_node_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    views = a_package(tmp_path / "views")
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ViewsError, match="Node is not installed"):
        await build_views(views)


@needs_npm
async def test_a_failed_build_keeps_the_end_of_its_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    views = a_package(tmp_path / "views")
    (tmp_path / "views" / "src" / "index.js").write_text("syntax error\n")
    with pytest.raises(ViewsBuildError, match="failed") as raised:
        await build_views(views)
    assert "src/index.js:1 syntax error" in raised.value.output
    assert "failureErrorWithLog" not in raised.value.output  # stack frames are left out
    # The output still reaches the terminal as the build runs.
    assert "src/index.js:1 syntax error" in capsys.readouterr().err


# ---- the watcher ----


async def test_updates_start_with_the_current_state_and_say_when_nothing_happens(
    tmp_path: Path,
) -> None:
    watcher = ViewsWatcher(a_package(tmp_path / "views"))
    updates = watcher.updates(timeout=0.05)
    assert (await anext(updates)) == ViewsState(version=None, state="building")
    with anyio.fail_after(5):
        assert (await anext(updates)) is None
    watcher._publish(ViewsState(version="v", state="ready"))
    assert (await next_state(updates)) == ViewsState(version="v", state="ready")


@needs_npm
async def test_the_watcher_rebuilds_on_an_edit_and_not_on_a_test_edit(tmp_path: Path) -> None:
    views = a_package(tmp_path / "views")
    watcher = ViewsWatcher(views, debounce_ms=50)
    await watcher.refresh()
    first = watcher.state
    assert first.state == "ready" and first.version == bundle_version(bundle_dir(views))
    updates = watcher.updates(timeout=0.05)
    assert (await next_state(updates)) == first
    async with anyio.create_task_group() as tg:
        await tg.start(watcher.run)

        # An edit to a source: built, a new version.
        (tmp_path / "views" / "src" / "index.js").write_text("export default 2;\n")
        assert (await next_state(updates)).state == "building"
        second = await next_state(updates)
        assert second.state == "ready" and second.version not in (None, first.version)
        assert views_response(watcher, "bundle.js", first.version).status_code == 410

        # A failed build: the error and the end of its output, the last bundle still served.
        (tmp_path / "views" / "src" / "index.js").write_text("syntax error\n")
        assert (await next_state(updates)).state == "building"
        assert views_response(watcher, "bundle.js", second.version).body == b"export default 2;\n"
        failed = await next_state(updates)
        assert (failed.state, failed.version) == ("failed", second.version)
        assert "src/index.js:1 syntax error" in (failed.error or "")
        assert views_response(watcher, "bundle.js", second.version).body == b"export default 2;\n"

        # Fixed: built again.
        (tmp_path / "views" / "src" / "index.js").write_text("export default 3;\n")
        assert (await next_state(updates)).state == "building"
        assert (await next_state(updates)).state == "ready"
        built = (bundle_dir(views) / "bundle.js").stat().st_mtime_ns

        # A test edit: nothing built, nothing published.
        (tmp_path / "views" / "test" / "bundle.test.ts").write_text("// edited\n")
        with anyio.move_on_after(1.5):
            assert (await next_state(updates, timeout=2)) is None, "a test edit built the bundle"
        assert (bundle_dir(views) / "bundle.js").stat().st_mtime_ns == built
        tg.cancel_scope.cancel()


@needs_npm
async def test_a_rebuild_of_the_same_bundle_is_not_a_new_version(tmp_path: Path) -> None:
    views = a_package(tmp_path / "views")
    watcher = ViewsWatcher(views)
    await watcher.refresh()
    first = watcher.state
    # The build script is a source too; changing a comment in it rebuilds the same bundle.
    (tmp_path / "views" / "build.js").write_text(BUILD + "// a comment\n")
    await watcher.refresh()
    assert watcher.state == first


# ---- /api/events and /api/views/status ----


async def test_the_hub_follows_the_bundles_states(tmp_path: Path) -> None:
    watcher = ViewsWatcher(a_package(tmp_path / "views"))
    hub = EventHub()
    with hub.subscribe() as events:
        async with anyio.create_task_group() as tg:
            tg.start_soon(follow_views, watcher, hub)
            with anyio.fail_after(5):
                assert json.loads(await events.receive()) == {
                    "type": "views",
                    "version": None,
                    "state": "building",
                    "error": None,
                }
                watcher._publish(ViewsState(version="v1", state="ready"))
                assert json.loads(await events.receive())["version"] == "v1"
            tg.cancel_scope.cancel()


def a_views_project(root: Path) -> Project:
    return Project(
        root=root,
        name="p",
        modules=(),
        stale_after=timedelta(seconds=30),
        database_url="unused",
        schema="unused",
        views=ViewsConfig(package=root / "views"),
    )


async def first_response(app: Any, path: str) -> tuple[int, dict[str, str], bytes]:
    """The status, headers, and first body chunk of a request, then the client goes away."""
    messages: list[Message] = []
    got_body = anyio.Event()
    disconnect = anyio.Event()

    async def receive() -> Message:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body":
            got_body.set()

    scope = {
        "type": "http",
        "method": "GET",
        "path": urlsplit(path).path,
        "raw_path": urlsplit(path).path.encode(),
        "root_path": "",
        "query_string": urlsplit(path).query.encode(),
        "headers": [(b"host", b"127.0.0.1:8000")],
    }
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(app, scope, receive, send)
            await got_body.wait()
            disconnect.set()
    start = messages[0]
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, messages[1].get("body", b"")


async def test_a_project_without_views_has_no_views_status(tmp_path: Path) -> None:
    async with a_client(Database("postgresql+asyncpg://unused", pg_schema="unused")) as client:
        app = create_app(client)
        assert client.project.views is None
        assert (await first_response(app, "/api/views/status"))[0] == 404


@needs_npm
async def test_a_watching_app_builds_at_startup_and_streams_rebuilds(
    tmp_path: Path,
) -> None:
    views = a_package(tmp_path / "views")
    client = a_client(Database("postgresql+asyncpg://unused", pg_schema="unused"))
    client.project = a_views_project(tmp_path)
    app = create_app(client, frontend_dist=tmp_path / "frontend")
    async with app.router.lifespan_context(app):
        version = bundle_version(bundle_dir(views))
        assert version is not None  # built before serving
        status, _, body = await first_response(app, "/api/views/status")
        assert status == 200 and json.loads(body) == {
            "version": version,
            "state": "ready",
            "error": None,
        }
        async with a_page_socket(app) as page:
            assert (await page.next_event())["version"] == version


@needs_npm
@pytest.mark.timeout_seconds(30)
@pytest.mark.parametrize("previous_bundle", ["none", "valid", "incompatible"])
@pytest.mark.parametrize("failure", ["node", "build"])
async def test_an_initial_build_failure_keeps_serving_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_bundle: str,
    failure: str,
) -> None:
    views = a_package(tmp_path / "views")
    original = (views.package / "src/index.js").read_bytes()
    if previous_bundle != "none":
        await build_views(views)
    if previous_bundle == "incompatible":
        manifest_path = bundle_dir(views) / VIEW_BUNDLE_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        manifest["apiVersion"] = VIEW_API_VERSION + 1
        manifest_path.write_text(json.dumps(manifest))
    previous_version = bundle_version(bundle_dir(views))
    source = views.package / "src/index.js"
    source.write_text("syntax error\n")
    original_path = os.environ["PATH"]
    if failure == "node":
        a_fake_node(tmp_path / "bin", "v20.17.0")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{original_path}")
    client = a_client(Database("postgresql+asyncpg://unused", pg_schema="unused"))
    client.project = a_views_project(tmp_path)
    app = create_app(client, frontend_dist=tmp_path / "frontend")
    async with app.router.lifespan_context(app):
        status, headers, body = await first_response(app, "/api/views/status")
        state = json.loads(body)
        assert (status, headers["cache-control"]) == (200, "no-cache")
        assert state["state"] == "failed"
        assert state["version"] == (previous_version if previous_bundle == "valid" else None)
        assert ("Node 22 or later" if failure == "node" else "syntax error") in state["error"]
        async with a_page_socket(app) as page:
            assert await page.next_event() == {"type": "views", **state}
        status, _, body = await first_response(app, "/api/views/bundle.js")
        if previous_bundle == "valid":
            assert status == 200 and body == original
        else:
            assert status == 409 and state["error"].encode() == body
        assert bundle_version(bundle_dir(views)) == previous_version

        # Fixing the environment and source rebuilds and publishes without restarting the app.
        monkeypatch.setenv("PATH", original_path)
        source.write_text("export default () => ({ renderers: [] }); // fixed\n")
        with anyio.fail_after(20):
            while True:
                _, _, body = await first_response(app, "/api/views/status")
                state = json.loads(body)
                if state["state"] == "ready" and state["version"] not in (None, previous_version):
                    break
                await anyio.sleep(0.05)
        assert state["error"] is None
        status, headers, body = await first_response(
            app, f"/api/views/{VIEW_BUNDLE_MANIFEST_FILENAME}"
        )
        assert status == 200 and headers[VIEWS_VERSION_HEADER.lower()] == state["version"]
        status, _, body = await first_response(app, f"/api/views/bundle.js?v={state['version']}")
        assert status == 200 and body == source.read_bytes()


@pytest.mark.parametrize("configured_views", [False, True])
async def test_startup_without_node_keeps_the_viewer_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_views: bool
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    client = a_client(Database("postgresql+asyncpg://unused", pg_schema="unused"))
    if configured_views:
        a_package(tmp_path / "views")
        client.project = a_views_project(tmp_path)
    app = create_app(client, frontend_dist=tmp_path / "frontend")
    async with app.router.lifespan_context(app):
        status, _, body = await first_response(app, "/api/views/status")
        assert status == (200 if configured_views else 404)
        if configured_views:
            state = json.loads(body)
            assert state["state"] == "failed" and state["version"] is None
            assert "Node is not installed" in state["error"]


@pytest.mark.parametrize("server_exits", [False, True])
async def test_a_failed_server_startup_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_exits: bool
) -> None:
    from fxtr.runner.postgres.debug_web import server as server_module

    client = a_client(Database("postgresql+asyncpg://unused", pg_schema="unused"))
    client.project = a_views_project(tmp_path)
    monkeypatch.setattr(server_module.LocalProjectClient, "from_project", lambda *a, **kw: client)
    monkeypatch.setattr(server_module, "find_viewer", lambda project: None)

    async def failed_startup(server: Any, **kwargs: Any) -> None:
        server.should_exit = True
        if server_exits:
            raise SystemExit(3)

    monkeypatch.setattr(server_module.uvicorn.Server, "serve", failed_startup)
    with pytest.raises(SystemExit) as raised:
        await server_module.serve(open_browser=False)
    assert raised.value.code == 3


# ---- immutable snapshots and startup reconciliation ----


def capture_build(views: ViewsConfig) -> ViewBundle:
    """A synchronous test build that captures the current source and its input fingerprint."""
    dist = bundle_dir(views)
    dist.mkdir(exist_ok=True)
    source = (views.package / "src/index.js").read_bytes()
    (dist / "bundle.js").write_bytes(source)
    (dist / "bundle.css").write_bytes(b"/* " + source + b" */")
    (dist / VIEW_BUNDLE_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "apiVersion": VIEW_API_VERSION,
                "module": "bundle.js",
                "stylesheet": "bundle.css",
            }
        )
    )
    (dist / VIEW_BUNDLE_SOURCES_FILENAME).write_text(
        json.dumps({"sources": sources_fingerprint(views)})
    )
    return read_bundle(dist)


async def test_only_the_current_snapshot_is_served_and_publishing_expires_its_predecessor(
    tmp_path: Path,
) -> None:
    config = a_package(tmp_path / "views")
    first = capture_build(config)
    watcher = ViewsWatcher(config)
    await watcher.refresh()
    manifest = views_response(watcher, VIEW_BUNDLE_MANIFEST_FILENAME)
    assert manifest.status_code == 200 and manifest.headers[VIEWS_VERSION_HEADER] == first.version
    (config.package / "src/index.js").write_text("second build")
    second = capture_build(config)
    # Files on disk can change without changing the published snapshot's bytes.
    for name in first.files:
        response = views_response(watcher, name, first.version)
        assert response.status_code == 200 and response.body == first.files[name]

    # Publishing B immediately expires A; all of B's resources still match its manifest.
    await watcher.refresh()
    manifest = views_response(watcher, VIEW_BUNDLE_MANIFEST_FILENAME)
    assert manifest.status_code == 200 and manifest.headers[VIEWS_VERSION_HEADER] == second.version
    for name in first.files:
        assert views_response(watcher, name, first.version).status_code == 410
    for name in second.files:
        response = views_response(watcher, name, second.version)
        assert response.status_code == 200 and response.body == second.files[name]
    assert views_response(watcher, "bundle.js", "unknown").status_code == 410


@pytest.mark.parametrize("initial", [True, False])
async def test_edits_during_initial_or_later_builds_are_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: bool,
) -> None:
    from fxtr.runner.postgres.debug_web import views_watch

    config = a_package(tmp_path / "views")
    watcher = ViewsWatcher(config, debounce_ms=50)
    if not initial:
        capture_build(config)
    built = []

    async def build(views: ViewsConfig) -> ViewBundle:
        snapshot = capture_build(views)
        built.append(snapshot)
        if len(built) == 1:
            (views.package / "src/index.js").write_text("edited during the build")
        return snapshot

    monkeypatch.setattr(views_watch, "build_views", build)
    async with anyio.create_task_group() as tg:
        await tg.start(watcher.run)
        if not initial:
            (config.package / "src/index.js").write_text("start a later build")
            with anyio.fail_after(5):
                async for state in watcher.updates(timeout=0.05):
                    if state is not None and state.state == "ready" and len(built) >= 2:
                        break
        assert len(built) == 2
        current = watcher.bundle()
        assert current is not None and current.files["bundle.js"] == b"edited during the build"
        assert current.stale_reason(config) is None
        tg.cancel_scope.cancel()


async def test_new_watch_roots_reconcile_an_edit_during_resubscription(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxtr.runner.postgres.debug_web import views_watch

    config = a_package(tmp_path / "views")
    capture_build(config)
    watcher = ViewsWatcher(config)
    switched = False
    subscriptions = 0
    reconciled = anyio.Event()

    def roots() -> list[Path]:
        return [tmp_path if switched else config.package]

    async def watch(*args: Any, **kwargs: Any) -> AsyncIterator[set[Any]]:
        nonlocal switched, subscriptions
        subscriptions += 1
        if subscriptions == 1:
            switched = True
        else:
            (config.package / "src/index.js").write_text("edited while switching watch roots")
        yield set()  # A quiet first tick must still reconcile after installing each watch.
        if subscriptions == 2:
            reconciled.set()
        await anyio.sleep_forever()

    async def build(views: ViewsConfig) -> ViewBundle:
        return capture_build(views)

    monkeypatch.setattr(watcher, "_roots", roots)
    monkeypatch.setattr(views_watch.watchfiles, "awatch", watch)
    monkeypatch.setattr(views_watch, "build_views", build)
    async with anyio.create_task_group() as tg:
        await tg.start(watcher.run)
        with anyio.fail_after(5):
            await reconciled.wait()
        current = watcher.bundle()
        assert (
            current is not None
            and current.files["bundle.js"] == b"edited while switching watch roots"
        )
        tg.cancel_scope.cancel()
