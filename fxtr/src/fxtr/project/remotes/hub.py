"""The ``hub`` kind of remote: jobs synced to a fxtr hub, commits in a git remote.

A fxtr hub keeps the jobs of a project for a team, each job once. A push sends the job's record
file (:mod:`fxtr.project.job_record`), and the hub merges its facts into what it holds of the
job. The hub names what it holds by a *record cid*: the root cid of the record it would export
of the job (``fxtr jobs export`` writes the same root for the same facts). Pushing a job the hub
has already is accepted only when the new record holds everything the hub knows of the job, as
``import_job(update=True)`` requires; if someone else pushed the job since this push read the
hub's record cid, the hub refuses it (``record_conflict``), and nothing is merged. A job resumed
on both sides cannot merge: its copies each hold attempts the other lacks, so a pull with
``replace`` takes the hub's copy. *Tags* are
names in the project that point at jobs; a push sets the ones its tags name.

A push (:meth:`~fxtr.project.remotes.Remote.push` exports the job and pushes the
commits its record names to the git remote first):

1. checks that this computer is signed in to the hub, and that its account may push to the
   remote's project;
2. asks the hub what it holds of the job (:meth:`HubClient.job_state`): its record cid, which
   the publish then expects, and what was attached beside the record. A job no process has
   taken yet has no commit it ran at, and nothing a hub would show: it is refused. If the hub
   holds the record this push would send, with the same attachments, nothing is sent;
3. uploads the record file, asks the hub to publish it, and waits until the hub has merged it
   (the upload is *synced*) or refused it. The upload's key is derived from the file, the
   expected record cid, and the attachments, so a push repeated after a lost answer resumes
   the upload it made, sending only the bytes the hub lacks; an upload an earlier push left
   failed or expired is passed over for a fresh one;
4. sets each tag it was given, each only if it still points where the push saw it point. The
   synced job stands either way, and a tag not set is reported with it.

A remote names its hub project by id, so which project a push reaches does not depend on the
checkout's directory. ``fxtr remote add --create`` makes a project
(:meth:`HttpHubClient.create_project`) for the checkout's GitHub repository.

A pull resolves its reference to a job (:meth:`HubRemote._resolve`): a job id (a UUID, or
``owner/repo/<uuid>``, which :meth:`HubRemote.job_id` checks against the checkout's GitHub
repository) names the job; anything else names a tag, and ``tag:<name>`` a tag whose name
looks like a job reference. A push returns the job's id as its
:attr:`~fxtr.project.remotes.PushedJob.ref`, so the command that a push prints
pulls the job as the hub holds it, and the hub's record cid after the sync as its
:attr:`~fxtr.project.remotes.PushedJob.version`. The kind
downloads the job's archive, which the hub exports from what it holds, and reads which record
the hub says it is (the ``X-Fxtr-Record-Cid`` header of the same answer, so no second request
can race another sync); :meth:`~fxtr.project.remotes.Remote.pull` then checks
that the file is that record of that job, fetches the commits the repository lacks, and imports
the job.

The kind reaches the hub through a :class:`HubClient`. :class:`HttpHubClient` speaks the hub's
HTTP API (version 1) with the standard library; tests use a fake. It authenticates with a bearer
token: the ``FXTR_HUB_TOKEN`` environment variable, or else the token that ``fxtr hub
login`` saved for the hub's URL in the user's configuration directory (:func:`credentials_path`),
never in ``fxtr.local.toml``. It follows no redirect, so the token goes only to the configured URL,
and that URL must pass :func:`check_hub_url` (HTTPS, or HTTP to this computer) wherever a token
is attached to a request or saved for a URL.
"""

from __future__ import annotations

import abc
import base64
import dataclasses
import functools
import hashlib
import http.client
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

import anyio
import anyio.to_thread

from fxtr.project.client import JobID, ProjectClient
from fxtr.project.config import REQUIRED, Project, RemoteConfig, Setting
from fxtr.project.environment import Environment
from fxtr.project.job_record import MAX_RECORD_FILE_BYTES, RecordFile
from fxtr.project.remotes import (
    JOB_VISIBILITIES,
    Download,
    PushedJob,
    Remote,
    RemoteError,
    register_kind,
)
from fxtr.project.views import (
    VIEW_BUNDLE_MANIFEST_FILENAME,
    ViewBundle,
    ViewsError,
    bundle_dir,
    read_bundle,
)

TOKEN_ENV_VAR = "FXTR_HUB_TOKEN"
"""The environment variable that, when set, holds the token for every hub."""

logger = logging.getLogger(__name__)

PUBLISH_TIMEOUT = 40 * 60.0
"""How many seconds a push waits for the hub to sync an upload: longer than a hub lets one
import take (30 minutes), with room for the upload's turn in its queue."""

WAIT_REPORT_SECONDS = 60.0
"""How often a push that waits for the hub says that it is still waiting. The hub reports a
state and no percentage, so this is how a person can tell a long merge from a hung command."""

DOWNLOAD_TIMEOUT = 20 * 60.0
"""How many seconds a pull waits for the hub's answer to a download: longer than a hub lets one
export take (15 minutes), since the answer's first byte comes after the export."""

RECORD_CID_HEADER = "X-Fxtr-Record-Cid"
"""The header of a job's archive that says which record the archive holds: the hub's record cid
of the job as the archive was exported."""

SEND_CHUNK_BYTES = 4 << 20
"""The most bytes of an upload one request sends; the hub refuses a request of over 8 MiB."""

MAX_PASSED_OVER_UPLOADS = 64
"""How many failed or expired uploads a push passes over along its chain of keys before it takes
a fresh key, and, after as many again, gives the hub up. Each one costs a request, and a hub that
answers every key with a fresh failed upload would otherwise be asked for ever."""

MAX_ANSWER_BYTES = 1 << 20
"""The most bytes read of a JSON answer, or of an error's body. A hub's answers are small (a
job's state, an upload, an id), so a longer one is not a hub's, and is not held in memory."""

_CHUNK = 1 << 20


async def _in_thread[T](call: Callable[[], T]) -> T:
    """Run a blocking request in a worker thread. A request can wait minutes for the hub's first
    byte, so Ctrl-C leaves the thread behind, and the command exits without it (``cli.entry``)."""
    return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)


class HubRequestError(RemoteError):
    """The hub refused a request.

    Attributes:
        status: The HTTP status.
        code: The hub's error code (``record_conflict``, ``upload_expired``, ...), or
            ``redirect`` when the hub redirected the request, which the client does not follow.
    """

    def __init__(
        self, status: int, code: str, message: str, details: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details: Mapping[str, object] = details or {}


@dataclasses.dataclass(frozen=True)
class JobState:
    """What a hub holds of a job, as the hub describes it.

    Attributes:
        job_id: The job.
        record_cid: The hub's record cid of the job: the root of the record it would export now.
        attachments: What the last push said was sent beside the record
            (``_Attachments.identity``), or ``None`` when nothing was.
        visibility: Who may read the job, or ``None`` when the hub does not report it.
    """

    job_id: str
    record_cid: str
    attachments: str | None
    visibility: str | None = None


@dataclasses.dataclass(frozen=True)
class Upload:
    """An upload, as the hub describes it.

    Attributes:
        id: The upload's id.
        state: ``receiving`` (it takes bytes), ``synced`` or ``failed`` (the hub is done with
            it), or another state the hub names while it validates and merges it.
        offset: How many bytes of the file the hub has.
        record_cid: The hub's record cid of the job as the sync committed it, once ``synced``.
        error: Why it failed, as the hub's error code and message, once ``failed``.
        visibility: The synced job's visibility reported by the hub, or ``None`` when absent.
    """

    id: str
    state: str
    offset: int
    record_cid: str | None = None
    error: tuple[str, str] | None = None
    visibility: str | None = None


class HubClient(abc.ABC):
    """The operations on a hub that this kind uses, for one project of the hub.

    Attributes:
        project: The hub project's id.
    """

    project: str

    @abc.abstractmethod
    async def check_push(self) -> None:
        """Check credentials and project contribution access before exporting or pushing pins."""

    @abc.abstractmethod
    async def job_state(self, job_id: JobID) -> JobState | None:
        """What the project holds of the job, or ``None`` if it holds nothing of it."""

    @abc.abstractmethod
    async def create_upload(self, record_cid: str, size: int, idempotency_key: str) -> Upload:
        """Start an upload of a record file of ``size`` bytes. A second call with the same key
        returns the same upload."""

    @abc.abstractmethod
    async def upload(self, upload_id: str) -> Upload:
        """The upload's current state."""

    @abc.abstractmethod
    async def send(self, upload_id: str, path: Path, offset: int) -> Upload:
        """Send the file from ``offset`` to its end."""

    @abc.abstractmethod
    async def publish(
        self, upload_id: str, expected_record_cid: str | None, visibility: str | None = None
    ) -> Upload:
        """Ask the hub to merge a complete upload into the job, if the hub's record cid of the
        job is still ``expected_record_cid`` (``None``: the project holds nothing of the job).
        If the project holds nothing of the job, the job gets ``visibility`` (``None``:
        ``shared``)."""

    @abc.abstractmethod
    async def set_job_visibility(self, job_id: JobID, visibility: str) -> None:
        """Change who may read a job the hub holds: ``shared``, ``private``, or ``public``."""

    @abc.abstractmethod
    async def create_bundle(self, bundle: ViewBundle, commit: str | None = None) -> str:
        """Upload a built view bundle and return its id.

        Args:
            commit: The recorded build commit, or ``None`` when it is unknown.
        """

    @abc.abstractmethod
    async def set_attachments(self, upload_id: str, bundle_id: str | None, identity: str) -> None:
        """Set what goes beside a receiving upload's record: a bundle uploaded with
        :meth:`create_bundle`. The hub keeps ``identity`` on the job, so a later push can tell
        whether it would send the same."""

    @abc.abstractmethod
    async def tag(self, name: str) -> JobID | None:
        """The job the tag points at, or ``None`` if there is no tag."""

    @abc.abstractmethod
    async def set_tag(self, name: str, job_id: JobID, expected: JobID | None) -> bool:
        """Point the tag at a job if it still points at ``expected`` (``None``: there is no
        tag). Return whether it was set."""

    @abc.abstractmethod
    async def download(self, job_id: JobID, path: Path) -> str | None:
        """Write the job's archive, the record file the hub exports of what it holds, to
        ``path``, and return the hub's record cid of it (which record the file holds), or
        ``None`` if the project holds nothing of the job."""


class HubRemote(Remote):
    """A remote of the ``hub`` kind (see the module docstring).

    Args:
        config: The configuration of the remote.
        hub: The client for the hub project.
        git_remote: The git remote that holds the commits.
        project: The project.
        environment: The environment, which runs git.
        publish_timeout: How many seconds a push waits for the hub to sync its upload. A test
            against a running hub shortens it, so a hub that never syncs fails the test soon.
    """

    def __init__(
        self,
        config: RemoteConfig,
        *,
        hub: HubClient,
        git_remote: str,
        project: Project,
        environment: Environment,
        publish_timeout: float = PUBLISH_TIMEOUT,
    ) -> None:
        super().__init__(config, project=project, environment=environment, git_remote=git_remote)
        self.hub = hub
        self.publish_timeout = publish_timeout

    async def check_push(self, tags: Sequence[str], visibility: str | None) -> None:
        for name in tags:
            check_tag(name)
        await self.hub.check_push()

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
        commit = _job_commit(file)
        if commit is None:
            raise RemoteError(
                f"Job {job_id} has never run, so there is nothing to publish to {self.name}:"
                " run it first"
            )
        record_cid = file.root.cid_str()
        attachments = _Attachments(bundle=self._bundle())
        held = await self.hub.job_state(job_id)
        expected = None if held is None else held.record_cid
        if (
            held is not None
            and held.record_cid == record_cid
            and attachments.identity in (None, held.attachments)
        ):
            # The hub holds this record, with these attachments: nothing to send. The record
            # cid alone does not say so: other attachments go with the same record. A checkout
            # with no built bundle (``views/dist`` is not committed) has no say on them.
            version, unchanged = held.record_cid, True
            visible_to = held.visibility
        else:
            synced = await self._sync(job_id, path, record_cid, expected, attachments, visibility)
            assert synced.record_cid is not None
            version, unchanged = synced.record_cid, False
            visible_to = synced.visibility
        # The job is synced now, so a failure to set a tag or the visibility names it.
        problems: list[str] = []
        if visibility is not None and held is not None:
            # A job the hub holds keeps its visibility through a sync; the visibility asked
            # for is set on it, if the pusher may.
            try:
                await self.hub.set_job_visibility(job_id, visibility)
                visible_to = visibility
            except HubRequestError as exc:
                problems.append(f"its visibility was not set to {visibility} ({exc})")
        not_set: list[str] = []
        for tag in tags:
            try:
                if not await self._set_tag(tag, job_id):
                    not_set.append(f"{tag} (it moved while the job was pushed)")
            except RemoteError as exc:
                not_set.append(f"{tag} ({exc})")
        if not_set:
            problems.append(f"these tags were not set: {'; '.join(not_set)}")
        if problems:
            raise RemoteError(
                f"Job {job_id} was pushed to {self.name} ({version}), but {', and '.join(problems)}"
            )
        # A pull by the job's id gets the job as the hub holds it, which this push is now, so
        # the job's id is the reference; the hub's record cid says what that is.
        return PushedJob(
            self.name,
            job_id,
            tuple(tags),
            str(job_id),
            version,
            unchanged=unchanged,
            visibility=visible_to,
            url=self._job_page(job_id),
        )

    def _job_page(self, job_id: JobID) -> str:
        """The address of the job's page on the hub."""
        settings = self.config.settings
        return f"{_normal_url(str(settings['url']))}/projects/{settings['project']}/jobs/{job_id}"

    async def set_visibility(self, job_id: JobID, visibility: str) -> None:
        """Change who may read a job this hub holds: ``shared`` with the repository's readers,
        ``private`` to its owner, or ``public`` (see the hub's rules on who may change it).

        Raises:
            RemoteError: If the hub refuses the change.
        """
        await self.hub.set_job_visibility(job_id, visibility)

    def _bundle(self) -> ViewBundle | None:
        """The project's built view bundle as it is now, or ``None`` when the project has none
        built (the hub then shows the default renderers)."""
        if self.project.views is None:
            return None
        directory = bundle_dir(self.project.views)
        if not directory.exists():
            return None
        try:
            return read_bundle(directory)
        except ViewsError as exc:
            logger.warning("this push sends no view bundle: %s", exc)
            return None

    async def _sync(
        self,
        job_id: JobID,
        path: Path,
        record_cid: str,
        expected_record_cid: str | None,
        attachments: _Attachments,
        visibility: str | None = None,
    ) -> Upload:
        """Upload and merge the record, expecting ``expected_record_cid``. Return the synced
        upload receipt, including the record CID committed by the sync.

        Raises:
            RemoteError: If the hub refuses the upload, or does not sync it in time.
        """
        size = path.stat().st_size
        # The same record file published over the same expected record is the same request: a
        # retry of a push that lost its answer finds the upload it made, and what the hub holds
        # since. The key names the file's bytes, not only its record: another fxtr can compress
        # the same record to other bytes, and an upload holds one file's bytes. An upload an
        # earlier push left failed or expired is done with; the next try takes a key derived
        # from it, so retries stay deterministic and each gets a fresh upload, at most
        # MAX_PASSED_OVER_UPLOADS of them.
        digest = _file_digest(path)
        identity = attachments.identity

        def key_after(upload: str | None) -> str:
            return _upload_key(
                digest, expected_record_cid, identity, after=upload, visibility=visibility
            )

        key = key_after(None)
        passed_over: set[str] = set()
        while True:
            if len(passed_over) >= 2 * MAX_PASSED_OVER_UPLOADS:
                raise RemoteError(
                    f"{self.name} answered {len(passed_over)} upload keys in a row with uploads"
                    " that had failed or expired; it is not taking this push"
                )
            if len(passed_over) == MAX_PASSED_OVER_UPLOADS:
                # A job this far along its chain of keys starts one of its own, or the failures
                # of earlier pushes would refuse it for good, even once their cause is fixed.
                key = key_after(str(uuid.uuid4()))
            upload = await self.hub.create_upload(record_cid, size, key)
            if upload.id in passed_over:
                raise RemoteError(
                    f"{self.name} answered a new upload key with upload {upload.id}, which an"
                    " earlier key named"
                )
            if upload.state == "failed":
                passed_over.add(upload.id)
                key = key_after(upload.id)
                continue
            try:
                upload = await self._finish(
                    upload, path, size, expected_record_cid, attachments, visibility
                )
            except HubRequestError as exc:
                if exc.code == "record_conflict":
                    raise self._record_conflict(job_id, str(exc)) from exc
                if exc.code != "upload_expired":
                    raise
                passed_over.add(upload.id)
                key = key_after(upload.id)
                continue
            if upload.state == "failed":
                code, message = upload.error or ("failed", "the hub gave no reason")
                if code == "record_conflict":
                    raise self._record_conflict(job_id, message)
                raise RemoteError(f"{self.name} refused the job ({code}): {message}")
            if upload.state != "synced":
                raise RemoteError(
                    f"{self.name} has not synced upload {upload.id} after"
                    f" {self.publish_timeout:g} seconds; it is {upload.state}. The hub keeps"
                    " working on it: push again to wait for it, without sending the file again"
                )
            if upload.record_cid is None:
                raise RemoteError(
                    f"{self.name} synced upload {upload.id} without saying which record the job"
                    " is now"
                )
            return upload

    def _record_conflict(self, job_id: JobID, detail: str) -> RemoteError:
        """Explain why the hub refused to merge this job record."""
        return RemoteError(
            f"{self.name} refused job {job_id} (record_conflict): the hub's copy has changes this"
            f" one lacks or contradicts ({detail}). If someone else pushed it, pull it first with"
            f" `fxtr pull {job_id} --update`, then push again. If that pull is refused too, the job"
            " was also resumed here, and the two copies cannot merge. To publish this copy's"
            " results, run the workflow again with the same inputs and root, as a new job that"
            " reuses them from the step cache, and push that job. To take the hub's copy, run"
            f" `fxtr pull {job_id} --replace`, which removes this project's job attempts that the"
            " hub lacks."
        )

    async def _finish(
        self,
        upload: Upload,
        path: Path,
        size: int,
        expected_record_cid: str | None,
        attachments: _Attachments,
        visibility: str | None,
    ) -> Upload:
        """Send what the hub lacks of a receiving upload and what goes beside it, publish it
        (with the visibility its job gets if the hub holds nothing of the job), and wait until
        it is synced or failed, or the timeout passes."""
        if upload.state == "receiving":
            if upload.offset < size:
                logger.info("uploading it to %s: %s", self.name, _size(size))
                upload = await self.hub.send(upload.id, path, upload.offset)
            if attachments.identity is not None:
                bundle_id = None
                if attachments.bundle is not None:
                    logger.info("uploading the project's view bundle")
                    bundle_id = await self.hub.create_bundle(attachments.bundle)
                await self.hub.set_attachments(upload.id, bundle_id, attachments.identity)
            upload = await self.hub.publish(upload.id, expected_record_cid, visibility)
        # The hub syncs a small upload within a second or so of the request; asking again at
        # these intervals finds that out soon after, and a long wait costs one small request a
        # second.
        with anyio.move_on_after(self.publish_timeout):
            delay = 0.2
            started = reported = anyio.current_time()
            state = None
            while upload.state not in ("synced", "failed"):
                now = anyio.current_time()
                if upload.state != state:
                    state, reported = upload.state, now
                    logger.info("waiting for %s: %s", self.name, state)
                elif now - reported >= WAIT_REPORT_SECONDS:
                    reported = now
                    logger.info("waiting for %s: still %s, %.0f s", self.name, state, now - started)
                await anyio.sleep(delay)
                delay = min(delay * 2, 1.0)
                upload = await self.hub.upload(upload.id)
        return upload

    async def _set_tag(self, name: str, job_id: JobID) -> bool:
        current = await self.hub.tag(name)
        if current == job_id:
            return True
        return await self.hub.set_tag(name, job_id, current)

    async def download(self, ref: str, path: Path) -> Download:
        job_id = await self._resolve(ref)
        record_cid = await self.hub.download(job_id, path)
        if record_cid is None:
            raise self._nothing_named(ref)
        return Download(
            f"job {job_id}",
            record_cid=record_cid,
            job_id=str(job_id),
            page=self._job_page(job_id),
        )

    async def job_id(self, ref: str) -> JobID:
        """Parse a job id: a UUID, or ``owner/repo/<uuid>`` or ``repo/<uuid>``.

        A repository before the UUID is checked against the checkout's GitHub repository
        (compared without case). It never selects the hub project: that comes from the remote's
        configuration.

        Raises:
            RemoteError: If ``ref`` is not a UUID after an optional repository, or names another
                repository than the checkout's.
        """
        repository, separator, identifier = ref.rpartition("/")
        if not separator:
            identifier = ref
        if not _is_uuid(identifier):
            raise RemoteError("Expected a job UUID or owner/repo/<job-id>")
        if separator:
            url, _ = await self.environment.repository_binding(self.project.root, self.git_remote)
            expected = github_repository(url)
            if repository.casefold() not in (
                expected.casefold(),
                expected.rsplit("/", 1)[-1].casefold(),
            ):
                raise RemoteError(
                    f"Job reference names repository {repository!r}, but this checkout uses "
                    f"{expected!r}; run the command from the matching project checkout"
                )
        return JobID(uuid.UUID(identifier))

    async def _resolve(self, ref: str) -> JobID:
        """The job a pull's reference names. A job id is parsed, not looked up: whether the
        hub holds the job is found out by the download itself, so no request before it can
        race another sync. A tag is looked up on the hub.

        Raises:
            RemoteError: If ``ref`` is a tag the hub does not have, or a job reference that
                names another repository than the checkout's.
        """
        if ref.startswith("tag:"):
            found = await self.hub.tag(ref.removeprefix("tag:"))
        elif _is_uuid(ref) or ("/" in ref and _is_uuid(ref.rsplit("/", 1)[-1])):
            return await self.job_id(ref)
        else:
            found = await self.hub.tag(ref)
        if found is None:
            raise self._nothing_named(ref)
        return found

    def _nothing_named(self, ref: str) -> RemoteError:
        return RemoteError(
            f"{ref!r} names no readable job or tag on {self.name}: check the reference, and that"
            " this computer is signed in (fxtr hub login) as an account that can"
            " read the project's repository"
        )


def _upload_key(
    digest: str,
    expected_record_cid: str | None,
    attachments: str | None,
    *,
    after: str | None,
    visibility: str | None = None,
) -> str:
    """The idempotency key of an upload of the record file whose SHA-256 is ``digest``. A push
    that asks for a visibility is another request than one that does not, or asks for another."""
    text = f"{digest}\n{expected_record_cid or ''}\n{attachments or ''}\n{after or ''}"
    if visibility is not None:
        text += f"\nvisibility:{visibility}"
    return hashlib.sha256(text.encode()).hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


@dataclasses.dataclass(frozen=True)
class _Attachments:
    """What a push sends beside the record.

    Attributes:
        bundle: The project's built view bundle, or ``None`` when not sent.
    """

    bundle: ViewBundle | None

    @property
    def identity(self) -> str | None:
        """What tells two pushes' attachments apart (the bundle's version), or ``None`` when
        nothing is attached. The hub keeps it on the job and compares it as an opaque string.
        The same record with other attachments is another upload, and a sync."""
        return None if self.bundle is None else self.bundle.version


def _job_commit(file: RecordFile) -> str | None:
    """The commit of the job's latest job attempt, as its code version records it, or ``None``
    for a job no process has taken yet: its record has no job attempt of its own."""
    own = [fact for fact in file.record.job_attempts if fact.job_id == file.record.job.id]
    if not own:
        return None
    latest = max(own, key=lambda fact: (fact.created_at, fact.id))
    return file.commit_of(latest)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# The official service used unless an operator explicitly selects another deployment.
DEFAULT_HUB_URL = "https://hub.transluce.org"


def default_hub_url() -> str:
    """The release hub, with an explicit environment override for other deployments.

    Raises:
        RemoteError: If the URL is not an origin a token may be sent to
            (:func:`check_hub_url`, with no path, query, or fragment).
    """
    url = os.environ.get("FXTR_HUB_URL") or DEFAULT_HUB_URL
    return check_hub_url(url, origin_only=True)


def check_tag(name: str) -> None:
    """Raise ``RemoteError`` if ``name`` cannot name a hub job tag."""
    if not name or len(name) > 128 or any(part in ("", ".", "..") for part in name.split("/")):
        raise RemoteError(
            "Tag must contain nonempty path segments without dot segments"
            " and be at most 128 characters"
        )
    if "\x00" in name:
        raise RemoteError("Tag cannot contain NUL")
    if name.startswith("tag:"):
        raise RemoteError("Tag cannot start with 'tag:', which a pull reads as naming a tag")
    try:
        uuid.UUID(name)
    except ValueError:
        return
    raise RemoteError("Tag cannot be a UUID, which identifies a job")


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def check_hub_url(url: str, *, origin_only: bool = False) -> str:
    """Return ``url`` without a trailing slash, if a token may be sent to it.

    A hub's token is a bearer token: whoever reads the request can use it. So a token goes
    only over HTTPS, except to a hub on this computer (plain HTTP to ``localhost``,
    ``127.0.0.1``, or ``::1``), where a development hub runs. A URL that carries a user name
    or a password is refused too: the token is the credential. Every place that attaches a
    token to a request, and every place that saves a token for a URL, applies this rule.

    Args:
        url: The hub's URL.
        origin_only: If true, refuse a URL with a path, a query, or a fragment, as the release
            hub's address and a browser login's are origins.

    Raises:
        RemoteError: If a token may not be sent to ``url``.
    """
    url = url.rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if not parsed.netloc or parsed.username or parsed.password:
        raise RemoteError(
            f"{url!r} is not a hub URL: it needs a host, and no user name or password (the"
            " token is the credential)"
        )
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS
    ):
        raise RemoteError(
            f"A hub token is sent only over HTTPS: {url} is not an https URL (http is allowed"
            " for localhost, 127.0.0.1, and ::1, where a development hub runs)"
        )
    if origin_only and (parsed.path or parsed.query or parsed.fragment):
        raise RemoteError(f"{url} must be an origin: no path, query, or fragment")
    return url


_SCP_URL = re.compile(r"(?:[^@/]+@)?(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*):(?P<path>[^/].*)")
"""git's short form of an SSH URL: ``[user@]host:path``."""


def _ssh_hostname(host: str) -> str:
    """The machine an SSH host name reaches, by the user's SSH configuration, or the name itself
    if ssh cannot say. A person with two GitHub accounts gives one account's key a name of its own
    there (``Host github.com-work``), and uses that name in git remotes."""
    try:
        answer = subprocess.run(
            ["ssh", "-G", host], capture_output=True, text=True, timeout=5, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return host
    for line in answer.stdout.splitlines():
        setting, _, value = line.partition(" ")
        if setting == "hostname":
            return value
    return host


def github_repository(url: str) -> str:
    """Extract owner/name from a git remote's URL, without the credentials it may carry, which
    must not reach the hub.

    Raises:
        RemoteError: If the URL is not a github.com repository's.
    """
    hostname: str | None = None
    if (scp := _SCP_URL.fullmatch(url)) is not None:
        hostname, path = _ssh_hostname(scp["host"]), scp["path"]
    else:
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.lstrip("/")
        if parsed.scheme == "https":
            hostname = parsed.hostname
        elif parsed.scheme == "ssh" and parsed.hostname is not None:
            hostname = _ssh_hostname(parsed.hostname)
    if hostname != "github.com":
        raise RemoteError(
            "This needs a github.com git remote to name the repository. For another git"
            " host, name a job by its bare id, and give `remote add --create` the"
            " repository with --repository."
        )
    path = path.removesuffix("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) or any(
        part in (".", "..") for part in path.split("/")
    ):
        raise RemoteError("This git remote's URL does not name a GitHub repository (owner/name)")
    return path


# ---- credentials ----


def credentials_path() -> Path:
    """The file where ``fxtr hub login`` keeps tokens, by hub URL:
    ``$XDG_CONFIG_HOME/fxtr/hub.toml``, or ``~/.config/fxtr/hub.toml``."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "fxtr" / "hub.toml"


def hub_token(url: str) -> str | None:
    """The token for the hub at ``url``: :data:`TOKEN_ENV_VAR` if it is set, else the token saved
    for ``url`` in :func:`credentials_path`, else ``None``."""
    token = os.environ.get(TOKEN_ENV_VAR)
    if token:
        return _checked_token(token, TOKEN_ENV_VAR)
    entry = _saved_hosts(credentials_path()).get(_normal_url(url))
    token = entry.get("token") if isinstance(entry, dict) else None  # pyright: ignore[reportUnknownMemberType]
    return _checked_token(token, str(credentials_path())) if isinstance(token, str) else None


def _checked_token(token: str, source: str) -> str:
    """The token without surrounding whitespace, as a CI secret or a pipe can leave it.

    Args:
        source: Where the token came from, for the message.

    Raises:
        RemoteError: If an HTTP header cannot hold the token. The message leaves the token out,
            since it can end up in a CI log.
    """
    token = token.strip()
    if not token or not all(" " < char <= "~" for char in token):
        raise RemoteError(
            f"The hub token from {source} is not valid: it is empty, or holds spaces or control"
            " characters"
        )
    return token


_JOB_PAGE = re.compile(
    r"(?P<origin>https?://[^/?#]+)/projects/(?P<project>[^/?#]+)/jobs/(?P<job>[^/?#]+)(/details)?/?"
)


def job_page_remote(
    address: str, project: Project, environment: Environment
) -> tuple[HubRemote, str] | None:
    """The hub that a job's page is on, as a remote of the project, and the job's id, so a job can
    be pulled from its address with no remote configured.

    Only the token saved for that hub goes with the request, never :data:`TOKEN_ENV_VAR`, which
    holds a token for every hub: an address can come from anyone, and could name any server.

    Args:
        address: What the person gave, which may not be an address at all.
        project: The project that receives the job.
        environment: The environment, which runs git.

    Returns:
        ``None`` if ``address`` is not the address of a job's page.

    Raises:
        RemoteError: If a token may not be sent to the address (:func:`check_hub_url`).
    """
    page = _JOB_PAGE.fullmatch(address)
    if page is None:
        return None
    url = check_hub_url(page["origin"], origin_only=True)
    config = RemoteConfig(
        urllib.parse.urlsplit(url).netloc, "hub", {"url": url, "project": page["project"]}
    )
    entry = _saved_hosts(credentials_path()).get(_normal_url(url))
    token = entry.get("token") if isinstance(entry, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    client = HttpHubClient(
        url,
        page["project"],
        token if isinstance(token, str) else None,
        remote=config.name,
    )
    return (
        HubRemote(
            config, hub=client, git_remote="origin", project=project, environment=environment
        ),
        page["job"],
    )


def save_token(url: str, token: str) -> Path:
    """Save the token for the hub at ``url`` in :func:`credentials_path`, readable only by the
    user, keeping the other hubs' tokens. Return the file's path.

    The tokens go into a new file, readable only by the user, which then replaces the old one.
    So no token is written into a file that others can read, and a crash part way through leaves
    the old file as it was.

    Raises:
        RemoteError: If a token may not be sent to ``url`` (:func:`check_hub_url`), ``url`` is
            not an origin (a remote's ``url`` is one, and the token is looked up by it), the
            token is not valid (:func:`_checked_token`), or the file exists but is not valid TOML.
    """
    url = check_hub_url(url, origin_only=True)
    path = credentials_path()
    hosts = dict(_saved_hosts(path))
    hosts[url] = {"token": _checked_token(token, "this sign-in")}
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"[hosts.{json.dumps(host)}]\ntoken = {json.dumps(entry['token'])}\n"
        for host, entry in sorted(hosts.items())
        if isinstance(entry, dict) and isinstance(entry.get("token"), str)  # pyright: ignore[reportUnknownMemberType]
    ]
    # mkstemp creates the file readable and writable by the user only.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as file:
            file.write("\n".join(lines))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def _saved_hosts(path: Path) -> dict[str, Any]:
    """The ``[hosts]`` table of the credentials file, or an empty table if there is no file.

    Raises:
        RemoteError: If the file is not valid TOML.
    """
    if not path.exists():
        return {}
    try:
        hosts = tomllib.loads(path.read_text()).get("hosts", {})
    except tomllib.TOMLDecodeError as exc:
        raise RemoteError(
            f"{path} is not valid TOML ({exc}). Correct the file, or delete it and run"
            " `fxtr hub login` again for each hub."
        ) from exc
    return dict(hosts) if isinstance(hosts, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


def _normal_url(url: str) -> str:
    return url.rstrip("/")


def _size(size: int) -> str:
    """A file's size for a person to read."""
    return f"{size / 2**20:.1f} MiB" if size >= 2**20 else f"{size / 2**10:.0f} KiB"


# ---- HTTP ----


class HttpHubClient(HubClient):
    """A :class:`HubClient` over the hub's HTTP API (``/v1``), with the standard library's
    ``urllib`` run in a worker thread.

    Args:
        url: The hub's base URL, such as ``https://hub.example``. With a token, it must pass
            :func:`check_hub_url`.
        project: The hub project's id.
        token: The bearer token, or ``None`` to send none (the hub then refuses what needs one).
        remote: The remote's name, for messages.
        timeout: Seconds to wait for each request's connection and each read.

    Raises:
        RemoteError: If ``token`` is given and may not be sent to ``url``.
    """

    def __init__(
        self, url: str, project: str, token: str | None, *, remote: str, timeout: float = 60.0
    ) -> None:
        self.url = _normal_url(url) if token is None else check_hub_url(url)
        self.project = project
        self.token = token
        self.remote = remote
        self.timeout = timeout

    async def create_project(
        self, name: str, repository: str, project_path: str, *, another: bool = False
    ) -> str:
        """Make a hub project of a repository and return its id. The signed-in account must be
        able to read the repository, and becomes the project's admin.

        Args:
            name: The project's name, shown on the hub.
            repository: The repository: ``owner/name`` on GitHub, or the id a development hub's
                configuration gives it.
            project_path: The directory of the repository the project is made from, relative
                to its root. The hub shows it with the project.
            another: Make the project even if this directory of the repository has one.

        Raises:
            RemoteError: If this computer is not signed in to the hub.
            HubRequestError: If the hub refuses, such as for a repository the account cannot
                read or one the hub's GitHub App is not installed on, or with code
                ``project_exists`` and the projects in its details.
        """
        if not self.token:
            raise RemoteError(
                f"Sign in to the hub before you create a project there: run"
                f" fxtr hub login {self.url}"
            )
        answer = await self._json(
            "POST",
            "/v1/projects",
            body={
                "name": name,
                "repository_id": repository,
                "project_path": project_path,
                **({"another": True} if another else {}),
            },
        )
        return answer.field("id", str)

    async def begin_login(self, name: str) -> dict[str, object]:
        return (await self._json("POST", "/v1/cli/authorize", body={"name": name})).object()

    async def poll_login(self, device_code: str) -> dict[str, object]:
        body = {"device_code": device_code}
        return (await self._json("POST", "/v1/cli/token", body=body)).object()

    async def check_push(self) -> None:
        if not self.token:
            raise RemoteError(
                f"Connect this computer before pushing to {self.remote}: run "
                f"fxtr hub login {self.url}"
            )
        await self._json("GET", "/v1/me")
        project = await self._json("GET", self._project_path)
        role = project.optional("role", str)
        if role in ("contributor", "maintainer", "admin"):
            return
        needs = (
            "pushing needs the contributor role or above (write access to the project's repository)"
        )
        connect = f"connect with an account that has it: fxtr hub login {self.url}"
        if role is not None:
            raise RemoteError(
                f"Your account has the {role} role on the hub project of remote"
                f" {self.remote!r}, and {needs}. Ask a repository admin for access, or {connect}"
            )
        # A project with a public job reads without a role when the hub could not check the
        # account's repository access, so no role is not always a refusal.
        raise RemoteError(
            f"The hub gave your account no role on the hub project of remote {self.remote!r},"
            f" and {needs}. If you have write access, the hub may not have been able to check"
            " it just now: try again. Otherwise ask a repository admin for access, or"
            f" {connect}"
        )

    async def job_state(self, job_id: JobID) -> JobState | None:
        found = await self._json_or_none("GET", f"{self._project_path}/jobs/{job_id}")
        return None if found is None else _job_state(found)

    async def create_upload(self, record_cid: str, size: int, idempotency_key: str) -> Upload:
        body = {"record_cid": record_cid, "size": size, "idempotency_key": idempotency_key}
        return _upload(await self._json("POST", f"{self._project_path}/uploads", body=body))

    async def upload(self, upload_id: str) -> Upload:
        return _upload(await self._json("GET", f"/v1/uploads/{upload_id}"))

    async def send(self, upload_id: str, path: Path, offset: int) -> Upload:
        """Send the file from ``offset`` in requests of at most :data:`SEND_CHUNK_BYTES`, each
        from the offset the hub answered the last one with."""
        size = path.stat().st_size

        route = f"/v1/uploads/{upload_id}"

        def put(at: int) -> _Answer:
            with path.open("rb") as file:
                file.seek(at)
                chunk = file.read(SEND_CHUNK_BYTES)
            headers = {"Content-Type": "application/octet-stream", "Upload-Offset": str(at)}
            return self._answer("PUT", route, self._send("PUT", route, data=chunk, headers=headers))

        if offset >= size:
            return await self.upload(upload_id)
        while True:
            try:
                upload = _upload(await _in_thread(functools.partial(put, offset)))
            except HubRequestError as exc:
                # The hub holds another offset than this push thought (a concurrent retry sent
                # bytes, or the hub lost some): it names the offset to continue from.
                held = exc.details.get("offset")
                if exc.code != "offset_conflict" or not isinstance(held, int) or held == offset:
                    raise
                offset = held
                if offset >= size:
                    return await self.upload(upload_id)
                continue
            if upload.offset <= offset:
                raise RemoteError(
                    f"{self.remote} took no bytes of upload {upload_id} at offset {offset}"
                )
            if upload.offset >= size:
                return upload
            # A report each tenth of the file: an upload of one request has none.
            if upload.offset * 10 // size > offset * 10 // size:
                sent = f"{_size(upload.offset)} of {_size(size)}"
                logger.info("uploaded %s (%d%%)", sent, upload.offset * 100 // size)
            offset = upload.offset

    async def publish(
        self, upload_id: str, expected_record_cid: str | None, visibility: str | None = None
    ) -> Upload:
        body: dict[str, object] = {"expected_record_cid": expected_record_cid}
        if visibility is not None:
            body["visibility"] = visibility
        return _upload(await self._json("POST", f"/v1/uploads/{upload_id}/publish", body=body))

    async def set_job_visibility(self, job_id: JobID, visibility: str) -> None:
        await self._json(
            "PUT", f"{self._project_path}/jobs/{job_id}/visibility", body={"visibility": visibility}
        )

    async def create_bundle(self, bundle: ViewBundle, commit: str | None = None) -> str:
        route = f"{self._project_path}/bundles"
        return (await self._json("POST", route, body=bundle_body(bundle, commit))).field("id", str)

    async def set_attachments(self, upload_id: str, bundle_id: str | None, identity: str) -> None:
        body = {"bundle_id": bundle_id, "identity": identity}
        await self._json("PUT", f"/v1/uploads/{upload_id}/attachments", body=body)

    async def tag(self, name: str) -> JobID | None:
        found = await self._json_or_none("GET", self._tag_path(name))
        return None if found is None else found.job_id("job_id")

    async def set_tag(self, name: str, job_id: JobID, expected: JobID | None) -> bool:
        body = {"job_id": str(job_id), "expected": None if expected is None else str(expected)}
        try:
            await self._json("PUT", self._tag_path(name), body=body)
        except HubRequestError as exc:
            if exc.code == "tag_conflict":
                return False
            raise
        return True

    async def download(self, job_id: JobID, path: Path) -> str | None:
        route = f"{self._project_path}/jobs/{job_id}/archive"

        def get() -> Any:
            with path.open("wb") as out:
                return self._send(
                    "GET",
                    route,
                    out=out,
                    missing_ok=True,
                    timeout=max(self.timeout, DOWNLOAD_TIMEOUT),
                )

        headers = await _in_thread(get)
        if headers is _MISSING:
            return None
        record_cid = headers.get(RECORD_CID_HEADER)
        if not record_cid:
            raise RemoteError(f"{self.remote} answered GET {route} without {RECORD_CID_HEADER}")
        return str(record_cid)

    @property
    def _project_path(self) -> str:
        return f"/v1/projects/{self.project}"

    def _tag_path(self, name: str) -> str:
        # A tag may hold "/" and other characters a path segment cannot.
        return f"{self._project_path}/tags/{urllib.parse.quote(name, safe='')}"

    async def _json(
        self, method: str, route: str, *, body: Mapping[str, Any] | None = None
    ) -> _Answer:
        """One request with a JSON body, if given, and the hub's JSON answer."""
        data = None if body is None else json.dumps(body).encode()
        headers = {} if body is None else {"Content-Type": "application/json"}
        found = await _in_thread(lambda: self._send(method, route, data=data, headers=headers))
        return self._answer(method, route, found)

    async def _json_or_none(self, method: str, route: str) -> _Answer | None:
        """As :meth:`_json`, but ``None`` when the hub answers 404: the route names nothing."""
        found = await _in_thread(lambda: self._send(method, route, missing_ok=True))
        return None if found is _MISSING else self._answer(method, route, found)

    def _answer(self, method: str, route: str, data: Any) -> _Answer:
        return _Answer(self.remote, f"{method} {route}", data)

    def _send(
        self,
        method: str,
        route: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        out: BinaryIO | None = None,
        missing_ok: bool = False,
        timeout: float | None = None,
    ) -> Any:
        """One request; the parsed JSON answer (``None`` for an empty body), or :data:`_MISSING`
        for a 404 when ``missing_ok``; with ``out``, the body is written there instead, at most
        :data:`MAX_RECORD_FILE_BYTES` of it, and the answer's headers are returned. ``timeout``
        replaces the client's own for this request."""
        request = urllib.request.Request(
            self.url + route, data=data, method=method, headers=dict(headers or {})
        )
        if self.token is not None:
            request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        what = f"{method} {route}"
        try:
            with _OPENER.open(request, timeout=timeout or self.timeout) as response:
                if out is not None:
                    self._copy(response, out, MAX_RECORD_FILE_BYTES, what)
                    return response.headers
                answer = io.BytesIO()
                self._copy(response, answer, MAX_ANSWER_BYTES, what)
                content = answer.getvalue()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return _MISSING
            raise self._error(exc) from exc
        # http.client.HTTPException covers an answer cut short (IncompleteRead), which is
        # neither a URLError nor an OSError.
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise RemoteError(f"Cannot reach {self.remote} at {self.url}: {_reason(exc)}") from exc
        try:
            return json.loads(content or b"null")
        except ValueError as exc:
            raise RemoteError(
                f"{self.remote} at {self.url} answered {method} {route} with something that is"
                f" not JSON; is {self.url} a hub?"
            ) from exc

    def _copy(
        self, response: http.client.HTTPResponse, out: BinaryIO, limit: int, what: str
    ) -> None:
        """Write a response's body to ``out``, if it is at most ``limit`` bytes: a longer one is
        refused as it arrives, never held whole. A body short of the length the response
        promised is an answer cut short, reported as the hub not reached (a read of a bounded
        amount, unlike one of the whole body, does not raise for it)."""
        written = 0
        while chunk := response.read(_CHUNK):
            written += len(chunk)
            if written > limit:
                raise RemoteError(
                    f"{self.remote} at {self.url} answered {what} with over {limit} bytes; is"
                    f" {self.url} a hub?"
                )
            out.write(chunk)
        if response.length:
            raise RemoteError(
                f"Cannot reach {self.remote} at {self.url}: the answer to {what} ended"
                f" {response.length} bytes short"
            )

    def _error(self, exc: urllib.error.HTTPError) -> HubRequestError:
        if 300 <= exc.code < 400:
            target = exc.headers.get("Location", "elsewhere")
            return HubRequestError(
                exc.code,
                "redirect",
                f"{self.remote} at {self.url} redirects to {target}; requests to a hub are not"
                " redirected, so its token goes only to the URL configured: set url to the"
                " address it redirects to",
            )
        details: Mapping[str, object] = {}
        try:
            error = json.loads(exc.read(MAX_ANSWER_BYTES))["error"]
            code, message = str(error["code"]), str(error["message"])
            if isinstance(error.get("details"), dict):
                details = cast(Mapping[str, object], error["details"])
        except (ValueError, KeyError, TypeError, OSError, http.client.HTTPException):
            # A proxy can close an error response before its body arrives. The received
            # status remains useful even when the hub's structured error is unavailable.
            code, message = str(exc.code), exc.reason
        if self.token is None and exc.code == 403:
            # The hub's own words are for a browser ("sign in"), and say nothing of this computer.
            # With no token from this computer, connecting it is the next step, not asking for
            # access: the account it would connect may have access already.
            hint = f"this computer is not connected to it: run fxtr hub login {self.url}"
            if os.environ.get(TOKEN_ENV_VAR):
                # Only a job's address gets here with the variable set (`job_page_remote`).
                hint += f" ({TOKEN_ENV_VAR} does not go with a job's address)"
            return HubRequestError(
                exc.code, code, f"{self.remote} refused the request ({code}): {hint}", details
            )
        if exc.code == 401:
            return HubRequestError(
                exc.code,
                code,
                f"{self.remote} refused the credentials ({code}: {message}); set"
                f" {TOKEN_ENV_VAR}, or run fxtr hub login {self.url}",
            )
        return HubRequestError(
            exc.code, code, f"{self.remote} refused the request ({code}): {message}", details
        )


def _reason(exc: BaseException) -> str:
    """Why a request did not reach the hub, as the operating system words it ("Connection
    refused"): urllib wraps that in a ``URLError`` whose text (``<urlopen error [Errno 61]
    Connection refused>``) is not for a user."""
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return _reason(reason) if isinstance(reason, BaseException) else str(reason)
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Follow no redirect: urllib's default handler would send the bearer token on to wherever
    the server points, another host included. A redirect is reported as the error it is."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects())

_MISSING = object()
"""What :meth:`HttpHubClient._send` returns for a 404 it was told to expect, as distinct from
an answer whose body is empty or ``null``."""


@dataclasses.dataclass(frozen=True)
class _Answer:
    """A hub's JSON answer to one request, read field by field. A field the hub left out, or
    gave with another type, is reported as the hub's fault (``RemoteError``), not as a
    ``KeyError`` or a ``TypeError`` of this client.

    Attributes:
        remote: The remote's name, for messages.
        request: The request, as ``METHOD /route``, for messages.
        data: The parsed answer: a JSON value, or ``None`` for an empty body.
    """

    remote: str
    request: str
    data: Any

    def field[T](self, name: str, kind: type[T]) -> T:
        """The field ``name``, which must be present and of type ``kind``."""
        value = self.optional(name, kind)
        if value is None:
            raise RemoteError(f"{self.remote} answered {self.request} without {name}")
        return value

    def optional[T](self, name: str, kind: type[T]) -> T | None:
        """The field ``name`` if it is present and not null, which must then be of type
        ``kind``."""
        value = self.data.get(name) if isinstance(self.data, Mapping) else None
        if value is None:
            return None
        if not isinstance(value, kind):
            raise RemoteError(
                f"{self.remote} answered {self.request} with {name} that is not a {kind.__name__}"
            )
        return value

    def nested(self, name: str) -> _Answer | None:
        """The field ``name`` as an answer of its own, if it is present and not null."""
        value = self.optional(name, Mapping)
        return None if value is None else _Answer(self.remote, self.request, value)

    def job_id(self, name: str) -> JobID:
        """The field ``name``, which must be present and a job id (a UUID)."""
        value = self.field(name, str)
        if not _is_uuid(value):
            raise RemoteError(
                f"{self.remote} answered {self.request} with {name} that is not a job id"
            )
        return JobID(uuid.UUID(value))

    def object(self) -> dict[str, object]:
        """The answer, which must be a JSON object."""
        if not isinstance(self.data, dict):
            raise RemoteError(f"{self.remote} answered {self.request} without a JSON object")
        return cast(dict[str, object], self.data)


def bundle_body(bundle: ViewBundle, commit: str | None) -> dict[str, Any]:
    """A bundle as the hub's bundle routes take it: its files (the manifest, the module, the
    stylesheet) in base64 by name, the parsed manifest, and the recorded build commit.
    A null commit means the build's provenance is unknown."""
    return {
        "files": {name: base64.b64encode(data).decode() for name, data in bundle.files.items()},
        "manifest": json.loads(bundle.files[VIEW_BUNDLE_MANIFEST_FILENAME]),
        "commit": commit,
    }


def _job_state(answer: _Answer) -> JobState:
    return JobState(
        answer.field("job_id", str),
        answer.field("record_cid", str),
        answer.optional("attachments", str),
        answer.optional("visibility", str),
    )


def _upload(answer: _Answer) -> Upload:
    error = answer.nested("error")
    return Upload(
        id=answer.field("id", str),
        state=answer.field("state", str),
        offset=answer.optional("offset", int) or 0,
        record_cid=answer.optional("record_cid", str),
        error=None if error is None else (error.field("code", str), error.field("message", str)),
        visibility=answer.optional("visibility", str),
    )


HUB_SETTINGS = (
    Setting("url", str, REQUIRED, "the hub's base URL"),
    Setting("project", str, REQUIRED, "the id of the hub project"),
    Setting("git_remote", str, "origin", "the name of a git remote"),
)
"""The settings of a ``hub`` remote, other than ``kind``."""


def build(
    config: RemoteConfig, settings: Mapping[str, Any], project: Project, environment: Environment
) -> Remote:
    """Build a ``hub`` remote over :class:`HttpHubClient`, with the token :func:`hub_token`
    finds for its URL."""
    url = settings["url"]
    return HubRemote(
        config,
        hub=HttpHubClient(url, settings["project"], hub_token(url), remote=config.name),
        git_remote=settings["git_remote"],
        project=project,
        environment=environment,
    )


register_kind("hub", HUB_SETTINGS, build)


__all__ = [
    "HUB_SETTINGS",
    "JOB_VISIBILITIES",
    "TOKEN_ENV_VAR",
    "HttpHubClient",
    "HubClient",
    "HubRemote",
    "HubRequestError",
    "JobState",
    "Upload",
    "build",
    "check_hub_url",
    "credentials_path",
    "hub_token",
    "save_token",
]
