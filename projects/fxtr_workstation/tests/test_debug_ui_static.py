"""Smoke tests for the ``/debug`` static-file route.

The test creates a temp dir with a stub ``index.html`` and points
the workstation at it. Doesn't depend on ``npm install`` /
``npm run build`` having happened.
"""

from __future__ import annotations

import logging
import pathlib

import anyio
import httpx
import pytest
from fxtr_workstation import WorkstationServer

DEADLOCK_TIMEOUT = 10.0
_STUB_INDEX = "<!doctype html><html><body><div id='root'>STUB</div></body></html>"


@pytest.fixture
def stub_dist(tmp_path: pathlib.Path) -> pathlib.Path:
    """A temp dir with the minimum a Vite build produces:
    ``index.html`` + an ``assets/`` subdir."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(_STUB_INDEX)
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("// stub\n")
    return dist


async def test_debug_route_serves_index_html(stub_dist: pathlib.Path) -> None:
    """``GET /debug/`` returns the bundled ``index.html``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer(debug_ui_dist_path=stub_dist) as ws:
            async with httpx.AsyncClient() as http:
                resp = await http.get(f"{ws.debug_http_url}/")
            assert resp.status_code == 200
            assert "STUB" in resp.text


async def test_debug_route_serves_assets(stub_dist: pathlib.Path) -> None:
    """Asset files inside dist/ are served too."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer(debug_ui_dist_path=stub_dist) as ws:
            async with httpx.AsyncClient() as http:
                resp = await http.get(f"{ws.debug_http_url}/assets/app.js")
            assert resp.status_code == 200
            assert resp.text == "// stub\n"


async def test_debug_route_404_when_not_configured() -> None:
    """Without ``debug_ui_dist_path``, the route is not mounted."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with httpx.AsyncClient() as http:
                resp = await http.get(f"{ws.debug_http_url}/")
            assert resp.status_code == 404


async def test_debug_route_skipped_when_path_missing(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If the configured path doesn't exist, log a warning and don't
    mount the route. (Callers who pass
    ``WorkstationServer.bundled_debug_ui_dist_path()`` without having
    built the UI take this path.)"""
    caplog.set_level(logging.WARNING, logger="fxtr_workstation.ws_app")
    missing = tmp_path / "does-not-exist"
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer(debug_ui_dist_path=missing) as ws:
            async with httpx.AsyncClient() as http:
                resp = await http.get(f"{ws.debug_http_url}/")
            assert resp.status_code == 404
    assert any(
        "does not exist" in r.message and "pnpm install" in r.message for r in caplog.records
    )


def test_bundled_debug_ui_dist_path_points_inside_package() -> None:
    """Sanity: the helper returns a path inside the package — even
    if the directory itself doesn't exist (build hasn't been run)."""
    p = WorkstationServer.bundled_debug_ui_dist_path()
    assert p.name == "dist"
    assert p.parent.name == "debug_ui"
    assert p.parent.parent.name == "fxtr_workstation"


async def test_bundled_dist_serves_real_index_html_if_built() -> None:
    """If the in-package debug UI has been built (``pnpm install &&
    pnpm build`` in ``fxtr_workstation/debug_ui/``), serving from
    that directory works end-to-end. Skipped if the build hasn't
    been run."""
    bundled = WorkstationServer.bundled_debug_ui_dist_path()
    if not (bundled / "index.html").is_file():
        pytest.skip("debug UI not built; run `pnpm install && pnpm build`")
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer(debug_ui_dist_path=bundled) as ws:
            async with httpx.AsyncClient() as http:
                resp = await http.get(f"{ws.debug_http_url}/")
            assert resp.status_code == 200
            # Vite-built index.html references the JS bundle.
            assert '<div id="root">' in resp.text or "<div id='root'>" in resp.text
            assert "/assets/" in resp.text
