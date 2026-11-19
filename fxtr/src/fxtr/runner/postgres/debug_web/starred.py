"""The jobs someone starred in the viewer's job picker, each with an optional name of their own.

They are kept in :data:`STARRED_FILE` in the project root, beside the viewer's record
(:mod:`fxtr.project.viewer`): a small JSON file of this checkout's, which the project's
``.gitignore`` leaves out. They are not in the project's database: a star is how one person
arranges their picker, not a fact about the job, and the viewer never writes to the database.
A job id the database no longer has is harmless here; the picker shows only stars whose job it
finds.

The file is ``{"jobs": {"<job id>": {"name": "<name or empty>", "starredAt": "<ISO time>"}}}``.
Writes replace the file whole through a scratch file, so a reader never sees half of one.
Updates in one process are serialized; two viewers of one checkout writing at the same moment
would keep the later write.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fxtr.project.config import Project

STARRED_FILE = Path(".fxtr") / "starred.json"
"""Where, relative to the project root, the starred jobs are kept."""

MAX_NAME_LENGTH = 200
"""The longest name a starred job may be given."""

_write_lock = threading.Lock()
"""Serialize the complete read/modify/write operation across the viewer's worker threads."""


def read_starred(project: Project) -> dict[str, dict[str, str]]:
    """The starred jobs by job id, each ``{"name", "starredAt"}``; none when the file is missing
    or is not what this module writes."""
    try:
        stored = json.loads((project.root / STARRED_FILE).read_text())
    except (OSError, ValueError):
        return {}
    jobs = stored.get("jobs") if isinstance(stored, dict) else None
    if not isinstance(jobs, dict):
        return {}
    return {
        job_id: {"name": str(entry.get("name", "")), "starredAt": str(entry.get("starredAt", ""))}
        for job_id, entry in jobs.items()
        if isinstance(job_id, str) and isinstance(entry, dict)
    }


def star_request(job_id: str, body: bytes) -> tuple[str, str]:
    """The job and name a ``PUT /api/starred/{job_id}`` asks to star: the id as a canonical UUID
    and the body's ``{"name": ...}`` (missing means none), stripped.

    Raises:
        ValueError: If the id is not a UUID, or the body is not that shape or its name is too
            long.
    """
    canonical = str(uuid.UUID(job_id))
    try:
        request: Any = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"the body is not JSON: {exc}") from exc
    name = request.get("name", "") if isinstance(request, dict) else None
    if not isinstance(name, str):
        raise ValueError('the body must be {"name": <string>}')
    name = name.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"a starred job's name is at most {MAX_NAME_LENGTH} characters")
    return canonical, name


def star(project: Project, job_id: str, name: str) -> dict[str, dict[str, str]]:
    """Star ``job_id`` under ``name``, or rename it if it is starred (keeping when it was); the
    starred jobs afterwards."""
    with _write_lock:
        jobs = read_starred(project)
        starred_at = jobs.get(job_id, {}).get("starredAt") or datetime.now(UTC).isoformat()
        jobs[job_id] = {"name": name, "starredAt": starred_at}
        _write(project, jobs)
        return jobs


def unstar(project: Project, job_id: str) -> dict[str, dict[str, str]]:
    """Remove ``job_id``'s star, if it has one; the starred jobs afterwards."""
    with _write_lock:
        jobs = read_starred(project)
        if jobs.pop(job_id, None) is not None:
            _write(project, jobs)
        return jobs


def _write(project: Project, jobs: dict[str, dict[str, str]]) -> None:
    path = project.root / STARRED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(f".{os.getpid()}.tmp")
    scratch.write_text(json.dumps({"jobs": jobs}, indent=2) + "\n")
    scratch.replace(path)
