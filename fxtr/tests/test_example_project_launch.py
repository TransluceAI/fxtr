"""Launches the example project the way a user does, in a copy of it that is a fresh git repository
with its own venv: the real environment (git, uv) pins the copy's commit and checks its venv, and
a launcher runs a job on the test database. Opt-in, since it syncs a venv and takes a while: set
``FXTR_TEST_EXAMPLE_PROJECT=1`` (and ``FXTR_TEST_DATABASE_URL``).

The copy, rather than the project in place, keeps the test from writing pins or synthetic commits
into this repository, and is the shape a user's project has: a project in its own repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import anyio
import pytest
from anyio import to_thread

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = REPO_ROOT / "example_projects" / "basic_project"
OPT_IN_ENV_VAR = "FXTR_TEST_EXAMPLE_PROJECT"


def _run(*command: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, (
        f"{' '.join(command)} failed ({completed.returncode}):"
        f"\n{completed.stdout}\n{completed.stderr}"
    )
    return completed.stdout


# Locking, syncing, and the launch (up to 120 seconds) take far longer than the default timeout.
@pytest.mark.timeout_seconds(300)
async def test_a_launcher_runs_a_job_of_a_copied_project(tmp_path: Path) -> None:
    if not os.environ.get(OPT_IN_ENV_VAR):
        pytest.skip(f"{OPT_IN_ENV_VAR} is not set")
    url = os.environ.get("FXTR_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("FXTR_TEST_DATABASE_URL is not set")
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("uv and git are required")
    pytest.importorskip("sqlalchemy")
    from fxtr.runner.postgres.database import Database

    schema = f"test_{uuid.uuid4().hex}"
    copy = tmp_path / "basic_project"
    shutil.copytree(
        PROJECT,
        copy,
        ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", "fxtr.local.toml", "node_modules", "dist"
        ),
    )
    # The copy depends on this checkout's fxtr, by absolute path, and inherits the shared pyright
    # policy the same way.
    pyproject = copy / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text()
        .replace('path = "../../fxtr"', f'path = "{REPO_ROOT / "fxtr"}"')
        .replace(
            'extends = "../../pyright-base.json"', f'extends = "{REPO_ROOT / "pyright-base.json"}"'
        )
    )
    (copy / "fxtr.local.toml").write_text(f'database_url = "{url}"\nschema = "{schema}"\n')
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "FXTR_DATABASE_URL")}
    try:
        # Lock and sync first (the lock changes with the path), then commit: the checkout is then
        # clean, and the launch pins its commit without any flag.
        _run("uv", "lock", cwd=copy, env=env)
        _run("uv", "sync", cwd=copy, env=env)
        _run("git", "init", "-q", cwd=copy)
        _run("git", "add", "-A", cwd=copy)
        _run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=copy)
        with anyio.fail_after(120):
            out = await to_thread.run_sync(
                lambda: _run(
                    "uv",
                    "run",
                    "python",
                    "launch_bio_refinement.py",
                    cwd=copy,
                    env=env,
                )
            )
        assert ": succeeded" in out, out
        # The pin landed in the copy's repository, under fxtr's ref, and nowhere else.
        head = _run("git", "rev-parse", "HEAD", cwd=copy).strip()
        refs = _run("git", "for-each-ref", "refs/fxtr/pins/", cwd=copy)
        assert head in refs, refs
        assert _run("git", "status", "--porcelain", cwd=copy) == ""
    finally:
        database = Database(url, pg_schema=schema)
        try:
            await database.drop_schema()
        finally:
            await database.dispose()
