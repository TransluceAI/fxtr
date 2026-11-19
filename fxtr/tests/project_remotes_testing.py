"""An in-memory kind of remote for tests: ``memory``, registered when this module
is imported.

Its remotes keep record files in :data:`STORE`, one store that every project in a test shares, as
a real remote would be shared. They implement only the steps a kind differs in (``upload`` and
``download``), so they test what is above a kind: the push and pull flow of ``Remote``, choosing
a remote, the functions over the client, and the CLI.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fxtr.project.client import JobID, ProjectClient
from fxtr.project.config import Project, RemoteConfig
from fxtr.project.environment import Environment
from fxtr.project.job_record import RecordFile
from fxtr.project.remotes import (
    Download,
    PushedJob,
    Remote,
    RemoteError,
    register_kind,
)

STORE: dict[str, bytes] = {}
"""What the in-memory remotes hold, by tag and by digest: one store all their projects share."""


class MemoryRemote(Remote):
    """A remote kept in :data:`STORE`."""

    async def upload(
        self,
        client: ProjectClient,
        job_id: JobID,
        file: RecordFile,
        path: Path,
        *,
        tags: Sequence[str],
        visibility: str | None,
    ) -> PushedJob:
        data = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        tags = tuple(tags) or (f"job-{job_id}",)
        unchanged = digest in STORE  # held already, as a hub reports it: the tags are set anyway
        for tag in (*tags, digest):
            STORE[tag] = data
        return PushedJob(self.name, job_id, tags, tags[0], digest, unchanged=unchanged)

    async def download(self, ref: str, path: Path) -> Download:
        data = STORE.get(ref.removeprefix("@"))
        if data is None:
            raise RemoteError(f"{ref!r} names nothing on {self.name}")
        path.write_bytes(data)
        return Download(repr(ref))


def build_memory(
    config: RemoteConfig, settings: Mapping[str, Any], project: Project, env: Environment
) -> Remote:
    return MemoryRemote(config, project=project, environment=env, git_remote="origin")


register_kind("memory", (), build_memory)
