"""Tests for the ``hub`` kind of remote: push and pull between two projects over a fake hub that
keeps the hub's rules (each job held once, a publish that expects the hub's record cid,
compare-and-swap tags, idempotent uploads), the HTTP client against a local server, the
settings, and the saved tokens."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from postgres_testing import STALE_AFTER, TEST_CODE_VERSION, FakeEnvironment, a_client, a_project

from fxtr.core import codec
from fxtr.core.car import CarLimits, read_car, write_car
from fxtr.entity_defns.array import Array
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project import cli
from fxtr.project.client import JobID, ProjectClient
from fxtr.project.config import (
    ConfigError,
    Project,
    ProjectError,
    RemoteConfig,
    RemotesConfig,
    ViewsConfig,
)
from fxtr.project.environment import Environment
from fxtr.project.job_record import RecordLimits, read_record
from fxtr.project.remotes import RemoteError, hub_login, pull_command, remote_for
from fxtr.project.remotes.hub import (
    TOKEN_ENV_VAR,
    HttpHubClient,
    HubClient,
    HubRemote,
    HubRequestError,
    JobState,
    Upload,
    _Attachments,  # pyright: ignore[reportPrivateUsage]
    _is_uuid,  # pyright: ignore[reportPrivateUsage]
    _upload_key,  # pyright: ignore[reportPrivateUsage]
    check_hub_url,
    credentials_path,
    hub_token,
    job_page_remote,
    save_token,
)
from fxtr.project.views import (
    VIEW_API_VERSION,
    VIEW_BUNDLE_MANIFEST_FILENAME,
    ViewBundle,
    read_bundle,
)

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres.database import Database  # noqa: E402

pytestmark = pytest.mark.anyio


@dataclasses.dataclass
class HeldJob:
    """A job as the fake hub holds it: its archive (the record file as last pushed, which the
    fake serves as the hub's export), the hub's record cid (the archive's root; a hub computes
    it from what it holds, and a test can set it apart), the attachments, and the visibility."""

    record_cid: str
    archive: bytes
    attachments: str | None
    visibility: str


class FakeHub(HubClient):
    """One hub project in memory, keeping the hub's rules: each job is held once, a publish
    names the record cid it expects, a tag moves only from where the caller saw it, and an
    upload key names one upload."""

    def __init__(self) -> None:
        self.project = "p"
        self.jobs: dict[str, HeldJob] = {}
        self.tags: dict[str, JobID] = {}
        self.uploads: dict[str, Upload] = {}
        self.keys: dict[str, str] = {}
        self.bytes: dict[str, bytes] = {}
        self.publish_requests: list[tuple[str, str | None]] = []
        # Knobs a test sets: the next publish fails with this code; the next send finds the
        # upload expired; setting a tag raises this; a download of a job serves another's
        # archive (under the named job's record cid, as a hub that mixed up its files would).
        self.fail_next_publish: str | None = None
        self.expire_next_send = False
        self.tag_error: RemoteError | None = None
        self.serve_instead: dict[str, str] = {}
        self.bundles: dict[str, tuple[ViewBundle, str | None]] = {}
        self.attachments: dict[str, tuple[str | None, str]] = {}
        # Setting a job's visibility raises this when it is set.
        self.visibility_error: RemoteError | None = None

    @property
    def visibilities(self) -> dict[str, str]:
        return {job_id: held.visibility for job_id, held in self.jobs.items()}

    async def create_bundle(self, bundle: ViewBundle, commit: str | None = None) -> str:
        bundle_id = str(uuid.uuid4())
        self.bundles[bundle_id] = (bundle, commit)
        return bundle_id

    async def set_attachments(self, upload_id: str, bundle_id: str | None, identity: str) -> None:
        assert self.uploads[upload_id].state == "receiving"
        self.attachments[upload_id] = (bundle_id, identity)

    async def check_push(self) -> None:
        pass

    async def job_state(self, job_id: JobID) -> JobState | None:
        held = self.jobs.get(str(job_id))
        return (
            None
            if held is None
            else JobState(str(job_id), held.record_cid, held.attachments, held.visibility)
        )

    async def create_upload(self, record_cid: str, size: int, idempotency_key: str) -> Upload:
        if idempotency_key in self.keys:
            return self.uploads[self.keys[idempotency_key]]
        upload = Upload(str(uuid.uuid4()), "receiving", 0)
        self.uploads[upload.id] = upload
        self.keys[idempotency_key] = upload.id
        self.bytes[upload.id] = b""
        return upload

    async def upload(self, upload_id: str) -> Upload:
        return self.uploads[upload_id]

    async def send(self, upload_id: str, path: Path, offset: int) -> Upload:
        if self.expire_next_send:
            self.expire_next_send = False
            raise HubRequestError(410, "upload_expired", "Upload expired")
        assert offset == len(self.bytes[upload_id])
        self.bytes[upload_id] += path.read_bytes()[offset:]
        upload = Upload(upload_id, "receiving", len(self.bytes[upload_id]))
        self.uploads[upload_id] = upload
        return upload

    async def set_job_visibility(self, job_id: JobID, visibility: str) -> None:
        if self.visibility_error is not None:
            raise self.visibility_error
        assert str(job_id) in self.jobs, "a job's visibility is set once the hub holds it"
        self.jobs[str(job_id)].visibility = visibility

    async def publish(
        self, upload_id: str, expected_record_cid: str | None, visibility: str | None = None
    ) -> Upload:
        self.publish_requests.append((upload_id, expected_record_cid))
        data = self.bytes[upload_id]
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "upload.car"
            path.write_bytes(data)
            file = read_record(path)
        job_id = file.record.job.id
        held = self.jobs.get(job_id)
        current = None if held is None else held.record_cid
        if self.fail_next_publish is not None:
            code, self.fail_next_publish = self.fail_next_publish, None
            upload = Upload(upload_id, "failed", len(data), error=(code, "fix it and push again"))
        elif current != expected_record_cid:
            upload = Upload(
                upload_id,
                "failed",
                len(data),
                error=("record_conflict", f"the record of job {job_id} is {current}"),
            )
        else:
            attached = self.attachments.get(upload_id)
            # The new record holds everything the hub knew, so it is what the hub holds now.
            self.jobs[job_id] = HeldJob(
                file.root.cid_str(),
                data,
                None if attached is None else attached[1],
                # A job the hub holds keeps its visibility; a new job gets the one asked for.
                held.visibility if held is not None else visibility or "shared",
            )
            upload = Upload(
                upload_id,
                "synced",
                len(data),
                record_cid=file.root.cid_str(),
                visibility=self.jobs[job_id].visibility,
            )
        self.uploads[upload_id] = upload
        return upload

    async def tag(self, name: str) -> JobID | None:
        if _is_uuid(name):  # as the hub does: a tag never looks like a job id
            raise HubRequestError(422, "invalid_tag", "Tag cannot be a UUID")
        return self.tags.get(name)

    async def set_tag(self, name: str, job_id: JobID, expected: JobID | None) -> bool:
        if self.tag_error is not None:
            raise self.tag_error
        if self.tags.get(name) != expected:
            return False
        self.tags[name] = job_id
        return True

    async def download(self, job_id: JobID, path: Path) -> str | None:
        held = self.jobs.get(str(job_id))
        if held is None:
            return None
        path.write_bytes(self.jobs[self.serve_instead.get(str(job_id), str(job_id))].archive)
        return held.record_cid


def hub_remote(
    database: Database, hub: HubClient, environment: Environment, project: Project | None = None
) -> HubRemote:
    config = RemoteConfig("team", "hub", {"url": "http://hub.test", "project": "p"})
    return HubRemote(
        config,
        hub=hub,
        git_remote="origin",
        project=a_project(database) if project is None else project,
        environment=environment,
    )


def a_built_bundle(package: Path) -> None:
    """A renderer package with a built bundle in its dist/: a manifest and a module."""
    dist = package / "dist"
    dist.mkdir(parents=True)
    manifest = {"apiVersion": VIEW_API_VERSION, "module": "bundle.js", "stylesheet": None}
    (dist / VIEW_BUNDLE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    (dist / "bundle.js").write_text("export default () => ({ renderers: [] })\n")


@pytest.fixture
async def elsewhere(database: Database) -> AsyncIterator[Database]:
    """A second project's database, on its own schema of the same server."""
    async with Database(database.url, pg_schema=f"test_{uuid.uuid4().hex}") as other:
        await other.create_schema()
        try:
            yield other
        finally:
            with anyio.fail_after(30):
                await other.drop_schema(lock_timeout="5s")


@step(inputs={"x": int}, output=int)
async def double(context: StepContext, x: int) -> int:
    return 2 * x


@workflow(inputs={"x": int}, output=int)
async def doubled(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("double", double, {"x": x})


FAIL = [False]
"""Whether ``flaky`` raises: a job that stops, then succeeds when resumed."""


@step(inputs={"x": int}, output=int)
async def flaky(context: StepContext, x: int) -> int:
    if FAIL[0]:
        raise RuntimeError("not yet")
    return x + 1


@workflow(inputs={"x": int}, output=int)
async def once_flaky(context: WorkflowContext, x: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("flaky", flaky, {"x": x})


# ---- push and pull ----


async def test_a_resumed_job_pushed_again_expects_the_hubs_record_and_replaces_it(
    database: Database, elsewhere: Database
) -> None:
    hub = FakeHub()
    FAIL[0] = True
    try:
        async with a_client(database, flaky, once_flaky) as client:
            job = (await client.run(once_flaky, {"x": Array.scalar(1, int)})).job_id
            stopped = await hub_remote(database, hub, FakeEnvironment()).push(client, job)
    finally:
        FAIL[0] = False
    async with a_client(database, flaky, once_flaky) as client:
        assert (await (await client.resume(job)).wait()).state == "succeeded"
        finished = await hub_remote(database, hub, FakeEnvironment()).push(client, job)
    # Each push names the job, and says which record the hub holds after it; the second push
    # expected the record the first left, and the hub holds the job once, as the second left it.
    assert stopped.ref == finished.ref == str(job)
    assert stopped.version != finished.version == hub.jobs[str(job)].record_cid
    assert [expected for _, expected in hub.publish_requests] == [None, stopped.version]
    async with a_client(elsewhere, flaky, once_flaky) as client:
        remote = hub_remote(elsewhere, hub, FakeEnvironment())
        assert await remote.pull(client, finished.ref) == job
        assert (await client.jobs.status(job)).state == "succeeded"


async def test_a_pull_that_git_cannot_fetch_commits_for_explains_itself_and_imports_nothing(
    database: Database, elsewhere: Database
) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        await hub_remote(database, hub, FakeEnvironment()).push(client, job)

    class NoGitAccess(FakeEnvironment):
        async def fetch_commits(
            self, project: Project, git_remote: str, commits: Iterable[str]
        ) -> None:
            raise ProjectError("fatal: repository not found")

    environment = NoGitAccess()
    environment.known_commits = frozenset()
    async with a_client(elsewhere, double, doubled) as client:
        remote = hub_remote(elsewhere, hub, environment)
        with pytest.raises(RemoteError) as refused:
            await remote.pull(client, str(job))
        message = str(refused.value)
        assert "read access" in message and "fatal: repository not found" in message
        assert f"/jobs/{job}" in message
        assert await client.list_jobs(limit=1) == []


async def test_a_job_that_never_ran_is_refused_before_anything_is_sent(
    database: Database,
) -> None:
    """A job created but never taken by a process has no job attempt, so no commit it ran at
    and nothing a hub would show; the push says so instead of failing inside."""
    hub = FakeHub()
    environment = FakeEnvironment()
    async with a_client(database, double, doubled) as client:
        job = await client.jobs.create(doubled, {"x": Array.scalar(1, int)})
        with pytest.raises(RemoteError, match=f"Job {job} has never run.*nothing to publish"):
            await hub_remote(database, hub, environment).push(client, job)
    assert hub.uploads == {} and environment.pushed == []


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "/name",
        "name/",
        "a//b",
        ".",
        "a/../b",
        "a\x00b",
        "x" * 129,
        "00000000-0000-0000-0000-000000000001",
        "tag:odd",
    ],
)
async def test_invalid_tag_is_refused_before_exporting_or_pushing(
    database: Database,
    tag: str,
) -> None:
    hub = FakeHub()
    environment = FakeEnvironment()
    async with a_client(database, double, doubled) as client:
        # A missing job makes an attempted export fail, independently of tag validation.
        with pytest.raises(RemoteError, match="Tag"):
            await hub_remote(database, hub, environment).push(
                client, JobID(uuid.uuid4()), tags=[tag]
            )
    assert hub.uploads == {} and environment.pushed == []


async def test_a_record_naming_a_commit_this_checkout_lacks_is_refused_before_pushing(
    database: Database,
) -> None:
    """The job ran from another checkout sharing the database, or the commit was pruned: the
    push says which commits are missing, where git would fail with its own message."""
    hub = FakeHub()
    environment = FakeEnvironment(known_commits=frozenset())
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        with pytest.raises(RemoteError, match="commits this checkout does not have"):
            await hub_remote(database, hub, environment).push(client, job)
    assert hub.uploads == {} and environment.pushed == []


async def test_a_push_captures_one_snapshot_before_a_resume(database: Database) -> None:
    hub = FakeHub()
    FAIL[0] = True
    try:
        async with a_client(database, flaky, once_flaky) as client:
            job = (await client.run(once_flaky, {"x": Array.scalar(1, int)})).job_id
    finally:
        FAIL[0] = False
    exported: list[str] = []
    async with a_client(database, flaky, once_flaky) as client:
        export_job = client.export_job

        async def export_then_resume(job_id: JobID, path: Path) -> Any:
            root = await export_job(job_id, path)
            exported.append(root.cid_str())
            assert (await (await client.resume(job_id)).wait()).state == "succeeded"
            return root

        client.export_job = export_then_resume  # type: ignore[method-assign]
        pushed = await hub_remote(database, hub, FakeEnvironment()).push(client, job)
    # The hub got the record as exported, before the resume.
    assert exported == [pushed.version] and len(hub.uploads) == 1


async def test_a_push_gives_a_new_job_its_visibility_and_changes_a_published_ones(
    database: Database,
) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        pushed = await remote.push(client, job, visibility="private")
        assert pushed.visibility == "private"
        # The first sync carried it: the job was never shared.
        assert hub.visibilities == {str(job): "private"}
        unchanged = await remote.push(client, job)
        assert unchanged.unchanged and unchanged.visibility == "private"
        assert hub.visibilities == {str(job): "private"}
        await remote.push(client, job, visibility="public")
        assert hub.visibilities == {str(job): "public"}
        hub.visibility_error = HubRequestError(403, "forbidden", "only the owner")
        with pytest.raises(
            RemoteError, match=r"was pushed to team \(.*\), but .* not set to shared"
        ):
            await remote.push(client, job, visibility="shared")
        assert hub.visibilities == {str(job): "public"}


def test_the_cli_takes_a_visibility_for_a_push_and_for_a_published_job() -> None:
    job = str(uuid.uuid4())
    pushed = cli._parser().parse_args(["push", job, "--visibility", "private"])
    assert pushed.visibility == "private"
    assert cli._parser().parse_args(["push", job]).visibility is None
    changed = cli._parser().parse_args(["hub", "visibility", job, "public"])
    assert (changed.job, changed.visibility, changed.remote) == (job, "public", None)
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["hub", "visibility", job, "everyone"])


async def test_a_hub_remote_changes_a_published_jobs_visibility(database: Database) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        await remote.push(client, job)
        await remote.set_visibility(job, "public")
    assert hub.visibilities == {str(job): "public"}


async def test_a_pulled_job_is_pushed_with_its_execution_record(
    database: Database, elsewhere: Database
) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        await hub_remote(database, hub, FakeEnvironment()).push(client, job, tags=["one"])
    async with a_client(elsewhere, double, doubled) as client:
        await hub_remote(elsewhere, hub, FakeEnvironment()).pull(client, "one")
        other = FakeHub()
        pushed = await hub_remote(elsewhere, other, FakeEnvironment()).push(client, job)
    assert pushed.ref == str(job) and list(other.jobs) == [str(job)]


async def test_other_bytes_for_one_record_take_their_own_upload(
    database: Database, tmp_path: Path
) -> None:
    """Another fxtr can write the same record as other bytes. An upload holds one archive's
    bytes, so the other archive must not resume the first one's upload."""
    hub = FakeHub()
    remote = hub_remote(database, hub, FakeEnvironment())
    attachments = _Attachments(bundle=None)
    one, other = tmp_path / "one.car", tmp_path / "other.car"
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        root = (await client.export_job(job, one)).cid_str()
    # The same record, its blocks in another order: the same root, other bytes.
    (car_root,), blocks = read_car(one)
    write_car(other, [car_root], list(reversed(list(blocks.items()))))
    assert other.read_bytes() != one.read_bytes() and read_record(other).root.cid_str() == root
    first = await remote._sync(job, one, root, None, attachments)  # pyright: ignore[reportPrivateUsage]
    # Over the same expected record, so only the bytes tell the two apart; the hub then refuses
    # the second publish, because it holds the job now. It must not answer with the first upload.
    with pytest.raises(RemoteError, match="record_conflict"):
        await remote._sync(job, other, root, None, attachments)  # pyright: ignore[reportPrivateUsage]
    assert len(hub.keys) == 2
    # The first archive, sent again, finds the upload it made.
    assert await remote._sync(job, one, root, None, attachments) == first  # pyright: ignore[reportPrivateUsage]


async def test_a_published_job_is_pulled_into_another_project(
    database: Database, elsewhere: Database
) -> None:
    hub = FakeHub()
    here = FakeEnvironment()
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        pushed = await hub_remote(database, hub, here).push(client, job, tags=["baseline"])
    # The tag names the job; a push of a job the hub does not hold expects no record.
    assert hub.jobs[str(job)].record_cid == pushed.version and hub.tags == {"baseline": job}
    assert [expected for _, expected in hub.publish_requests] == [None]
    assert here.pushed == [("origin", (TEST_CODE_VERSION.commit,))]
    there = FakeEnvironment(known_commits=frozenset())
    async with a_client(elsewhere, double, doubled) as client:
        assert await hub_remote(elsewhere, hub, there).pull(client, "baseline") == job
    assert there.fetched == [("origin", (TEST_CODE_VERSION.commit,))]
    # The job's id (what the push returned) names it too.
    async with a_client(elsewhere, double, doubled) as client:
        remote = hub_remote(elsewhere, hub, FakeEnvironment())
        assert await remote.pull(client, pushed.ref, update=True) == job


class SlowHub(FakeHub):
    """A hub that takes a while to merge an upload: queued, then processing, then synced."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting: list[str] = ["processing", "processing"]
        self.synced: Upload | None = None

    async def publish(
        self, upload_id: str, expected_record_cid: str | None, visibility: str | None = None
    ) -> Upload:
        self.synced = await super().publish(upload_id, expected_record_cid, visibility)
        return dataclasses.replace(self.synced, state="queued")

    async def upload(self, upload_id: str) -> Upload:
        assert self.synced is not None
        if self.waiting:
            return dataclasses.replace(self.synced, state=self.waiting.pop())
        return self.synced


async def test_a_push_reports_each_stage(
    database: Database, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fxtr.project.remotes.hub.WAIT_REPORT_SECONDS", 0.0)
    hub = SlowHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        with caplog.at_level(logging.INFO, logger="fxtr.project.remotes"):
            pushed = await remote.push(client, job)
            reports = [
                record.getMessage()
                for record in caplog.records
                if record.name.startswith("fxtr.project.remotes")
            ]
            assert not pushed.unchanged
            assert reports[:2] == [f"exporting job {job}", "sending its commits to origin"]
            assert reports[2].startswith("uploading it to team: ")
            assert reports[3] == "waiting for team: queued"
            assert reports[4] == "waiting for team: processing"
            assert reports[5].startswith("waiting for team: still processing, ")
            # A push with nothing to send has nothing to wait for.
            caplog.clear()
            assert (await remote.push(client, job)).unchanged
            assert not [r for r in caplog.records if "upload" in r.getMessage()]


async def test_a_send_reports_each_tenth_of_a_large_file(
    server: tuple[str, Recorded],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url, recorded = server
    monkeypatch.setattr("fxtr.project.remotes.hub.SEND_CHUNK_BYTES", 2**19)
    held = {"offset": 0}

    def respond(method: str, path: str, headers: dict[str, str], body: bytes) -> Answer | None:
        held["offset"] += len(body)
        return (200, {"id": "u1", "state": "receiving", "offset": held["offset"]})

    recorded.respond = respond
    file = tmp_path / "job.car"
    file.write_bytes(bytes(2**20 + 2**19))
    client = HttpHubClient(url, "proj", "secret", remote="team")
    with caplog.at_level(logging.INFO, logger="fxtr.project.remotes"):
        await client.send("u1", file, 0)
    assert [record.getMessage() for record in caplog.records] == [
        "uploaded 512 KiB of 1.5 MiB (33%)",
        "uploaded 1.0 MiB of 1.5 MiB (66%)",
    ]


async def test_a_push_sends_the_built_views(database: Database, tmp_path: Path) -> None:
    hub = FakeHub()
    a_built_bundle(tmp_path / "views")
    project = dataclasses.replace(
        a_project(database), views=ViewsConfig(package=tmp_path / "views")
    )
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment(), project)
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        await remote.push(client, job)
    ((bundle_id, identity),) = hub.attachments.values()
    assert bundle_id in hub.bundles
    path = tmp_path / "job.car"
    path.write_bytes(hub.jobs[str(job)].archive)
    history = read_record(path).record.execution
    assert history is not None and history.job_id == str(job)
    assert len(history.job_attempts) == 1
    bundle, commit = hub.bundles[bundle_id]
    assert set(bundle.files) == {VIEW_BUNDLE_MANIFEST_FILENAME, "bundle.js"}
    assert commit is None
    assert (
        identity == bundle.version == hub.jobs[str(job)].attachments
    )  # what a later push compares
    assert len(hub.bundles) == 1


async def test_pushing_an_unchanged_job_again_sends_nothing(database: Database) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        first = await remote.push(client, job)
        again = await remote.push(client, job, tags=["latest"])
    assert again.ref == first.ref == str(job) and len(hub.uploads) == 1
    assert (first.unchanged, again.unchanged) == (False, True)
    assert again.version == first.version == hub.jobs[str(job)].record_cid
    assert hub.tags == {"latest": job}
    # The pull command names the job, with or without tags: what you push is what you pull.
    assert pull_command(a_project(database), first).endswith(f"pull {job} --remote team")
    assert pull_command(a_project(database), again).endswith(f"pull {job} --remote team")


async def test_pushing_a_job_again_with_other_attachments_syncs_it_again(
    database: Database, tmp_path: Path
) -> None:
    hub = FakeHub()
    project = dataclasses.replace(
        a_project(database), views=ViewsConfig(package=tmp_path / "views")
    )
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment(), project)
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        without_views = await remote.push(client, job)
        a_built_bundle(tmp_path / "views")
        with_views = await remote.push(client, job)
        again = await remote.push(client, job)
    # The record is the same, so the record cid alone would say "unchanged"; the attachments
    # differ, so the second push syncs the job again, expecting the record the first left.
    assert without_views.ref == with_views.ref == again.ref == str(job)
    assert without_views.version == with_views.version == hub.jobs[str(job)].record_cid
    assert (with_views.unchanged, again.unchanged) == (False, True)
    assert [expected for _, expected in hub.publish_requests] == [None, without_views.version]
    assert len(hub.uploads) == 2 and hub.jobs[str(job)].attachments is not None


async def test_a_push_from_a_checkout_with_no_built_bundle_leaves_the_jobs_attachments(
    database: Database, tmp_path: Path
) -> None:
    hub = FakeHub()
    project = dataclasses.replace(
        a_project(database), views=ViewsConfig(package=tmp_path / "views")
    )
    a_built_bundle(tmp_path / "views")
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment(), project)
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        await remote.push(client, job)
        attachments = hub.jobs[str(job)].attachments
        assert attachments is not None
        shutil.rmtree(tmp_path / "views" / "dist")
        again = await remote.push(client, job)
    assert again.unchanged and hub.jobs[str(job)].attachments == attachments


async def test_a_push_over_a_record_that_changed_is_refused(database: Database) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id

        # Someone else pushes the job between this push's look at it and its publish.
        looked = hub.job_state

        async def state_then_someone_else_pushes(job_id: JobID) -> JobState | None:
            state = await looked(job_id)
            hub.jobs[str(job_id)] = HeldJob("someone-else", b"", None, "shared")
            return state

        hub.job_state = state_then_someone_else_pushes  # type: ignore[method-assign]
        with pytest.raises(
            RemoteError, match=r"refused job .* \(record_conflict\).*pull it first.*--replace"
        ):
            await remote.push(client, job)
    # The job is as the other push left it.
    assert hub.jobs[str(job)].record_cid == "someone-else"
    assert [upload.state for upload in hub.uploads.values()] == ["failed"]


async def test_a_tag_that_moved_meanwhile_is_reported_and_the_job_stays_synced(
    database: Database,
) -> None:
    hub = FakeHub()
    moved = JobID(uuid.uuid4())
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        read = hub.tag

        async def tag_then_it_moves(name: str) -> JobID | None:
            seen = await read(name)
            hub.tags[name] = moved
            return seen

        hub.tag = tag_then_it_moves  # type: ignore[method-assign]
        with pytest.raises(RemoteError, match="were not set: baseline"):
            await remote.push(client, job, tags=["baseline"])
    assert str(job) in hub.jobs and hub.tags == {"baseline": moved}


async def test_a_retried_push_resumes_the_upload_it_made(database: Database) -> None:
    hub = FakeHub()
    send = hub.send
    lost = [True]

    async def send_half_then_lose_the_connection(upload_id: str, path: Path, offset: int) -> Upload:
        if not lost[0]:
            return await send(upload_id, path, offset)
        lost[0] = False
        hub.bytes[upload_id] = path.read_bytes()[: path.stat().st_size // 2]
        hub.uploads[upload_id] = Upload(upload_id, "receiving", len(hub.bytes[upload_id]))
        raise RemoteError("Cannot reach team: connection lost")

    hub.send = send_half_then_lose_the_connection  # type: ignore[method-assign]
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        with pytest.raises(RemoteError, match="connection lost"):
            await remote.push(client, job)
        # The same file over the same expected record is the same upload: the push sends the
        # rest of it, not a fresh one.
        again = await remote.push(client, job)
        # A push whose answer was lost after the hub synced it finds the job held: nothing sent.
        once_more = await remote.push(client, job)
    assert again.ref == str(job) and not again.unchanged and once_more.unchanged
    assert again.version == once_more.version == hub.jobs[str(job)].record_cid
    assert len(hub.uploads) == 1 and len(hub.publish_requests) == 1


async def test_after_a_refused_publish_is_fixed_the_next_push_takes_a_fresh_upload(
    database: Database,
) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        hub.fail_next_publish = "pin_missing"
        with pytest.raises(RemoteError, match=r"refused the job \(pin_missing\)"):
            await remote.push(client, job)
        # The same record over the same expected record: the failed upload is passed over, not
        # reused.
        pushed = await remote.push(client, job)
    failed, fresh = hub.uploads.values()
    assert failed.state == "failed" and fresh.record_cid == pushed.version
    assert hub.jobs[str(job)].record_cid == pushed.version


async def test_a_push_gives_up_a_hub_that_answers_every_key_with_a_failed_upload(
    database: Database,
) -> None:
    class FailsEveryUpload(FakeHub):
        async def create_upload(self, record_cid: str, size: int, idempotency_key: str) -> Upload:
            upload = Upload(str(uuid.uuid4()), "failed", 0, error=("pin_missing", "no pin"))
            self.uploads[upload.id] = upload
            return upload

    hub = FailsEveryUpload()
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        with pytest.raises(RemoteError, match="128 upload keys in a row"):
            await hub_remote(database, hub, FakeEnvironment()).push(client, job)
    assert len(hub.uploads) == 128 and hub.jobs == {}


async def test_a_job_with_many_failed_pushes_can_be_pushed_once_their_cause_is_fixed(
    database: Database,
) -> None:
    class FailedEarlierPushes(FakeHub):
        """Answers the keys of a job's chain with the failed uploads of earlier pushes."""

        failed = 0

        async def create_upload(self, record_cid: str, size: int, idempotency_key: str) -> Upload:
            if self.failed == 64:
                return await super().create_upload(record_cid, size, idempotency_key)
            self.failed += 1
            upload = Upload(str(uuid.uuid4()), "failed", 0, error=("pin_missing", "no pin"))
            self.uploads[upload.id] = upload
            return upload

    hub = FailedEarlierPushes()
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        await hub_remote(database, hub, FakeEnvironment()).push(client, job)
    assert str(job) in hub.jobs


async def test_a_push_passes_over_earlier_failed_uploads(
    database: Database, tmp_path: Path
) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        await client.export_job(job, tmp_path / "job.car")
        # The key names the archive's bytes; an export of the job writes the same bytes each time.
        archive = hashlib.sha256((tmp_path / "job.car").read_bytes()).hexdigest()
        attachments = None
        # Twenty earlier pushes of this archive over no record, each refused, along the key
        # chain.
        key = _upload_key(archive, None, attachments, after=None)
        for _ in range(20):
            failed = Upload(str(uuid.uuid4()), "failed", 0, error=("pin_missing", "no pin"))
            hub.uploads[failed.id] = failed
            hub.keys[key] = failed.id
            key = _upload_key(archive, None, attachments, after=failed.id)
        pushed = await hub_remote(database, hub, FakeEnvironment()).push(client, job)
    assert hub.jobs[str(job)].record_cid == pushed.version and not pushed.unchanged
    assert len(hub.uploads) == 21


async def test_an_upload_that_expired_is_replaced_by_a_fresh_one(database: Database) -> None:
    hub = FakeHub()
    hub.expire_next_send = True
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        pushed = await remote.push(client, job)
    assert hub.jobs[str(job)].record_cid == pushed.version and not pushed.unchanged
    assert len(hub.uploads) == 2


async def test_a_tag_the_hub_refuses_is_reported_with_the_synced_job(
    database: Database,
) -> None:
    hub = FakeHub()
    hub.tag_error = HubRequestError(403, "forbidden", "team refused the request (forbidden)")
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        with pytest.raises(RemoteError) as raised:
            await remote.push(client, job, tags=["baseline"])
    assert f"was pushed to team ({hub.jobs[str(job)].record_cid})" in str(raised.value)
    assert "baseline (team refused the request (forbidden))" in str(raised.value)


async def test_a_pull_refuses_a_download_that_is_not_the_job_it_names(
    database: Database, elsewhere: Database
) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        first = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        second = (await client.run(doubled, {"x": Array.scalar(2, int)})).job_id
        one = await remote.push(client, first, tags=["one"])
        await remote.push(client, second)
    hub.serve_instead[str(first)] = str(second)
    async with a_client(elsewhere, double, doubled) as client:
        remote = hub_remote(elsewhere, hub, FakeEnvironment())
        for ref in (one.ref, "one", "tag:one"):
            with pytest.raises(RemoteError, match=f"is not job {first}"):
                await remote.pull(client, ref)
        assert await client.list_jobs() == []


async def test_a_pull_of_a_download_cut_short_is_refused_and_imports_nothing(
    database: Database, elsewhere: Database
) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        await remote.push(client, job, tags=["one"])
    hub.jobs[str(job)].archive = hub.jobs[str(job)].archive[:9]
    async with a_client(elsewhere, double, doubled) as client:
        remote = hub_remote(elsewhere, hub, FakeEnvironment())
        with pytest.raises(RemoteError, match="is not a job record"):
            await remote.pull(client, "one")
        assert await client.list_jobs() == []


async def test_a_pull_of_a_root_that_is_not_entity_data_is_refused(
    database: Database, elsewhere: Database, tmp_path: Path
) -> None:
    """A block's bytes can match its cid and still not be DAG-CBOR: anyone can hash bytes."""
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        await remote.push(client, job, tags=["one"])
    data = b"\xff\xff\xff"
    cid = codec.compute_cid(data)
    write_car(tmp_path / "garbage.car", [cid], [(cid, data)])
    hub.jobs[str(job)].archive = (tmp_path / "garbage.car").read_bytes()
    async with a_client(elsewhere, double, doubled) as client:
        remote = hub_remote(elsewhere, hub, FakeEnvironment())
        with pytest.raises(RemoteError, match="is not a job record.*root is not entity data"):
            await remote.pull(client, "one")
        assert await client.list_jobs() == []


async def test_a_pull_reads_the_download_under_the_pull_limits(
    database: Database, elsewhere: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        await remote.push(client, job, tags=["one"])
    monkeypatch.setattr(
        "fxtr.project.remotes.IMPORT_LIMITS",
        RecordLimits(car=CarLimits(max_compressed_bytes=1)),
    )
    async with a_client(elsewhere, double, doubled) as client:
        remote = hub_remote(elsewhere, hub, FakeEnvironment())
        with pytest.raises(RemoteError, match="is not a job record.*over the limit of 1"):
            await remote.pull(client, "one")


async def test_a_pull_of_a_name_the_hub_does_not_know_is_refused(database: Database) -> None:
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, FakeHub(), FakeEnvironment())
        with pytest.raises(RemoteError, match="names no readable job or tag"):
            await remote.pull(client, "missing")
        # A UUID names a job, never a tag: the download finds out the hub has no such job.
        with pytest.raises(RemoteError, match="names no readable job or tag"):
            await remote.pull(client, str(uuid.uuid4()))


# ---- the HTTP client ----


Answer = tuple[int, Any] | tuple[int, Any, dict[str, str]]


class Recorded:
    """What the local server received, and what it answers: a fixed answer by method and path,
    or one ``respond`` computes from the request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.answers: dict[tuple[str, str], Answer] = {}
        self.respond: Callable[[str, str, dict[str, str], bytes], Answer | None] | None = None


@pytest.fixture
def server() -> Iterator[tuple[str, Recorded]]:
    with serving() as served:
        yield served


@pytest.fixture
def elsewhere_server() -> Iterator[tuple[str, Recorded]]:
    """A second local server, on another origin."""
    with serving() as served:
        yield served


@contextlib.contextmanager
def serving() -> Iterator[tuple[str, Recorded]]:
    recorded = Recorded()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            headers = dict(self.headers)
            recorded.requests.append((self.command, self.path, headers, body))
            answer = recorded.respond and recorded.respond(self.command, self.path, headers, body)
            answer = answer or recorded.answers.get((self.command, self.path), (404, None))
            status, payload, *extra = answer
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            if status == 404 and payload is None:
                data = json.dumps({"error": {"code": "not_found", "message": "no"}}).encode()
            self.send_response(status)
            response_headers = {"Content-Length": str(len(data)), **(extra[0] if extra else {})}
            for name, value in response_headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = _answer

        def log_message(self, format: str, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", recorded
    finally:
        httpd.shutdown()
        httpd.server_close()


async def test_the_http_client_speaks_the_hubs_routes(
    server: tuple[str, Recorded], tmp_path: Path
) -> None:
    url, recorded = server
    client = HttpHubClient(url + "/", "proj", "secret", remote="team")
    job = JobID(uuid.uuid4())
    assert await client.job_state(job) is None  # a 404 is "the hub holds nothing of the job"
    recorded.answers[("POST", "/v1/projects/proj/uploads")] = (
        201,
        {"id": "u1", "state": "receiving", "offset": 0, "size": 10},
    )
    upload = await client.create_upload("bafy", 10, "key")
    assert upload == Upload("u1", "receiving", 0)
    file = tmp_path / "job.car"
    file.write_bytes(b"0123456789")
    recorded.answers[("PUT", "/v1/uploads/u1")] = (
        200,
        {"id": "u1", "state": "receiving", "offset": 10},
    )
    assert (await client.send("u1", file, 4)).offset == 10
    recorded.answers[("GET", "/v1/uploads/u1")] = (
        200,
        {
            "id": "u1",
            "state": "failed",
            "offset": 10,
            "error": {"code": "pin_missing", "message": "abc"},
        },
    )
    assert (await client.upload("u1")).error == ("pin_missing", "abc")
    recorded.answers[("PUT", "/v1/uploads/u1/attachments")] = (204, b"")
    await client.set_attachments("u1", "bundle-1", "bundle-version")
    recorded.answers[("POST", "/v1/uploads/u1/publish")] = (
        202,
        {"id": "u1", "state": "queued", "offset": 10, "record_cid": None},
    )
    assert (await client.publish("u1", "bafy", "private")).state == "queued"
    recorded.answers[("GET", "/v1/uploads/u1")] = (
        200,
        {"id": "u1", "state": "synced", "offset": 10, "job_id": str(job), "record_cid": "bafz"},
    )
    assert (await client.upload("u1")).record_cid == "bafz"  # the sync's receipt
    state = {
        "project_id": "proj",
        "job_id": str(job),
        "record_cid": "bafz",
        "attachments": "bundle-version",
        "synced_by": "alice",
    }
    recorded.answers[("GET", f"/v1/projects/proj/jobs/{job}")] = (200, state)
    assert await client.job_state(job) == JobState(str(job), "bafz", "bundle-version")
    recorded.answers[("GET", "/v1/projects/proj/tags/a%2Fb")] = (
        200,
        {"name": "a/b", "job_id": str(job)},
    )
    assert await client.tag("a/b") == job
    recorded.answers[("PUT", "/v1/projects/proj/tags/a%2Fb")] = (
        409,
        {"error": {"code": "tag_conflict", "message": "moved"}},
    )
    other = JobID(uuid.uuid4())
    assert await client.set_tag("a/b", other, job) is False
    recorded.answers[("GET", f"/v1/projects/proj/jobs/{job}/archive")] = (
        200,
        b"car bytes",
        {"X-Fxtr-Record-Cid": "bafz"},
    )
    assert await client.download(job, tmp_path / "out.car") == "bafz"
    assert (tmp_path / "out.car").read_bytes() == b"car bytes"
    assert await client.download(other, tmp_path / "none.car") is None  # a 404: no such job

    by_route = {
        (method, path): (headers, body) for method, path, headers, body in recorded.requests
    }
    headers, body = by_route[("POST", "/v1/projects/proj/uploads")]
    assert headers["Authorization"] == "Bearer secret"
    assert json.loads(body) == {"record_cid": "bafy", "size": 10, "idempotency_key": "key"}
    headers, body = by_route[("PUT", "/v1/uploads/u1")]
    assert (headers["Upload-Offset"], body) == ("4", b"456789")  # only what the hub lacks
    _, body = by_route[("PUT", "/v1/uploads/u1/attachments")]
    assert json.loads(body) == {"bundle_id": "bundle-1", "identity": "bundle-version"}
    _, body = by_route[("POST", "/v1/uploads/u1/publish")]
    assert json.loads(body) == {"expected_record_cid": "bafy", "visibility": "private"}
    _, body = by_route[("PUT", "/v1/projects/proj/tags/a%2Fb")]
    assert json.loads(body) == {"job_id": str(other), "expected": str(job)}


async def test_an_upload_is_sent_in_bounded_chunks_and_a_complete_one_is_not_sent_again(
    server: tuple[str, Recorded], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, recorded = server
    monkeypatch.setattr("fxtr.project.remotes.hub.SEND_CHUNK_BYTES", 4)
    held = bytearray()

    def respond(method: str, path: str, headers: dict[str, str], body: bytes) -> Answer | None:
        """A hub that takes a chunk only at the offset it has, and at most 4 bytes of it."""
        if path != "/v1/uploads/u1":
            return None
        if method == "PUT":
            if int(headers["Upload-Offset"]) != len(held) or len(body) > 4:
                return (409, {"error": {"code": "offset_conflict", "message": "no"}})
            held.extend(body)
        return (200, {"id": "u1", "state": "receiving", "offset": len(held)})

    recorded.respond = respond
    file = tmp_path / "job.car"
    file.write_bytes(b"0123456789")
    client = HttpHubClient(url, "proj", "secret", remote="team")
    assert (await client.send("u1", file, 0)).offset == 10 and bytes(held) == b"0123456789"
    puts = [h["Upload-Offset"] for m, _, h, _ in recorded.requests if m == "PUT"]
    assert puts == ["0", "4", "8"]
    # Every byte arrived but the last answer was lost: resuming sends nothing.
    recorded.requests.clear()
    assert (await client.send("u1", file, 10)).offset == 10
    assert [m for m, *_ in recorded.requests] == ["GET"]


async def test_a_send_resumes_from_the_offset_a_conflict_names(
    server: tuple[str, Recorded], tmp_path: Path
) -> None:
    """A retry of a push can find the hub holding more than this push sent (an earlier try's
    bytes arrived after their answer was lost); the hub's refusal names what it holds."""
    url, recorded = server
    held = bytearray(b"0123")

    def respond(method: str, path: str, headers: dict[str, str], body: bytes) -> Answer | None:
        """A hub that takes a chunk only at the offset it has, and at most 6 bytes of it."""
        if path != "/v1/uploads/u1":
            return None
        if method == "PUT":
            if int(headers["Upload-Offset"]) != len(held) or len(body) > 6:
                conflict = {
                    "code": "offset_conflict",
                    "message": "no",
                    "details": {"offset": len(held)},
                }
                return (409, {"error": conflict})
            held.extend(body)
        return (200, {"id": "u1", "state": "receiving", "offset": len(held)})

    recorded.respond = respond
    file = tmp_path / "job.car"
    file.write_bytes(b"0123456789")
    client = HttpHubClient(url, "proj", "secret", remote="team")
    assert (await client.send("u1", file, 0)).offset == 10 and bytes(held) == b"0123456789"
    puts = [h["Upload-Offset"] for m, _, h, _ in recorded.requests if m == "PUT"]
    assert puts == ["0", "4"]
    # A conflict at the offset this push sent from is a real disagreement, reported as such.
    held[:] = b"012"
    file.write_bytes(b"0123456789abc")
    recorded.requests.clear()
    with pytest.raises(HubRequestError) as refused:
        await client.send("u1", file, 3)
    assert refused.value.code == "offset_conflict" and refused.value.details == {"offset": 3}


async def test_the_http_client_follows_no_redirect_so_the_token_stays_with_the_hub(
    server: tuple[str, Recorded], elsewhere_server: tuple[str, Recorded]
) -> None:
    url, recorded = server
    other, elsewhere = elsewhere_server
    recorded.answers[("GET", "/v1/uploads/u1")] = (302, b"", {"Location": other + "/v1/uploads/u1"})
    elsewhere.answers[("GET", "/v1/uploads/u1")] = (200, {"id": "u1", "state": "receiving"})
    client = HttpHubClient(url, "proj", "test-only-token", remote="team")
    with pytest.raises(HubRequestError, match=f"redirects to {other}") as raised:
        await client.upload("u1")
    assert raised.value.code == "redirect"
    assert elsewhere.requests == []


async def test_the_http_client_reports_the_hubs_errors(server: tuple[str, Recorded]) -> None:
    url, recorded = server
    client = HttpHubClient(url, "proj", None, remote="team")
    recorded.answers[("POST", "/v1/uploads/u1/publish")] = (
        413,
        {"error": {"code": "limit", "message": "the record is too large"}},
    )
    with pytest.raises(RemoteError, match=r"team refused the request \(limit\): the record is too"):
        await client.publish("u1", None)
    recorded.answers[("GET", "/v1/uploads/u2")] = (
        401,
        {"error": {"code": "unauthenticated", "message": "no token"}},
    )
    with pytest.raises(RemoteError, match=f"set {TOKEN_ENV_VAR}, or run fxtr hub login"):
        await client.upload("u2")
    # Nothing listens on port 1: the message carries the system's reason, not urllib's wrapper.
    unreachable = HttpHubClient("http://127.0.0.1:1", "proj", None, remote="team")
    with pytest.raises(RemoteError, match="^Cannot reach team at http://127.0.0.1:1: Connection"):
        await unreachable.upload("u")


# ---- settings and tokens ----


def remote_project(tmp_path: Path, settings: dict[str, object]) -> Project:
    return Project(
        root=tmp_path,
        name="test",
        modules=(),
        stale_after=STALE_AFTER,
        database_url="unused",
        schema="unused",
        remotes=RemotesConfig({"team": RemoteConfig("team", "hub", settings)}),
    )


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"project": "p"}, "url is required"),
        ({"url": "http://hub.test"}, "project is required"),
        ({"url": "", "project": "p"}, r"url must be the hub's base URL \(must not be empty\)"),
        ({"url": "http://hub.test", "project": ""}, r"project must be .* \(must not be empty\)"),
        ({"url": "http://hub.test", "project": "p", "repository": "r"}, "unknown setting"),
    ],
)
def test_a_hub_remote_needs_valid_settings(
    tmp_path: Path, settings: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        remote_for(remote_project(tmp_path, settings), FakeEnvironment())


def test_a_jobs_address_names_its_hub_and_sends_only_that_hubs_saved_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv(TOKEN_ENV_VAR, "for-every-hub")
    project = remote_project(tmp_path, {"url": "https://other.test", "project": "x"})
    save_token("https://hub.test/", "saved")
    found = job_page_remote(
        "https://hub.test/projects/p/jobs/j/details", project, FakeEnvironment()
    )
    assert found is not None
    remote, job = found
    assert job == "j" and remote.name == "hub.test" and remote.git_remote == "origin"
    assert isinstance(remote.hub, HttpHubClient)
    assert (remote.hub.url, remote.hub.project, remote.hub.token) == (
        "https://hub.test",
        "p",
        "saved",
    )
    # An address could name any server, so the token for every hub does not go to it.
    found = job_page_remote("https://elsewhere.test/projects/p/jobs/j", project, FakeEnvironment())
    assert found is not None
    assert isinstance(found[0].hub, HttpHubClient) and found[0].hub.token is None
    # What is not a job's address goes to the project's remotes.
    assert job_page_remote("https://hub.test/projects/p", project, FakeEnvironment()) is None
    assert job_page_remote("baseline", project, FakeEnvironment()) is None
    # A token may not go over plain HTTP to another computer.
    with pytest.raises(RemoteError):
        job_page_remote("http://hub.test/projects/p/jobs/j", project, FakeEnvironment())


async def test_a_refusal_with_no_token_says_to_connect_this_computer(
    server: tuple[str, Recorded], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, recorded = server
    recorded.answers[("GET", "/v1/uploads/u1")] = (
        403,
        {"error": {"code": "no_access", "message": "You don't have access to this project."}},
    )
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with pytest.raises(RemoteError, match=f"not connected to it: run fxtr hub login {url}$"):
        await HttpHubClient(url, "proj", None, remote="hub").upload("u1")
    # The token for every hub is set, but did not go with a job's address.
    monkeypatch.setenv(TOKEN_ENV_VAR, "for-every-hub")
    with pytest.raises(RemoteError, match=f"{TOKEN_ENV_VAR} does not go with a job's address"):
        await HttpHubClient(url, "proj", None, remote="hub").upload("u1")
    # A computer with a token has connected, so the hub's refusal is about the account.
    with pytest.raises(RemoteError, match="You don't have access"):
        await HttpHubClient(url, "proj", "secret", remote="hub").upload("u1")


def test_ctrl_c_ends_a_request_the_hub_never_answers() -> None:
    """A hub can take minutes to send a download's first byte, and someone who stops waiting must
    not wait for the socket's timeout too."""
    script = """
import signal, sys
from fxtr.project import cli
from fxtr.project.remotes.hub import HttpHubClient
# A test worker ignores this signal, and a child starts as its parent left it.
signal.signal(signal.SIGINT, signal.default_int_handler)
client = HttpHubClient(sys.argv[1], "p", "token", remote="team")
async def hung(_argv):
    await client.upload("u")
cli.main = hung
print("asking", flush=True)
cli.entry()
"""
    with socket.create_server(("127.0.0.1", 0)) as server:
        url = f"http://127.0.0.1:{server.getsockname()[1]}"
        process = subprocess.Popen(
            [sys.executable, "-c", script, url], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        try:
            assert process.stdout is not None
            assert process.stdout.readline() == b"asking\n"
            connection, _ = server.accept()
            with connection:
                process.send_signal(signal.SIGINT)
                # Well under the client's own timeout, which is a minute.
                assert process.wait(timeout=10) == cli.EXIT_INTERRUPTED
        finally:
            process.kill()
            process.wait()


def test_a_token_loses_surrounding_whitespace_and_is_refused_with_no_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "secret-token-value\n")
    assert hub_token("http://hub.test") == "secret-token-value"
    monkeypatch.setenv(TOKEN_ENV_VAR, "secret\ntoken")
    with pytest.raises(RemoteError) as refused:
        hub_token("http://hub.test")
    assert "secret" not in str(refused.value) and TOKEN_ENV_VAR in str(refused.value)


def test_a_hub_remote_is_built_with_the_saved_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    save_token("https://hub.test/", "saved")
    settings: dict[str, object] = {
        "url": "https://hub.test",
        "project": "p",
        "git_remote": "upstream",
    }
    remote = remote_for(remote_project(tmp_path, settings), FakeEnvironment())
    assert isinstance(remote, HubRemote) and isinstance(remote.hub, HttpHubClient)
    assert (remote.hub.token, remote.hub.project, remote.git_remote) == ("saved", "p", "upstream")


def test_a_token_goes_only_over_https_or_to_this_computer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bearer token is sent only to an https URL, or over http to a loopback host, where a
    development hub runs: wherever a token is attached to a request, or saved for a URL."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv(TOKEN_ENV_VAR, "secret")
    settings: dict[str, object] = {"url": "http://hub.example", "project": "p"}
    with pytest.raises(RemoteError, match="sent only over HTTPS: http://hub.example is not"):
        remote_for(remote_project(tmp_path, settings), FakeEnvironment())
    with pytest.raises(RemoteError, match="sent only over HTTPS"):
        HttpHubClient("http://hub.example", "p", "secret", remote="team")
    with pytest.raises(RemoteError, match="sent only over HTTPS"):
        save_token("http://hub.example", "secret")
    # A token is saved for an origin: a remote's url is one, and a token is looked up by it.
    with pytest.raises(RemoteError, match="must be an origin"):
        save_token("https://hub.example/v1", "secret")
    with pytest.raises(RemoteError, match="no user name or password"):
        HttpHubClient("https://user:pw@hub.example", "p", "secret", remote="team")
    assert hub_token("http://hub.example") == "secret"  # the variable is read, not sent
    # Without a token, the URL is only normalized: a browser login begins with no token.
    assert (
        HttpHubClient("http://hub.example/", "p", None, remote="team").url == "http://hub.example"
    )
    for allowed in (
        "https://hub.example/",
        "http://127.0.0.1:8097",
        "http://localhost",
        "http://[::1]:80",
    ):
        assert HttpHubClient(allowed, "p", "secret", remote="team").url == allowed.rstrip("/")
        assert check_hub_url(allowed) == allowed.rstrip("/")


def test_tokens_are_saved_per_hub_readable_only_by_the_user_and_the_variable_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    assert hub_token("https://a.test") is None
    path = save_token("https://a.test", "one")
    save_token("https://b.test/", 'two"quoted')
    save_token("https://a.test/", "three")  # replaces a's, keeps b's
    assert path == credentials_path() == tmp_path / "fxtr" / "hub.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (hub_token("https://a.test/"), hub_token("https://b.test")) == ("three", 'two"quoted')
    monkeypatch.setenv(TOKEN_ENV_VAR, "from-env")
    assert hub_token("https://a.test") == "from-env"


def test_a_token_file_is_replaced_whole_and_readable_only_by_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    path = save_token("https://a.test", "one")
    path.chmod(0o644)  # as a user or another tool might leave it
    save_token("https://b.test", "two")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (hub_token("https://a.test"), hub_token("https://b.test")) == ("one", "two")
    assert [p.name for p in path.parent.iterdir()] == ["hub.toml"]  # no temporary file left

    def fails(*args: object) -> None:
        raise OSError("disk full")

    # A save that fails part way leaves the saved tokens as they were.
    monkeypatch.setattr("os.replace", fails)
    with pytest.raises(OSError, match="disk full"):
        save_token("https://c.test", "three")
    assert (hub_token("https://a.test"), hub_token("https://c.test")) == ("one", None)
    assert [p.name for p in path.parent.iterdir()] == ["hub.toml"]


def test_a_token_file_that_is_not_toml_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    credentials_path().parent.mkdir(parents=True)
    credentials_path().write_text("[hosts\n")
    with pytest.raises(RemoteError, match="is not valid TOML"):
        hub_token("https://a.test")
    with pytest.raises(RemoteError, match="is not valid TOML"):
        save_token("https://a.test", "one")


@pytest.mark.parametrize("commit", [None, "a" * 40])
async def test_bundle_uploads_preserve_known_or_unknown_build_provenance(
    server: tuple[str, Recorded], tmp_path: Path, commit: str | None
) -> None:
    url, recorded = server
    client = HttpHubClient(url, "proj", None, remote="team")
    recorded.answers[("POST", "/v1/projects/proj/bundles")] = (201, {"id": "bundle"})
    a_built_bundle(tmp_path / "views")
    bundle = read_bundle(tmp_path / "views" / "dist")
    assert await client.create_bundle(bundle, commit) == "bundle"
    method, path, _, body = recorded.requests[0]
    assert (method, path) == ("POST", "/v1/projects/proj/bundles")
    assert json.loads(body)["commit"] == commit


async def test_an_answer_that_is_not_json_is_reported(server: tuple[str, Recorded]) -> None:
    url, recorded = server
    recorded.respond = lambda method, path, headers, body: (200, b"<html>not a hub</html>")
    client = HttpHubClient(url, "proj", None, remote="team")
    with pytest.raises(RemoteError, match="not JSON; is .* a hub"):
        await client.job_state(JobID(uuid.uuid4()))


async def test_an_answer_of_the_wrong_shape_is_the_hubs_fault(
    server: tuple[str, Recorded], tmp_path: Path
) -> None:
    """A field the hub leaves out, or an empty body where an object is due, is reported as the
    remote's answer, not as this client's KeyError."""
    url, recorded = server
    client = HttpHubClient(url, "proj", None, remote="team")
    route = "/v1/projects/proj/uploads"
    for answer in ({}, b"", {"id": 1}, [1]):
        recorded.answers[("POST", route)] = (200, answer)
        with pytest.raises(RemoteError, match=f"team answered POST {route} with(out id| id that)"):
            await client.create_upload("bafy", 10, "key")
    recorded.answers[("GET", "/v1/projects/proj/tags/a")] = (200, {"name": "a"})
    with pytest.raises(RemoteError, match="without job_id"):
        await client.tag("a")
    recorded.answers[("GET", "/v1/projects/proj/tags/a")] = (200, {"job_id": "not-a-uuid"})
    with pytest.raises(RemoteError, match="with job_id that is not a job id"):
        await client.tag("a")
    job = JobID(uuid.uuid4())
    recorded.answers[("GET", f"/v1/projects/proj/jobs/{job}/archive")] = (200, b"car bytes")
    with pytest.raises(RemoteError, match="archive without X-Fxtr-Record-Cid"):
        await client.download(job, tmp_path / "out.car")
    recorded.answers[("POST", "/v1/projects/proj/bundles")] = (201, b"")
    a_built_bundle(tmp_path / "views")
    with pytest.raises(RemoteError, match="answered POST /v1/projects/proj/bundles without id"):
        await client.create_bundle(read_bundle(tmp_path / "views" / "dist"), "a" * 40)
    recorded.answers[("GET", "/v1/uploads/u1")] = (
        200,
        {"id": "u1", "state": "failed", "error": "broken"},
    )
    with pytest.raises(RemoteError, match="error that is not a Mapping"):
        await client.upload("u1")


async def test_an_answer_or_a_download_over_the_bound_is_refused(
    server: tuple[str, Recorded], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, recorded = server
    monkeypatch.setattr("fxtr.project.remotes.hub.MAX_ANSWER_BYTES", 16)
    monkeypatch.setattr("fxtr.project.remotes.hub.MAX_RECORD_FILE_BYTES", 16)
    client = HttpHubClient(url, "proj", None, remote="team")
    recorded.answers[("GET", "/v1/uploads/u1")] = (200, {"id": "u1", "state": "receiving"})
    with pytest.raises(RemoteError, match="GET /v1/uploads/u1 with over 16 bytes"):
        await client.upload("u1")
    job = JobID(uuid.uuid4())
    recorded.answers[("GET", f"/v1/projects/proj/jobs/{job}/archive")] = (200, bytes(17))
    with pytest.raises(RemoteError, match="archive with over 16 bytes"):
        await client.download(job, tmp_path / "out.car")
    assert (tmp_path / "out.car").stat().st_size <= 16


async def test_hub_login_saves_a_token_read_from_standard_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    login = ["hub", "login"]
    monkeypatch.setattr("sys.stdin", _Stdin("tok\n"))
    assert await cli.main([*login, "https://hub.test", "--token-stdin"]) == cli.EXIT_OK
    assert hub_token("https://hub.test") == "tok"
    assert "saved the token for https://hub.test" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", _Stdin("\n"))
    assert await cli.main([*login, "https://hub.test", "--token-stdin"]) == cli.EXIT_REFUSED
    assert "no token given" in capsys.readouterr().err
    # A token is not saved for a URL it may not be sent to, nor for one that is not an origin:
    # a remote with that url would ask for /v1/v1/..., and would not find the token.
    monkeypatch.setattr("sys.stdin", _Stdin("tok\n"))
    assert await cli.main([*login, "http://hub.test", "--token-stdin"]) == cli.EXIT_REFUSED
    assert "sent only over HTTPS" in capsys.readouterr().err
    assert hub_token("http://hub.test") is None
    monkeypatch.setattr("sys.stdin", _Stdin("tok\n"))
    assert await cli.main([*login, "https://hub.test/v1", "--token-stdin"]) == cli.EXIT_REFUSED
    assert "must be an origin" in capsys.readouterr().err
    assert hub_token("https://hub.test/v1") is None


def test_hub_login_signs_in_through_the_browser_unless_a_token_is_given() -> None:
    parser = cli._parser()
    login = parser.parse_args(["hub", "login"])
    assert (login.url, login.no_browser, login.token_stdin) == (None, False, False)
    assert parser.parse_args(["hub", "login", "--no-browser"]).no_browser
    with pytest.raises(SystemExit):  # one or the other: a token read from stdin needs no link
        parser.parse_args(["hub", "login", "--no-browser", "--token-stdin"])


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def readline(self) -> str:
        return self._text


@pytest.mark.parametrize("status", [200, 503])
async def test_a_short_http_response_is_reported_without_a_traceback(
    server: tuple[str, Recorded], status: int
) -> None:
    url, recorded = server
    recorded.respond = lambda method, path, headers, body: (status, b"{", {"Content-Length": "100"})
    client = HttpHubClient(url, "proj", None, remote="team")
    with pytest.raises(RemoteError) as refused:
        await client.job_state(JobID(uuid.uuid4()))
    if status == 503:
        assert isinstance(refused.value, HubRequestError)
        assert refused.value.status == 503
        assert "Service Unavailable" in str(refused.value)
    else:
        assert "Cannot reach team" in str(refused.value)


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:acme/research.git",
        "ssh://git@github.com/acme/research.git",
        "https://github.com/acme/research",
        "https://user:secret@github.com/acme/research.git",
    ],
)
def test_github_identity_excludes_transport_and_credentials(url: str) -> None:
    from fxtr.project.remotes.hub import github_repository

    assert github_repository(url) == "acme/research"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.attacker.test/acme/research.git",
        "https://elsewhere.test/acme/research",
        "https://github.com/../research",
        "file:///tmp/research",
        "https://github.com/acme/research/extra",
        "git@elsewhere.test:acme/research.git",
        "https://github.com-work/acme/research.git",
    ],
)
def test_a_url_that_is_not_a_github_repository_names_none(url: str) -> None:
    from fxtr.project.remotes.hub import github_repository

    with pytest.raises(RemoteError):
        github_repository(url)


@pytest.mark.parametrize(
    "url",
    ["git@github.com-work:acme/research.git", "ssh://git@github.com-work/acme/research.git"],
)
def test_an_ssh_host_name_for_github_names_the_repository(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second GitHub account's key has a host name of its own in the SSH configuration."""
    from fxtr.project.remotes import hub

    def ssh_config(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert command == ["ssh", "-G", "github.com-work"]
        return subprocess.CompletedProcess(
            command, 0, stdout="user git\nhostname github.com\nport 22\n"
        )

    monkeypatch.setattr(hub.subprocess, "run", ssh_config)
    assert hub.github_repository(url) == "acme/research"


def test_an_ssh_host_name_is_not_github_when_ssh_cannot_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxtr.project.remotes import hub

    def no_ssh(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(hub.subprocess, "run", no_ssh)
    with pytest.raises(RemoteError, match="github.com git remote"):
        hub.github_repository("git@github.com-work:acme/research.git")


async def test_login_without_url_stores_credentials_for_the_default_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("FXTR_HUB_URL", "http://localhost:8097")
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr("sys.stdin", _Stdin("tok\n"))
    assert await cli.main(["hub", "login", "--token-stdin"]) == cli.EXIT_OK
    assert hub_token("http://localhost:8097") == "tok"
    assert hub_token("https://different.test") is None


def test_release_hub_and_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from fxtr.project.remotes import hub

    monkeypatch.delenv("FXTR_HUB_URL", raising=False)
    monkeypatch.setattr(hub, "DEFAULT_HUB_URL", "https://official.test")
    assert hub.default_hub_url() == "https://official.test"
    monkeypatch.setenv("FXTR_HUB_URL", "http://localhost:8097/")
    assert hub.default_hub_url() == "http://localhost:8097"
    for invalid in (
        "http://other.test",
        "https://user:secret@other.test",
        "https://other.test/path",
    ):
        monkeypatch.setenv("FXTR_HUB_URL", invalid)
        with pytest.raises(RemoteError):
            hub.default_hub_url()


class GitHubCheckout(FakeEnvironment):
    async def repository_binding(self, root: Path, git_remote: str) -> tuple[str, str]:
        return "git@github.com:acme/research.git", "nested"


@pytest.mark.parametrize("prefix", ["", "research/", "acme/research/", "ACME/RESEARCH/"])
async def test_cli_push_and_pull_accept_the_same_job_reference(
    database: Database,
    elsewhere: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    prefix: str,
) -> None:
    hub = FakeHub()
    checkout = GitHubCheckout()
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(4, int)})).job_id
    destination = database

    @contextlib.asynccontextmanager
    async def opener(
        project: Path | None, *, verify_environment: bool
    ) -> AsyncIterator[ProjectClient]:
        async with a_client(destination, double, doubled, environment=checkout) as client:
            yield client

    def select_remote(project: Project, environment: Environment, name: str | None) -> HubRemote:
        return hub_remote(destination, hub, environment, project)

    monkeypatch.setattr("fxtr.project.remotes.remote_for", select_remote)
    ref = f"{prefix}{job}"
    assert await cli.main(["push", ref], open_client=opener) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert f"pull {job}" in output and "visibility: shared" in output
    assert f"view it at: http://hub.test/projects/p/jobs/{job}\n" in output
    destination = elsewhere
    assert await cli.main(["pull", ref], open_client=opener) == cli.EXIT_OK
    async with a_client(elsewhere, double, doubled) as client:
        assert await client.jobs.result(job) == Array.scalar(8, int)


async def test_qualified_job_references_refuse_the_wrong_repository(database: Database) -> None:
    hub = FakeHub()
    remote = hub_remote(database, hub, GitHubCheckout())
    job = JobID(uuid.uuid4())
    for prefix in ("other/research", "another", "https://github.com/acme/research", "../research"):
        with pytest.raises(RemoteError, match="this checkout uses"):
            await remote.job_id(f"{prefix}/{job}")
    with pytest.raises(RemoteError, match="job UUID"):
        await remote.job_id("acme/research/not-a-uuid")
    assert hub.publish_requests == []


async def test_job_and_tag_references_resolve_to_jobs(database: Database) -> None:
    hub = FakeHub()
    job = JobID(uuid.uuid4())
    other = JobID(uuid.uuid4())
    hub.tags["release/stable"] = other
    # A tag can look like a qualified job reference; tag: says which it is.
    hub.tags[f"acme/research/{job}"] = other
    remote = hub_remote(database, hub, GitHubCheckout())
    # A job reference is parsed, not looked up: the download finds out whether the hub holds it.
    assert await remote._resolve(str(job)) == job  # pyright: ignore[reportPrivateUsage]
    assert await remote._resolve(f"acme/research/{job}") == job  # pyright: ignore[reportPrivateUsage]
    assert await remote._resolve("release/stable") == other  # pyright: ignore[reportPrivateUsage]
    assert await remote._resolve(f"tag:acme/research/{job}") == other  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RemoteError, match="names no readable job or tag"):
        await remote._resolve("tag:missing")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RemoteError, match="this checkout uses"):
        await remote._resolve(f"other/repo/{job}")  # pyright: ignore[reportPrivateUsage]


async def test_the_pull_hint_names_the_job_even_when_the_push_set_a_tag(
    database: Database,
) -> None:
    # A tag moves with later pushes; the job's id always gets the job's latest push, so the
    # command a push prints names the id whatever tags the push set.
    hub = FakeHub()
    async with a_client(database, double, doubled) as client:
        remote = hub_remote(database, hub, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(5, int)})).job_id
        pushed = await remote.push(client, job, tags=["release", "latest"])
    assert pushed.tags == ("release", "latest") and pushed.ref == str(job)
    assert pull_command(a_project(database), pushed) == f"fxtr pull {job} --remote team"
    assert pushed.version == hub.jobs[str(job)].record_cid


@pytest.mark.parametrize(
    ("lifetime", "approval", "interval", "slow_down_to"),
    [(900, 720, 6, None), (12, 9, 3, None), (12, None, 3, None), (15, 12, 6, 12)],
)
async def test_browser_login_uses_the_hubs_lifetime_and_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
    lifetime: int,
    approval: int | None,
    interval: int,
    slow_down_to: int | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    elapsed = 0.0
    polls: list[float] = []

    class LoginClient(HttpHubClient):
        async def begin_login(self, name: str) -> dict[str, object]:
            return {
                "device_code": "device",
                "user_code": "ABCD-EFGH",
                "verification_uri": "http://localhost:8090/approve?source=cli",
                "expires_in": lifetime,
                "interval": interval,
            }

        async def poll_login(self, device_code: str) -> dict[str, object]:
            polls.append(elapsed)
            if slow_down_to is not None and len(polls) == 1:
                return {"status": "slow_down", "interval": slow_down_to}
            if approval is not None and elapsed >= approval:
                return {"status": "connected", "token": "hub_cli_connected"}
            return {"status": "pending"}

    async def sleep(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(hub_login, "HttpHubClient", LoginClient)
    monkeypatch.setattr(hub_login, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(hub_login.anyio, "sleep", sleep)
    if approval is None:
        with pytest.raises(RemoteError, match="connection request expired"):
            await hub_login.browser_login("http://localhost:8090", open_browser=False)
        assert elapsed == lifetime
    else:
        assert (
            await hub_login.browser_login("http://localhost:8090", open_browser=False)
            == "hub_cli_connected"
        )
        assert elapsed == approval
    waited = slow_down_to or interval
    assert polls == list(range(0, int(elapsed) + (waited if approval is not None else 0), waited))
    assert "http://localhost:8090/approve?source=cli&code=ABCD-EFGH" in capsys.readouterr().out


async def test_a_push_without_credentials_is_refused_before_exporting_or_pushing_pins(
    database: Database,
) -> None:
    environment = FakeEnvironment()
    hub = HttpHubClient("http://localhost:8090", "project", None, remote="team")
    async with a_client(database, double, doubled) as client:
        with pytest.raises(RemoteError, match="fxtr hub login"):
            await hub_remote(database, hub, environment).push(client, JobID(uuid.uuid4()))
    assert environment.pushed == []


@pytest.mark.parametrize(
    "failure",
    [
        RemoteError("Cannot reach hub: The handshake operation timed out"),
        HubRequestError(503, "unavailable", "hub refused the request (unavailable)"),
        HubRequestError(429, "too_many", "hub refused the request (too_many)"),
    ],
)
async def test_browser_login_keeps_waiting_when_a_check_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: RemoteError
) -> None:
    answers: list[dict[str, object] | RemoteError] = [
        failure,
        failure,
        {"status": "pending"},
        {"status": "connected", "token": "hub_cli_connected"},
    ]

    class LoginClient(HttpHubClient):
        async def begin_login(self, name: str) -> dict[str, object]:
            return {
                "device_code": "device",
                "user_code": "ABCD-EFGH",
                "verification_uri": "http://localhost:8090/approve",
                "expires_in": 900,
                "interval": 5,
            }

        async def poll_login(self, device_code: str) -> dict[str, object]:
            answer = answers.pop(0)
            if isinstance(answer, RemoteError):
                raise answer
            return answer

    async def sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(hub_login, "HttpHubClient", LoginClient)
    monkeypatch.setattr(hub_login.anyio, "sleep", sleep)
    token = await hub_login.browser_login("http://localhost:8090", open_browser=False)
    assert token == "hub_cli_connected" and not answers
    assert capsys.readouterr().out.count("Still waiting") == 1


async def test_browser_login_stops_when_the_hub_refuses_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LoginClient(HttpHubClient):
        async def begin_login(self, name: str) -> dict[str, object]:
            return {
                "device_code": "device",
                "user_code": "ABCD-EFGH",
                "verification_uri": "http://localhost:8090/approve",
                "expires_in": 900,
                "interval": 5,
            }

        async def poll_login(self, device_code: str) -> dict[str, object]:
            raise HubRequestError(400, "connection_denied", "hub refused the request")

    monkeypatch.setattr(hub_login, "HttpHubClient", LoginClient)
    with pytest.raises(HubRequestError, match="hub refused the request"):
        await hub_login.browser_login("http://localhost:8090", open_browser=False)


async def test_browser_login_refuses_a_connection_page_on_another_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LoginClient(HttpHubClient):
        async def begin_login(self, name: str) -> dict[str, object]:
            return {
                "device_code": "device",
                "user_code": "ABCD-EFGH",
                "expires_in": 900,
                "interval": 3,
                "verification_uri": "https://another.test/connect",
            }

        async def poll_login(self, device_code: str) -> dict[str, object]:
            return {"status": "connected", "token": "hub_cli_test"}

    monkeypatch.setattr(hub_login, "HttpHubClient", LoginClient)
    with pytest.raises(RemoteError, match="another origin"):
        await hub_login.browser_login("http://localhost:8090", open_browser=False)
