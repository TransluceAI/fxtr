"""Remotes: places that a project pushes jobs to and pulls jobs from.

A project's remotes are configured by name, as git's are: committed in ``pyproject.toml``,
so every checkout has them, or for this machine only in ``fxtr.local.toml`` (see
:mod:`fxtr.project.config`). ``fxtr remote`` adds, renames, and removes them, and
chooses the default (:mod:`.files`). A push or a pull uses a configured remote and nothing
else: with none configured, it is refused. Each remote has a *kind*. The kind tells how to
reach the remote. A kind is an implementation of :class:`Remote` and a list of the settings
that its table can hold. :func:`register_kind` adds a kind by name. Thus, later code can add
kinds that sync in different ways. :func:`remote_for` builds the remote that a push or a pull
names, or the default remote.

Push and pull are not methods of the project client. They are functions that use the
:class:`~fxtr.project.client.ProjectClient` interface (:func:`push_job`, :func:`pull_job`). A
remote uses the methods of the client to do its work. Only the local client touches the database.

A push or a pull moves one job, and runs the same way on every kind (:meth:`Remote.push`,
:meth:`Remote.pull`). A push exports the job's record file, pushes the commits that the record
names to the remote's git remote under fxtr's pin refs (refused if this checkout lacks one), and
then sends the file the kind's way. A pull downloads the file the kind's way, reads it under an
import's limits (:data:`~fxtr.project.job_record.IMPORT_LIMITS`), fetches the commits that this
repository lacks, and imports the job, so it refuses what an import refuses. With ``update``, a
pull can bring a job that the project has up to date; with ``replace``, it replaces the project's
copy of the job (see :meth:`~fxtr.project.client.ProjectClient.import_job`).

A push sets the tags that you give to the job. What a tag is, whether a push with no tags sets a
default tag, and whether a tag that is already on the remote moves to this job, is the kind's rule
(the ``oci`` kind sets a default tag and moves tags as ``docker push`` does; the ``hub`` kind sets
no default tag and moves a tag only from where the push saw it). A push also returns what the
printed pull command names to get the job back (:attr:`PushedJob.ref`: the job's id on a hub,
tags or not; the first tag on an OCI registry) and what the remote calls that push
(:attr:`PushedJob.version`: the hub's record cid of the job after the sync; the artifact's
digest on a registry, which names that push for ever, also after its tags move). A pull names a
tag, a job id, or a digest after ``@``; what else a pull names is the kind's rule (the ``hub``
kind also takes ``tag:<name>`` and ``owner/repo/<job id>``, see :mod:`.hub`).

The built-in kinds (:mod:`.hub`, :mod:`.oci`) load when :func:`remote_for` first builds a remote,
so importing this package, as the command line does to parse its arguments, loads no transport.
No module here imports an optional dependency at module level: a kind that needs an extra imports
it when it builds a remote.
"""

from __future__ import annotations

import abc
import dataclasses
import logging
import shlex
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from fxtr.project.client import JobID, ProjectClient, ProjectClientError
from fxtr.project.config import Project, ProjectError, RemoteConfig, Setting, read_settings
from fxtr.project.environment import Environment
from fxtr.project.job_record import IMPORT_LIMITS, RecordFile, RecordFileError, read_record

logger = logging.getLogger(__name__)

JOB_VISIBILITIES = ("shared", "private", "public")
"""Who may read a hub job: project readers, its owner, or anyone."""


class RemoteError(ProjectClientError):
    """A remote cannot do what you asked. Possible causes: the remote is not configured; its kind
    is unknown; its kind needs an extra that is not installed; a reference names nothing on the
    remote; or the connection to the remote failed."""


@dataclasses.dataclass(frozen=True)
class PushedJob:
    """The location of a job that a push sent to a remote.

    Attributes:
        remote: The name of the remote.
        job_id: The job.
        tags: The tags that the push set to the job, in the order given.
        ref: What the printed pull command names to get the job back
            (:func:`pull_command`): the job's id on a hub, whether or not the push set tags,
            since what you push is what you pull and a pull resolves the id to the job's
            latest push; on an OCI registry the first tag the push set (a push there always
            sets one), which moves with later pushes, as ``docker`` tags do.
        version: What the remote calls this push: on a hub, the hub's record cid of the job
            after the sync (the root of the record the hub exports of the job, which a pull
            checks); on an OCI registry, the artifact's digest (a pull takes it after ``@``),
            which names this push for ever, also after the job is pushed again.
        unchanged: True if the remote already held this record, with everything that goes
            beside it, so the push sent nothing new. The push set its tags either way.
        visibility: Who may read the job on a hub, as its answer reports it; ``None`` for a
            remote or answer that does not report visibility.
        url: The job's page on a hub, to open in a browser or send to someone; ``None`` for a
            remote with no web pages.
    """

    remote: str
    job_id: JobID
    tags: tuple[str, ...]
    ref: str
    version: str
    unchanged: bool = False
    visibility: str | None = None
    url: str | None = None


@dataclasses.dataclass(frozen=True)
class Download:
    """What a kind downloaded for a pull (see :meth:`Remote.download`).

    Attributes:
        what: What the remote served, for messages: ``job <id>`` on a hub, ``'<tag>' in
            <repository>`` on a registry.
        record_cid: The id of the record that the remote says the file holds, or ``None`` if the
            remote does not say. A kind that sets it sets ``job_id`` too.
        job_id: The job that the remote says the record is of, or ``None`` if the remote does
            not say.
        page: The address where a person can read the job without pulling it, or ``None`` if the
            remote has no such page.
    """

    what: str
    record_cid: str | None = None
    job_id: str | None = None
    page: str | None = None


class Remote(abc.ABC):
    """A configured remote. Its kind builds it (see the module docstring).

    :meth:`push` and :meth:`pull` run the same way on every kind. A kind implements the steps
    that differ: :meth:`upload` and :meth:`download`, :meth:`check_push` when it has a rule on
    what a push asks for, and :meth:`set_visibility` when it has job visibility.

    Args:
        config: The configuration of the remote.
        project: The project. Its repository has the commits that a pushed record names, and
            receives the commits that a pulled record names.
        environment: The environment, which runs git.
        git_remote: The git remote that holds the commits.
    """

    def __init__(
        self, config: RemoteConfig, *, project: Project, environment: Environment, git_remote: str
    ) -> None:
        self.config = config
        self.project = project
        self.environment = environment
        self.git_remote = git_remote

    @property
    def name(self) -> str:
        """The name of the remote in the project's configuration."""
        return self.config.name

    async def push(
        self,
        client: ProjectClient,
        job_id: JobID,
        *,
        tags: Sequence[str] = (),
        visibility: str | None = None,
    ) -> PushedJob:
        """Send a settled job to the remote, and set tags to it.

        Args:
            client: The client of the project that has the job.
            job_id: The job to send.
            tags: The tags to set. Whether an empty ``tags`` sets a default tag, and whether a
                tag that is already on the remote moves to this job, is the kind's rule (see
                the module docstring).
            visibility: Who may read the job on the remote, for a kind that has job
                visibility (``shared``, ``private``, or ``public`` on a hub). ``None`` leaves
                the remote's default for a new job, and as it is for a job the remote holds.

        Raises:
            JobRunningError: If a process holds the job.
            RemoteError: If a tag is not valid, the remote is not available, the record names a
                commit this checkout lacks, or ``visibility`` is given to a kind without job
                visibility.
        """
        await self.check_push(tags, visibility)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "job.car"
            logger.info("exporting job %s", job_id)
            await client.export_job(job_id, path)
            file = read_record(path)
            await self._push_commits(file)
            return await self.upload(client, job_id, file, path, tags=tags, visibility=visibility)

    async def pull(
        self, client: ProjectClient, ref: str, *, update: bool = False, replace: bool = False
    ) -> JobID:
        """Get a job from the remote and import it into the project. Return the id of the job.

        Args:
            client: The client of the project that receives the job.
            ref: A tag on the remote, the reference a push returned (:attr:`PushedJob.ref`),
                or another reference the kind accepts (see the module docstring).
            update: If true, and the project has the job, bring the job up to date (see
                :meth:`~fxtr.project.client.ProjectClient.import_job`).
            replace: If true, and the project has the job, replace its copy with the remote's
                (see :meth:`~fxtr.project.client.ProjectClient.import_job`).

        Raises:
            RemoteError: If ``ref`` names nothing on the remote, the remote is not available,
                what it sent is not a job record this fxtr reads, or the git remote does not give
                the commits the job ran at.
            ImportRefusedError: If the import refuses the job.
        """
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "job.car"
            download = await self.download(ref, path)
            try:
                file = read_record(path, limits=IMPORT_LIMITS)
            except RecordFileError as exc:
                raise RemoteError(
                    f"{self.name} sent {download.what}, but it is not a job record this fxtr"
                    f" reads ({exc})"
                ) from exc
            found = (file.root.cid_str(), file.record.job.id)
            if download.record_cid is not None and found != (download.record_cid, download.job_id):
                raise RemoteError(
                    f"{self.name} sent a record that is not {download.what}: record {found[0]} of"
                    f" job {found[1]}, where {download.what} is record {download.record_cid} of"
                    f" job {download.job_id}"
                )
            missing = await self.environment.missing_commits(self.project, file.commits())
            if missing:
                try:
                    await self.environment.fetch_commits(self.project, self.git_remote, missing)
                except ProjectError as exc:
                    elsewhere = (
                        "" if download.page is None else f" You can read it at {download.page}."
                    )
                    raise RemoteError(
                        f"Could not pull {download.what}: it ran at commits this checkout lacks,"
                        f" and git could not fetch them from {self.git_remote!r}. A pull needs read"
                        " access to the job's git repository, so nothing was imported."
                        f"{elsewhere} Git said: {exc}"
                    ) from exc
            return await client.import_job(path, update=update, replace=replace)

    async def check_push(self, tags: Sequence[str], visibility: str | None) -> None:
        """Refuse what this remote cannot do for a push, before the job is exported. The
        default refuses a visibility, as a kind without job visibility must.

        Args:
            tags: The tags the push sets.
            visibility: The visibility the push asks for, or ``None``.

        Raises:
            RemoteError: If the remote cannot do what the push asks.
        """
        if visibility is not None:
            raise self._no_visibility()

    @abc.abstractmethod
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
        """Send an exported record file to the remote, set the tags, and say what the remote
        calls the push. The commits that the record names are on the git remote already.

        Args:
            client: The client of the project that has the job.
            job_id: The job.
            file: The record, as read from ``path``.
            path: The record file. It is deleted after this returns.
            tags: The tags to set, as given to :meth:`push`.
            visibility: Who may read the job, as given to :meth:`push`.

        Raises:
            RemoteError: If the remote refuses the push or is not available.
        """

    @abc.abstractmethod
    async def download(self, ref: str, path: Path) -> Download:
        """Write the record file that ``ref`` names to ``path``, and say what was served.

        Args:
            ref: A tag, or the reference the remote gave a push, as given to :meth:`pull`.
            path: Where to write the file.

        Raises:
            RemoteError: If ``ref`` names nothing on the remote, what it names is not a pushed
                job, or the remote is not available.
        """

    async def job_id(self, ref: str) -> JobID:
        """The job a command names to push or change: a job id. A kind may take other forms
        (a hub also takes ``owner/repo/<job id>``).

        Raises:
            RemoteError: If ``ref`` is not a job id.
        """
        try:
            return JobID(uuid.UUID(ref))
        except ValueError:
            raise RemoteError(f"not a job id: {ref!r}") from None

    async def set_visibility(self, job_id: JobID, visibility: str) -> None:
        """Change who may read a job on the remote. The default refuses: only a kind with job
        visibility (a hub) overrides it.

        Args:
            job_id: The job.
            visibility: ``shared``, ``private``, or ``public`` on a hub.

        Raises:
            RemoteError: If the kind has no job visibility, or the remote refuses the change.
        """
        raise self._no_visibility()

    def _no_visibility(self) -> RemoteError:
        return RemoteError(
            f"{self.name} (kind {self.config.kind!r}) has no job visibility: a job visibility"
            " applies only to a hub remote"
        )

    async def _push_commits(self, file: RecordFile) -> None:
        """Push the commits a record names to the git remote, under fxtr's pin refs
        (:meth:`~fxtr.project.environment.Environment.push_commits`).

        Raises:
            RemoteError: If the project's repository lacks a commit the record names. The job ran
                from another checkout that shares this project's database, or the commit was
                pruned; git would refuse the push with its own message.
        """
        commits = file.commits()
        missing = await self.environment.missing_commits(self.project, commits)
        if missing:
            raise RemoteError(
                f"Job {file.record.job.id} cannot be pushed to {self.name}: its record names"
                f" commits this checkout does not have ({', '.join(missing)}); fetch them first"
            )
        if commits:
            logger.info("sending its commits to %s", self.git_remote)
            await self.environment.push_commits(self.project, self.git_remote, commits)


type RemoteBuilder = Callable[[RemoteConfig, Mapping[str, Any], Project, Environment], Remote]
"""The function that builds a remote of a kind. Its arguments are: the configuration of the
remote; its settings, read against the settings that the kind declares; the project; and the
environment (a kind can use it to run git)."""


@dataclasses.dataclass(frozen=True)
class RemoteKind:
    """A kind of remote.

    Attributes:
        settings: The settings that a remote's table of this kind can hold, other than
            ``kind``.
        build: The function that builds a remote of this kind.
    """

    settings: tuple[Setting, ...]
    build: RemoteBuilder


_KINDS: dict[str, RemoteKind] = {}


def register_kind(kind: str, settings: tuple[Setting, ...], build: RemoteBuilder) -> None:
    """Add a kind of remote.

    Args:
        kind: The name of the kind, as a ``kind`` setting writes it. If a kind has this name
            already, the new kind replaces it.
        settings: The settings that a table of this kind can hold, other than ``kind``.
        build: The function that builds a remote of this kind.
    """
    _KINDS[kind] = RemoteKind(settings, build)


def remote_for(project: Project, environment: Environment, name: str | None = None) -> Remote:
    """Build a remote of the project.

    Args:
        project: The project. Its configuration files configure the remotes.
        environment: The environment, which the kind can use to run git.
        name: The name of the remote. If it is ``None``, use the project's default remote, or
            its only remote when it sets no default.

    Raises:
        RemoteError: If the project does not configure the remote; if ``name`` is ``None`` and
            the project configures no remote, or several and no default; or if the kind of the
            remote is unknown.
        ConfigError: If the settings of the remote are not correct for its kind.
    """
    remotes = project.remotes.by_name
    if name is None:
        name = implied_remote(project)
        if name is None and not remotes:
            raise RemoteError(
                "This project has no remote to push to or pull from: add one with `fxtr remote add`"
            )
        if name is None:
            raise RemoteError(
                f"Name a remote (--remote {'|'.join(sorted(remotes))}), or choose the default"
                " with `fxtr remote set-default`"
            )
    config = remotes.get(name)
    if config is None:
        configured = ", ".join(sorted(remotes)) or "none"
        raise RemoteError(f"This project has no remote named {name!r} (it has: {configured})")
    _load_builtin_kinds()
    kind = _KINDS.get(config.kind)
    if kind is None:
        known = ", ".join(sorted(_KINDS)) or "none"
        raise RemoteError(f"{config.where} has kind {config.kind!r}; known: {known}")
    settings = read_settings(config.settings, kind.settings, config.where)
    return kind.build(config, settings, project, environment)


def implied_remote(project: Project) -> str | None:
    """The remote a push or a pull uses when it names none: the project's default remote, or
    its only remote when it sets no default, or ``None``.

    A ``default_remote`` that names no remote is refused here, not when the project loads. It
    comes from checking out an older branch or a teammate removing a remote, and every command,
    including the ``fxtr remote`` commands that fix it, must still run.

    Raises:
        RemoteError: If ``default_remote`` names no configured remote.
    """
    remotes = project.remotes.by_name
    default = project.remotes.default
    if default is not None:
        if default not in remotes:
            raise RemoteError(
                f"default_remote {default!r} names no configured remote: change it with"
                " `fxtr remote set-default`, or name a remote with --remote"
            )
        return default
    return next(iter(remotes)) if len(remotes) == 1 else None


def _load_builtin_kinds() -> None:
    """Load the built-in kinds, which register themselves when their modules load."""
    from fxtr.project.remotes import hub, oci  # noqa: F401  # pyright: ignore[reportUnusedImport]


async def push_job(
    client: ProjectClient,
    job_id: JobID,
    *,
    remote: str | None = None,
    tags: Sequence[str] = (),
    visibility: str | None = None,
) -> PushedJob:
    """Push a job to a remote of the client's project (see :meth:`Remote.push`).

    Args:
        client: The client of the project. Its ``project`` configures the remotes, and its
            ``environment`` runs git for them.
        job_id: The job to push.
        remote: The name of the remote. If it is ``None``, :func:`remote_for` selects the remote.
        tags: The tags to set (see :meth:`Remote.push`).
        visibility: Who may read the job on the remote (see :meth:`Remote.push`).
    """
    built = remote_for(client.project, client.environment, remote)
    return await built.push(client, job_id, tags=tags, visibility=visibility)


async def pull_job(
    client: ProjectClient,
    ref: str,
    *,
    remote: str | None = None,
    update: bool = False,
    replace: bool = False,
) -> JobID:
    """Pull a job from a remote of the client's project (see :meth:`Remote.pull`).

    Args:
        client: The client of the project. Its ``project`` configures the remotes, and its
            ``environment`` runs git for them.
        ref: A tag, the reference a push returned, or another reference the kind accepts (see
            :meth:`Remote.pull`).
        remote: The name of the remote. If it is ``None``, :func:`remote_for` selects the remote.
        update: If true, and the project has the job, bring the job up to date.
        replace: If true, and the project has the job, replace its copy with the remote's.
    """
    built = remote_for(client.project, client.environment, remote)
    return await built.pull(client, ref, update=update, replace=replace)


def pull_command(project: Project, pushed: PushedJob) -> str:
    """Return the command that pulls the job of a push, by the reference the push returned
    (:attr:`PushedJob.ref`). The command names the remote only if a pull does not select that
    remote without its name."""
    implied = implied_remote(project)
    ref = pushed.ref
    args = ["fxtr", "pull"]
    if not ref.startswith("-"):
        args.append(ref)
    if pushed.remote != implied:
        if pushed.remote.startswith("-"):
            args.append(f"--remote={pushed.remote}")
        else:
            args.extend(("--remote", pushed.remote))
    if ref.startswith("-"):
        args.extend(("--", ref))
    return shlex.join(args)


__all__ = [
    "JOB_VISIBILITIES",
    "Download",
    "PushedJob",
    "Remote",
    "RemoteBuilder",
    "RemoteError",
    "RemoteKind",
    "implied_remote",
    "pull_command",
    "pull_job",
    "push_job",
    "register_kind",
    "remote_for",
]
