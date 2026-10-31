"""Where a project's viewer runs, so a launch can show its job there.

``fxtr view`` serves one project, the one its working directory (or ``--project``) is in, at a URL
of its own (automatically selected unless ``--port`` says otherwise). It keeps a *record* of its
last successful URL (:func:`record_viewer`): :data:`VIEWER_RECORD` in the project root, a small
JSON file of this machine's that the project's ``.gitignore`` leaves out (``fxtr new`` writes
it so). The viewer answers ``GET /api/info`` with the root, schema, and an opaque fingerprint
of its database target. Neither that endpoint nor the record contains connection credentials.

:func:`find_viewer` is how a launcher finds the viewer of its project: the recorded URL, then the
default one, whichever answers ``/api/info`` for this project; a record left by a viewer that died
answers nothing and is not reused as a running viewer. The record persists after shutdown so
automatic selection can prefer that port on restart. :func:`announce_job` tells the viewer about a
launched job, so the pages of it open in a browser can show it, and answers how many heard it;
:func:`open_job` opens a job in a new browser tab, unless :data:`NO_BROWSER_ENV` is set (as the
test suites set it).

Standard library only: launchers import it without the viewer's extra.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from fxtr.project.config import Project

DEFAULT_VIEWER_PORT = 8000
"""The first port automatic selection tries without a remembered address."""

DEFAULT_VIEWER_URL = f"http://127.0.0.1:{DEFAULT_VIEWER_PORT}/"
"""The default address probed when looking for a running viewer."""

INFO_PATH = "api/info"
"""The viewer's route answering what it serves, relative to its URL."""

LAUNCHES_PATH = "api/launches"
"""The viewer's route a launched job is announced at, relative to its URL."""

NO_BROWSER_ENV = "FXTR_NO_BROWSER"
"""Set (to anything but empty or ``0``), nothing fxtr runs opens a browser for a launched job."""

VIEWER_RECORD = Path(".fxtr") / "viewer.json"
"""Where, relative to the project root, ``fxtr view`` records its last successful URL."""

_PROBE_TIMEOUT = 0.5
"""Seconds to wait for a viewer's ``/api/info``: it is a local server, or none."""

_ANNOUNCE_TIMEOUT = 3.0
"""Seconds to wait for a viewer to answer an announced launch, which it looks up first."""


def project_identity(project: Project) -> dict[str, str]:
    """The root, schema, and database target served by a viewer.

    The target fingerprint normalizes the driver suffix, DNS host case, default Postgres port,
    and URL escapes. Passwords are excluded; the user is retained because it can select the
    default database and affect access. Query options are sorted and retained conservatively
    since they can override the destination or connection behavior. Only the digest is public.
    """
    url = urlsplit(project.database_url)
    target = (
        url.scheme.split("+", 1)[0],
        unquote(url.username or ""),
        url.hostname or "",
        url.port or 5432,
        unquote(url.path),
        sorted((k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k != "password"),
    )
    return {
        "root": str(project.root.resolve()),
        "schema": project.schema,
        "database": hashlib.sha256(json.dumps(target).encode()).hexdigest(),
    }


def viewer_record(project: Project) -> Path:
    """The file recording where ``project``'s viewer runs (:data:`VIEWER_RECORD`)."""
    return project.root / VIEWER_RECORD


def record_viewer(project: Project, url: str) -> None:
    """Record that ``project``'s viewer runs at ``url`` (``fxtr view``, once it listens)."""
    path = viewer_record(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"url": url, "pid": os.getpid(), **project_identity(project)}
    scratch = path.with_suffix(f".{os.getpid()}.tmp")
    scratch.write_text(json.dumps(record) + "\n")
    scratch.replace(path)


def recorded_viewer(project: Project) -> str | None:
    """The last successful URL recorded for this project identity, whether or not it is live."""
    try:
        record = json.loads(viewer_record(project).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not all(
        record.get(k) == v for k, v in project_identity(project).items()
    ):
        return None
    url = record.get("url")
    return url if isinstance(url, str) else None


def _ask(request: urllib.request.Request | str, timeout: float) -> Any:
    """The JSON a local viewer answers ``request`` with, ``None`` when it answers none."""
    # The viewer is local: no proxy the environment names is between here and it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, ValueError):
        return None


def viewer_info(url: str) -> dict[str, Any] | None:
    """What the viewer at ``url`` says it serves (``/api/info``), ``None`` when nothing there
    answers as a viewer."""
    info = _ask(url + INFO_PATH, _PROBE_TIMEOUT)
    return info if isinstance(info, dict) else None


def find_viewer(project: Project) -> str | None:
    """The URL of ``project``'s running viewer (see the module docstring), ``None`` when none
    runs. Makes local HTTP requests: call it off the event loop."""
    candidates: list[str] = []
    if (recorded := recorded_viewer(project)) is not None:
        candidates.append(recorded)
    candidates.append(DEFAULT_VIEWER_URL)
    identity = project_identity(project)
    for url in dict.fromkeys(candidates):
        info = viewer_info(url)
        if info is not None and all(info.get(k) == v for k, v in identity.items()):
            return url
    return None


def job_url(viewer: str, job_id: str) -> str:
    """The URL showing job ``job_id`` in the viewer at ``viewer``."""
    return f"{viewer}#job={job_id}"


def announce_job(viewer: str, job_id: str) -> int | None:
    """Tell the viewer at ``viewer`` that job ``job_id`` was launched (or resumed), so the pages of
    it open in a browser can show it. Returns how many pages heard it, ``None`` when the viewer
    answered no such count (one that does not announce launches, or none). Makes a local HTTP
    request: call it off the event loop."""
    request = urllib.request.Request(
        viewer + LAUNCHES_PATH,
        data=json.dumps({"jobId": job_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    answer = _ask(request, _ANNOUNCE_TIMEOUT)
    listeners = answer.get("listeners") if isinstance(answer, dict) else None
    return listeners if isinstance(listeners, int) else None


def browser_disabled() -> bool:
    """Whether :data:`NO_BROWSER_ENV` turns opening a browser off."""
    return os.environ.get(NO_BROWSER_ENV, "") not in ("", "0")


def open_job(viewer: str, job_id: str) -> str | None:
    """Open job ``job_id`` in a new browser tab of the viewer at ``viewer``, unless
    :data:`NO_BROWSER_ENV` says not to. Returns why it could not, ``None`` when it did or was not
    to. Blocks while the browser starts: call it off the event loop."""
    if browser_disabled():
        return None
    try:
        webbrowser.open(job_url(viewer, job_id))
    except Exception as exc:  # noqa: BLE001 - a job outlives a missing browser
        return str(exc)
    return None


__all__ = [
    "DEFAULT_VIEWER_URL",
    "DEFAULT_VIEWER_PORT",
    "INFO_PATH",
    "LAUNCHES_PATH",
    "NO_BROWSER_ENV",
    "VIEWER_RECORD",
    "announce_job",
    "browser_disabled",
    "find_viewer",
    "job_url",
    "open_job",
    "project_identity",
    "record_viewer",
    "recorded_viewer",
    "viewer_info",
    "viewer_record",
]
