"""The ``oci`` kind of remote: jobs in an OCI registry, commits in a git remote.

An OCI registry (GHCR, Docker Hub, ECR, a local ``registry:2``) holds container images, and it can
also hold other *artifacts*. This kind keeps each pushed job as one artifact in one repository of
the registry. The artifact has:

- the artifact type :data:`ARTIFACT_TYPE`;
- the empty config (:data:`EMPTY_CONFIG_MEDIA_TYPE`), because the artifact needs no config;
- one layer: the job's record file (:mod:`fxtr.project.job_record`), media type
  :data:`LAYER_MEDIA_TYPE`;
- annotations that tell what the job is (:func:`job_annotations`). A registry shows them without a
  download of the layer. The remote's ``annotations`` setting adds more (for example,
  ``org.opencontainers.image.source``, which GHCR uses to connect a package to a repository).

Tags name the artifacts. A push sets the tags that you give. If you give no tags, it sets a default
tag (:func:`default_tag`): the job's workflow, the time the job was created, and the job's id. If a
tag is already in the repository, the push moves it, as ``docker push`` does. Thus a job that you
push again after a resume moves its default tag to the newer record. The digest of each push names
that push for ever.

The record names the commits that its results were produced at. A pull can import the job only if
the receiving repository has those commits. So a push also pushes the commits to a git remote,
under fxtr's pin refs (refused if this checkout lacks one of them), and a pull fetches the commits
that the receiving repository does not have
(:meth:`~fxtr.project.environment.Environment.push_commits`,
:meth:`~fxtr.project.environment.Environment.fetch_commits`).

:class:`OciRemote` talks to the registry through a :class:`RegistryClient`. The client of the
``oci`` extra uses oras-py; tests use a fake client.
"""

from __future__ import annotations

import abc
import dataclasses
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fxtr.project.client import JobID, ProjectClient
from fxtr.project.config import REQUIRED, Project, RemoteConfig, Setting
from fxtr.project.environment import Environment
from fxtr.project.job_record import JOB_RECORD_TYPE, MAX_RECORD_FILE_BYTES, RecordFile
from fxtr.project.remotes import (
    Download,
    PushedJob,
    Remote,
    RemoteError,
    register_kind,
)

logger = logging.getLogger(__name__)

ARTIFACT_TYPE = "application/vnd.transluce.fxtr.job-record.v0.1"
"""The artifact type of a pushed job."""

LAYER_MEDIA_TYPE = "application/vnd.transluce.fxtr.job-record.car+gzip"
"""The media type of the layer: a job's record file, a gzipped CAR."""

EMPTY_CONFIG_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
"""The media type of the empty config, which OCI 1.1 defines for artifacts that need no config."""

ANNOTATION_PREFIX = "org.transluce.fxtr"
"""The prefix of the annotations that fxtr writes."""

CREATED_ANNOTATION = "org.opencontainers.image.created"
"""The standard annotation for the time the job was created, which fxtr writes."""

_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
"""The grammar of an OCI tag."""

_TAG_LENGTH = 128


@dataclasses.dataclass(frozen=True)
class Manifest:
    """The parts of an artifact's manifest that this kind reads.

    Attributes:
        digest: The digest of the manifest. It names the artifact.
        artifact_type: The artifact type, or ``None`` if the manifest has none.
        layers: The digest, media type, and size in bytes of each layer, in order.
        annotations: The annotations of the manifest.
    """

    digest: str
    artifact_type: str | None
    layers: tuple[tuple[str, str, int], ...]
    annotations: Mapping[str, str]


class RegistryClient(abc.ABC):
    """The operations on an OCI registry that this kind uses. A repository is written as
    ``host/path`` (for example, ``ghcr.io/org/jobs``)."""

    @abc.abstractmethod
    async def push_artifact(
        self,
        repository: str,
        *,
        layer: Path,
        layer_media_type: str,
        artifact_type: str,
        annotations: Mapping[str, str],
        tags: Sequence[str],
    ) -> str:
        """Upload a file as the one layer of an artifact, with the empty config, and set each tag
        to the artifact. Return the digest of the manifest.

        Args:
            repository: The repository.
            layer: The file to upload as the layer.
            layer_media_type: The media type of the layer.
            artifact_type: The artifact type of the manifest.
            annotations: The annotations of the manifest.
            tags: The tags to set. If a tag names another artifact, it moves to this one.

        Raises:
            RemoteError: If the registry refuses the upload or is not available.
        """

    @abc.abstractmethod
    async def fetch_manifest(self, repository: str, ref: str) -> Manifest | None:
        """Get the manifest that a tag or a digest (``sha256:…``) names. Return ``None`` if the
        repository has no such manifest.

        Raises:
            RemoteError: If the registry is not available.
        """

    @abc.abstractmethod
    async def fetch_layer(self, repository: str, digest: str, path: Path) -> None:
        """Download the blob with the digest to ``path``.

        Raises:
            RemoteError: If the repository has no such blob, or the registry is not available.
        """


def default_tag(workflow_name: str, created_at: datetime, job_id: JobID) -> str:
    """Return the default tag of a job: ``<workflow>-<YYYYMMDDTHHMMSSZ>-<job id>``. The workflow
    part keeps the characters that a tag allows and replaces the others with ``-``. If necessary,
    it is shortened so that the tag has at most 128 characters.

    Args:
        workflow_name: The name of the job's root workflow.
        created_at: The time the job was created.
        job_id: The job.
    """
    suffix = f"-{created_at.astimezone(UTC):%Y%m%dT%H%M%SZ}-{job_id}"
    workflow = re.sub(r"[^A-Za-z0-9_.-]", "-", workflow_name)[: _TAG_LENGTH - len(suffix)]
    if not workflow or not re.match(r"[A-Za-z0-9_]", workflow):
        workflow = ("job" + workflow)[: _TAG_LENGTH - len(suffix)]
    return workflow + suffix


def check_tag(tag: str) -> str:
    """Return ``tag`` if it is a valid OCI tag.

    Raises:
        RemoteError: If ``tag`` is not a valid OCI tag.
    """
    if not _TAG.fullmatch(tag):
        raise RemoteError(
            f"{tag!r} is not a valid tag: use letters, digits, '_', '.', and '-', at most 128"
            " characters, and start with a letter, a digit, or '_'"
        )
    return tag


def job_annotations(
    *, job_id: JobID, workflow_name: str, root: str, state: str, created_at: str, record: str
) -> dict[str, str]:
    """Return the annotations of a pushed job.

    Args:
        job_id: The job.
        workflow_name: The name of its root workflow.
        root: The pathname of its root invocation.
        state: Its state when it was pushed.
        created_at: The time it was created, in ISO 8601.
        record: The id of its record, as a cid string.
    """
    return {
        CREATED_ANNOTATION: created_at,
        f"{ANNOTATION_PREFIX}.job.id": str(job_id),
        f"{ANNOTATION_PREFIX}.job.workflow": workflow_name,
        f"{ANNOTATION_PREFIX}.job.root": root,
        f"{ANNOTATION_PREFIX}.job.state": state,
        f"{ANNOTATION_PREFIX}.record": record,
        f"{ANNOTATION_PREFIX}.record.format": JOB_RECORD_TYPE,
    }


def check_repository(value: str) -> str | None:
    """Check a repository's name. The registry client reads a name with no registry host as a
    repository on docker.io, so a typo would send a job's record there.

    Args:
        value: The repository, as the configuration gives it.

    Returns:
        What is wrong with it, or ``None`` if it is valid.
    """
    if "://" in value:
        return "with no scheme"
    host, _, path = value.partition("/")
    if not path or not ("." in host or ":" in host or host == "localhost"):
        return "starting with the registry's host"
    if ":" in path or "@" in path:
        return "with no tag or digest: a push sets the tags"
    return None


def check_annotations(value: Mapping[Any, Any]) -> str | None:
    """Check the annotations that a remote adds to each push.

    Args:
        value: The annotations, as the configuration gives them.

    Returns:
        What is wrong with them, or ``None`` if they are valid. Each key and each value must be a
        string. A key must not be one that fxtr writes: :data:`CREATED_ANNOTATION`, or a key that
        starts with :data:`ANNOTATION_PREFIX`.
    """
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, str):
            return "must map non-empty names to strings"
        if key == CREATED_ANNOTATION or key.startswith(f"{ANNOTATION_PREFIX}."):
            return f"must not set {key!r}, which fxtr writes"
    return None


class OciRemote(Remote):
    """A remote of the ``oci`` kind (see the module docstring).

    Args:
        config: The configuration of the remote.
        repository: The registry repository that holds the jobs (``host/path``).
        git_remote: The git remote that holds the commits.
        project: The project.
        environment: The environment, which runs git.
        registry: The client for the registry.
        annotations: More annotations for each push, beside those of :func:`job_annotations`.
            The configuration checks them (:func:`check_annotations`).
    """

    def __init__(
        self,
        config: RemoteConfig,
        *,
        repository: str,
        git_remote: str,
        project: Project,
        environment: Environment,
        registry: RegistryClient,
        annotations: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(config, project=project, environment=environment, git_remote=git_remote)
        self.repository = repository
        self.registry = registry
        self.annotations = dict(annotations or {})

    async def check_push(self, tags: Sequence[str], visibility: str | None) -> None:
        await super().check_push(tags, visibility)
        for tag in tags:
            check_tag(tag)

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
        job = file.record.job
        pushed_tags = tuple(tags) or (
            default_tag(job.workflow_name, datetime.fromisoformat(job.created_at), job_id),
        )
        status = await (await client.job(job_id)).status()
        logger.info("uploading it to %s", self.name)
        digest = await self.registry.push_artifact(
            self.repository,
            layer=path,
            layer_media_type=LAYER_MEDIA_TYPE,
            artifact_type=ARTIFACT_TYPE,
            annotations={
                **self.annotations,
                **job_annotations(
                    job_id=job_id,
                    workflow_name=job.workflow_name,
                    root=job.root,
                    state=status.state,
                    created_at=job.created_at,
                    record=file.root.cid_str(),
                ),
            },
            tags=pushed_tags,
        )
        return PushedJob(self.name, job_id, pushed_tags, pushed_tags[0], digest)

    async def download(self, ref: str, path: Path) -> Download:
        manifest = await self.registry.fetch_manifest(self.repository, ref.removeprefix("@"))
        if manifest is None:
            raise RemoteError(f"{ref!r} names nothing in {self.repository} ({self.name})")
        if ref.startswith("@") and manifest.digest != ref[1:]:
            raise RemoteError(f"{self.repository} returned a manifest that is not {ref}")
        if manifest.artifact_type != ARTIFACT_TYPE or len(manifest.layers) != 1:
            raise RemoteError(
                f"{ref!r} in {self.repository} is not a job pushed by fxtr (artifact type"
                f" {manifest.artifact_type!r})"
            )
        ((layer_digest, _, layer_size),) = manifest.layers
        if layer_size > MAX_RECORD_FILE_BYTES:
            raise RemoteError(
                f"{ref!r} in {self.repository} is {layer_size} bytes, over the"
                f" {MAX_RECORD_FILE_BYTES} a job record file may be"
            )
        await self.registry.fetch_layer(self.repository, layer_digest, path)
        record = manifest.annotations.get(f"{ANNOTATION_PREFIX}.record")
        job = manifest.annotations.get(f"{ANNOTATION_PREFIX}.job.id")
        if (record is None) != (job is None):
            raise RemoteError(f"{ref!r} in {self.repository} must name both its record and job")
        return Download(f"{ref!r} in {self.repository}", record_cid=record, job_id=job)


OCI_SETTINGS = (
    Setting("repository", str, REQUIRED, "a registry repository, host/path", check_repository),
    Setting("git_remote", str, "origin", "the name of a git remote"),
    Setting("insecure", bool, False, "true to talk to the registry over plain HTTP"),
    Setting("auth_file", str, None, "the path of a credential file"),
    Setting("annotations", dict, {}, "a table of annotation names to strings", check_annotations),
)
"""The settings of an ``oci`` remote, other than ``kind``."""


def build(
    config: RemoteConfig, settings: Mapping[str, Any], project: Project, environment: Environment
) -> Remote:
    """Build an ``oci`` remote with the oras-py registry client of the ``oci`` extra."""
    try:
        from fxtr.project.remotes.oras_client import OrasRegistryClient
    except ImportError as exc:
        raise RemoteError(f"Remote {config.name!r} needs the oci extra: install fxtr[oci]") from exc
    auth_file = settings["auth_file"]
    return OciRemote(
        config,
        repository=settings["repository"],
        git_remote=settings["git_remote"],
        project=project,
        environment=environment,
        registry=OrasRegistryClient(
            insecure=settings["insecure"],
            auth_file=None if auth_file is None else project.root / Path(auth_file).expanduser(),
        ),
        annotations=settings["annotations"],
    )


register_kind("oci", OCI_SETTINGS, build)


__all__ = [
    "ANNOTATION_PREFIX",
    "ARTIFACT_TYPE",
    "CREATED_ANNOTATION",
    "EMPTY_CONFIG_MEDIA_TYPE",
    "LAYER_MEDIA_TYPE",
    "OCI_SETTINGS",
    "Manifest",
    "OciRemote",
    "RegistryClient",
    "job_annotations",
    "build",
    "check_annotations",
    "check_repository",
    "check_tag",
    "default_tag",
]
