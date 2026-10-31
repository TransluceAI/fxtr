"""Where a project's viewer runs (``fxtr.project.viewer``), and a launch showing its job there
(``fxtr.project.running``'s ``show_job`` and ``wait_for_job``, ``fxtr run --no-open``): the
record ``fxtr view`` keeps, finding the project's viewer by ``/api/info``, a record left by a
viewer that died or another project's viewer ignored, the job announced to the viewer's open
pages, and a new browser tab opened only when none heard it and it should be."""

from __future__ import annotations

import dataclasses
import http.server
import json
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from fxtr.project import running
from fxtr.project import viewer as viewer_module
from fxtr.project.config import Project
from fxtr.project.viewer import (
    NO_BROWSER_ENV,
    VIEWER_RECORD,
    announce_job,
    find_viewer,
    job_url,
    project_identity,
    record_viewer,
    recorded_viewer,
    viewer_record,
)

pytestmark = pytest.mark.anyio


def a_project(root: Path, schema: str = "s") -> Project:
    root.mkdir(parents=True, exist_ok=True)
    return Project(
        root=root,
        name="p",
        modules=(),
        stale_after=timedelta(seconds=30),
        database_url="unused",
        schema=schema,
    )


@pytest.fixture(autouse=True)
def no_default_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    """No viewer at the default URL."""
    monkeypatch.setattr(viewer_module, "DEFAULT_VIEWER_URL", "http://127.0.0.1:9/")


@pytest.fixture
def a_viewer() -> Iterator[Any]:
    """Start fake viewers answering ``/api/info`` for a project; yields the starter."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def start(project: Project) -> str:
        body = json.dumps({**project_identity(project), "fxtrVersion": "0"}).encode()

        class Info(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                found = self.path == "/api/info"
                self.send_response(200 if found else 404)
                self.end_headers()
                if found:
                    self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Info)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_the_recorded_viewer_of_the_project_is_found(tmp_path: Path, a_viewer: Any) -> None:
    project = a_project(tmp_path / "p")
    assert find_viewer(project) is None
    url = a_viewer(project)
    record_viewer(project, url)
    assert viewer_record(project) == tmp_path / "p" / VIEWER_RECORD
    assert find_viewer(project) == url
    assert recorded_viewer(project) == url


def test_the_default_url_is_tried_without_a_record(
    tmp_path: Path, a_viewer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = a_project(tmp_path / "p")
    monkeypatch.setattr(viewer_module, "DEFAULT_VIEWER_URL", a_viewer(project))
    assert find_viewer(project) == viewer_module.DEFAULT_VIEWER_URL


def test_another_projects_viewer_and_a_dead_ones_record_are_ignored(
    tmp_path: Path, a_viewer: Any
) -> None:
    project = a_project(tmp_path / "p")
    other_schema = a_project(tmp_path / "p", schema="other")
    other_root = a_project(tmp_path / "q")
    record_viewer(project, a_viewer(other_schema))
    assert find_viewer(project) is None
    record_viewer(project, a_viewer(other_root))
    assert find_viewer(project) is None
    record_viewer(project, "http://127.0.0.1:9/")  # nothing listens there any more
    assert find_viewer(project) is None
    assert recorded_viewer(project) == "http://127.0.0.1:9/"


def test_a_record_is_only_a_preference_for_its_project_identity(tmp_path: Path) -> None:
    project = a_project(tmp_path / "p")
    record_viewer(project, "http://127.0.0.1:8001/")
    assert recorded_viewer(project) == "http://127.0.0.1:8001/"
    assert recorded_viewer(dataclasses.replace(project, schema="other")) is None
    assert recorded_viewer(dataclasses.replace(project, database_url="other")) is None


@pytest.mark.parametrize("record", ["{", "[]", "null", '{"url": "http://127.0.0.1:8001/"}'])
def test_an_invalid_record_is_not_a_port_preference(tmp_path: Path, record: str) -> None:
    project = a_project(tmp_path)
    record_viewer(project, "http://127.0.0.1:8001/")
    viewer_record(project).write_text(record)
    assert recorded_viewer(project) is None
    assert find_viewer(project) is None


def test_switching_databases_does_not_reuse_the_running_viewer(
    tmp_path: Path, a_viewer: Any
) -> None:
    project = dataclasses.replace(
        a_project(tmp_path), database_url="postgresql+asyncpg://user:secret@localhost/first"
    )
    record_viewer(project, a_viewer(project))
    assert find_viewer(project) is not None
    changed = dataclasses.replace(
        project, database_url=project.database_url.replace("first", "second")
    )
    assert find_viewer(changed) is None


def test_database_identity_normalizes_urls_without_publishing_credentials(tmp_path: Path) -> None:
    project = dataclasses.replace(
        a_project(tmp_path),
        database_url="postgresql+asyncpg://user:top-secret@LOCALHOST/my%20db?ssl=require&timeout=5",
    )
    equivalent = dataclasses.replace(
        project,
        database_url="postgresql://user:different-secret@localhost:5432/my%20db?timeout=5&ssl=require",
    )
    assert project_identity(project) == project_identity(equivalent)
    record_viewer(project, "http://127.0.0.1:8001/")
    public = json.dumps(project_identity(project)) + viewer_record(project).read_text()
    assert "top-secret" not in public and "postgresql" not in public and "user" not in public


@pytest.mark.parametrize(
    "other",
    [
        "postgresql+asyncpg://user@elsewhere/db",
        "postgresql+asyncpg://user@localhost:5433/db",
        "postgresql+asyncpg://user@localhost/another",
        "postgresql+asyncpg://user@localhost/db?host=/tmp/another-socket",
    ],
)
def test_database_identity_includes_connection_destinations(tmp_path: Path, other: str) -> None:
    project = dataclasses.replace(
        a_project(tmp_path), database_url="postgresql+asyncpg://user@localhost/db"
    )
    assert project_identity(project) != project_identity(
        dataclasses.replace(project, database_url=other)
    )


# ---- a launch shows its job ----


class Client:
    """The part of a project client ``show_job`` uses."""

    def __init__(self, url: str | None) -> None:
        self.url = url

    async def viewer_url(self) -> str | None:
        return self.url


VIEWER = "http://127.0.0.1:8000/"


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What a launch opens in a browser; the test suite's own off switch turned back on."""
    urls: list[str] = []
    monkeypatch.setattr(viewer_module.webbrowser, "open", urls.append)
    monkeypatch.delenv(NO_BROWSER_ENV, raising=False)
    return urls


class Announcements:
    """The launches announced to the viewer, and how many of its pages hear each: ``None``, a
    viewer that answers no count, unless a test says otherwise."""

    def __init__(self) -> None:
        self.jobs: list[str] = []
        self.heard: int | None = None

    def __call__(self, viewer: str, job_id: str) -> int | None:
        assert viewer == VIEWER
        self.jobs.append(job_id)
        return self.heard


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch) -> Announcements:
    announcements = Announcements()
    monkeypatch.setattr(running, "announce_job", announcements)
    return announcements


async def test_a_launch_shows_its_job_in_the_viewers_open_tabs(
    opened: list[str], announced: Announcements, capsys: pytest.CaptureFixture[str]
) -> None:
    announced.heard = 2
    await running.show_job(Client(VIEWER), "j1")  # type: ignore[arg-type]
    assert announced.jobs == ["j1"] and opened == []
    out = capsys.readouterr().out
    assert "watch it at http://127.0.0.1:8000/#job=j1" in out
    assert "sent it to the viewer's 2 open tabs" in out


@pytest.mark.parametrize("heard", [0, None])
async def test_a_launch_no_open_page_heard_opens_a_new_tab(
    opened: list[str], announced: Announcements, heard: int | None
) -> None:
    announced.heard = heard
    await running.show_job(Client(VIEWER), "j1")  # type: ignore[arg-type]
    assert announced.jobs == ["j1"]
    assert opened == [job_url(VIEWER, "j1")]


async def test_a_launch_without_a_viewer_says_how_to_start_one(
    opened: list[str], announced: Announcements, capsys: pytest.CaptureFixture[str]
) -> None:
    await running.show_job(Client(None), "j1")  # type: ignore[arg-type]
    assert opened == [] and announced.jobs == []
    assert "no viewer is running for this project: start one with `uv run fxtr view`" in (
        capsys.readouterr().out
    )


async def test_showing_is_turned_off_by_argument_or_environment(
    opened: list[str],
    announced: Announcements,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = Client(VIEWER)
    await running.show_job(client, "j1", open_browser=False)  # type: ignore[arg-type]
    monkeypatch.setenv(NO_BROWSER_ENV, "1")
    await running.show_job(client, "j2")  # type: ignore[arg-type]
    assert opened == [] and announced.jobs == []  # nor are the viewer's open pages told
    out = capsys.readouterr().out
    assert "#job=j1" in out and "#job=j2" in out  # the link is printed all the same


async def test_a_browser_that_fails_to_open_is_reported(
    opened: list[str],
    announced: Announcements,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(url: str) -> bool:
        raise RuntimeError("no browser here")

    monkeypatch.setattr(viewer_module.webbrowser, "open", fail)
    await running.show_job(Client(VIEWER), "j1")  # type: ignore[arg-type]
    assert "could not open a browser: no browser here" in capsys.readouterr().out


def test_a_launch_is_announced_as_json_and_answered_with_a_count() -> None:
    requests: list[tuple[str, str, bytes]] = []

    class Launches(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, self.headers["Content-Type"], body))
            found = self.path == "/api/launches"
            self.send_response(200 if found else 404)
            self.end_headers()
            if found:
                self.wfile.write(b'{"listeners": 3}')

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Launches)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        assert announce_job(url, "j1") == 3
        assert requests == [("/api/launches", "application/json", b'{"jobId": "j1"}')]
        assert announce_job(url + "elsewhere/", "j1") is None  # a viewer without the route
    finally:
        server.shutdown()
        server.server_close()
    assert announce_job("http://127.0.0.1:9/", "j1") is None  # nothing listens there
