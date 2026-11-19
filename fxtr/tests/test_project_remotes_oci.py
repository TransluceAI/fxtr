"""Tests for the ``oci`` kind of remote, over a fake registry client: tags, the artifact, push and
pull between two projects, and the git commits that go with them."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import sys
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from postgres_testing import STALE_AFTER, TEST_CODE_VERSION, FakeEnvironment, a_client, a_project

from fxtr.entity_defns.array import Array
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project.client import JobID
from fxtr.project.config import ConfigError, Project, RemoteConfig, RemotesConfig
from fxtr.project.environment import Environment
from fxtr.project.job_record import MAX_RECORD_FILE_BYTES
from fxtr.project.remotes import RemoteError, remote_for
from fxtr.project.remotes.oci import (
    ARTIFACT_TYPE,
    LAYER_MEDIA_TYPE,
    Manifest,
    OciRemote,
    RegistryClient,
    check_repository,
    check_tag,
    default_tag,
)

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres.database import Database  # noqa: E402

pytestmark = pytest.mark.anyio

REPOSITORY = "registry.test/team/jobs"


class FakeRegistry(RegistryClient):
    """A registry in memory: manifests by digest, tags pointing at them, blobs by digest."""

    def __init__(self) -> None:
        self.manifests: dict[str, Manifest] = {}
        self.tags: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}

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
        data = layer.read_bytes()
        blob = "sha256:" + hashlib.sha256(data).hexdigest()
        self.blobs[blob] = data
        body = f"{artifact_type} {blob} {sorted(annotations.items())}".encode()
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        self.manifests[digest] = Manifest(
            digest, artifact_type, ((blob, layer_media_type, len(data)),), dict(annotations)
        )
        for tag in tags:
            self.tags[tag] = digest
        return digest

    async def fetch_manifest(self, repository: str, ref: str) -> Manifest | None:
        return self.manifests.get(self.tags.get(ref, ref))

    async def fetch_layer(self, repository: str, digest: str, path: Path) -> None:
        if digest not in self.blobs:
            raise RemoteError(f"no blob {digest}")
        path.write_bytes(self.blobs[digest])


def oci_remote(
    database: Database,
    registry: RegistryClient,
    environment: Environment,
    repository: str = REPOSITORY,
    annotations: Mapping[str, str] | None = None,
) -> OciRemote:
    config = RemoteConfig("team", "oci", {"repository": repository})
    return OciRemote(
        config,
        repository=repository,
        git_remote="origin",
        project=a_project(database),
        environment=environment,
        registry=registry,
        annotations=annotations,
    )


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


# ---- tags ----


def test_the_default_tag_names_the_workflow_the_time_and_the_job() -> None:
    job = JobID(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    created = datetime(2026, 10, 2, 15, 30, 12, tzinfo=UTC)
    assert default_tag("my.flows/sweep v2", created, job) == (
        "my.flows-sweep-v2-20261002T153012Z-12345678-1234-5678-1234-567812345678"
    )
    # A name that starts with a character a tag cannot start with gets a prefix.
    assert default_tag(".hidden", created, job).startswith("job.hidden-")
    # A long name is shortened to fit, and the job id stays whole.
    long = default_tag("w" * 300, created, job)
    assert len(long) == 128 and long.endswith(str(job))
    assert check_tag(long) == long


@pytest.mark.parametrize("tag", ["", "-x", "has space", "a/b", "x" * 129])
def test_an_invalid_tag_is_refused(tag: str) -> None:
    with pytest.raises(RemoteError, match="not a valid tag"):
        check_tag(tag)


# ---- push and pull ----


async def test_a_job_pushed_as_an_artifact_is_pulled_into_another_project(
    database: Database, elsewhere: Database
) -> None:
    registry = FakeRegistry()
    here = FakeEnvironment()
    source = {"org.opencontainers.image.source": "https://github.com/team/project"}
    async with a_client(database, double, doubled) as client:
        remote = oci_remote(database, registry, here, annotations=source)
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        pushed = await remote.push(client, job, tags=["latest"])
    assert pushed.tags == ("latest",) and registry.tags["latest"] == pushed.version
    manifest = registry.manifests[pushed.version]
    assert manifest.artifact_type == ARTIFACT_TYPE
    assert [media for _, media, _ in manifest.layers] == [LAYER_MEDIA_TYPE]
    assert manifest.annotations["org.transluce.fxtr.job.id"] == str(job)
    assert manifest.annotations["org.transluce.fxtr.job.state"] == "succeeded"
    # The remote's own annotations go beside fxtr's.
    assert manifest.annotations.items() >= source.items()
    # The commits went to the git remote.
    assert here.pushed == [("origin", (TEST_CODE_VERSION.commit,))]
    # Elsewhere lacks the commit, so the pull fetches it first.
    there = FakeEnvironment(known_commits=frozenset())
    async with a_client(elsewhere, double, doubled) as client:
        pulled = await oci_remote(elsewhere, registry, there).pull(client, "latest")
        assert pulled == job
    assert there.fetched == [("origin", (TEST_CODE_VERSION.commit,))]


async def test_a_record_naming_a_commit_this_checkout_lacks_is_refused_before_pushing(
    database: Database,
) -> None:
    registry = FakeRegistry()
    environment = FakeEnvironment(known_commits=frozenset())
    async with a_client(database, double, doubled) as client:
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        with pytest.raises(RemoteError, match="commits this checkout does not have"):
            await oci_remote(database, registry, environment).push(client, job)
    assert registry.manifests == {} and environment.pushed == []


async def test_a_registry_takes_no_job_visibility(database: Database) -> None:
    registry = FakeRegistry()
    async with a_client(database, double, doubled) as client:
        remote = oci_remote(database, registry, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        with pytest.raises(RemoteError, match="only to a hub remote"):
            await remote.push(client, job, visibility="private")
    assert registry.tags == {}


async def test_a_push_without_tags_sets_the_default_tag_and_tags_move(
    database: Database,
) -> None:
    registry = FakeRegistry()
    environment = FakeEnvironment()
    async with a_client(database, double, doubled) as client:
        remote = oci_remote(database, registry, environment)
        first = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        second = (await client.run(doubled, {"x": Array.scalar(1, int)})).job_id
        (default,) = (await remote.push(client, first)).tags
        assert default.startswith(f"{doubled.name}-") and default.endswith(str(first))
        # Tags move to whatever is pushed under them last; each push's digest stays.
        a = await remote.push(client, first, tags=["latest"])
        b = await remote.push(client, second, tags=["latest"])
        assert registry.tags["latest"] == b.version != a.version
        assert registry.tags[default] == a.version  # the same record as the first push
        assert await remote.pull(client, "@" + a.version, update=True) == first


async def test_a_pull_refuses_what_is_not_a_pushed_job(database: Database, tmp_path: Path) -> None:
    registry = FakeRegistry()
    layer = tmp_path / "image.tar"
    layer.write_bytes(b"not a record")
    await registry.push_artifact(
        REPOSITORY,
        layer=layer,
        layer_media_type="application/vnd.oci.image.layer.v1.tar",
        artifact_type="application/vnd.example.other",
        annotations={},
        tags=["image"],
    )
    async with a_client(database, double, doubled) as client:
        remote = oci_remote(database, registry, FakeEnvironment())
        with pytest.raises(RemoteError, match="not a job pushed by fxtr"):
            await remote.pull(client, "image")
        with pytest.raises(RemoteError, match="names nothing"):
            await remote.pull(client, "missing")


async def test_a_pull_of_an_artifact_over_the_record_size_is_refused_before_it_downloads(
    database: Database, tmp_path: Path
) -> None:
    registry = FakeRegistry()
    layer = tmp_path / "job.car"
    layer.write_bytes(b"small")
    digest = await registry.push_artifact(
        REPOSITORY,
        layer=layer,
        layer_media_type=LAYER_MEDIA_TYPE,
        artifact_type=ARTIFACT_TYPE,
        annotations={},
        tags=["huge"],
    )
    manifest = registry.manifests[digest]
    ((blob, media, _),) = manifest.layers
    registry.manifests[digest] = dataclasses.replace(
        manifest, layers=((blob, media, MAX_RECORD_FILE_BYTES + 1),)
    )
    fetched: list[str] = []
    original = registry.fetch_layer

    async def counting(repository: str, digest: str, path: Path) -> None:
        fetched.append(digest)
        await original(repository, digest, path)

    registry.fetch_layer = counting  # type: ignore[method-assign]
    async with a_client(database, double, doubled) as client:
        remote = oci_remote(database, registry, FakeEnvironment())
        with pytest.raises(RemoteError, match=f"'huge' in .* is {MAX_RECORD_FILE_BYTES + 1} bytes"):
            await remote.pull(client, "huge")
    assert fetched == []


async def test_a_pull_of_an_artifact_whose_layer_is_not_a_record_is_refused(
    database: Database, tmp_path: Path
) -> None:
    """An artifact can claim fxtr's type and hold anything: the layer is read as a record, and
    what is not one is the remote's fault, reported as such."""
    registry = FakeRegistry()
    layer = tmp_path / "job.car"
    layer.write_bytes(b"not a record")
    await registry.push_artifact(
        REPOSITORY,
        layer=layer,
        layer_media_type=LAYER_MEDIA_TYPE,
        artifact_type=ARTIFACT_TYPE,
        annotations={},
        tags=["bad"],
    )
    async with a_client(database, double, doubled) as client:
        remote = oci_remote(database, registry, FakeEnvironment())
        with pytest.raises(RemoteError, match="'bad' in .* is not a job record this fxtr reads"):
            await remote.pull(client, "bad")
        assert await client.list_jobs() == []


# ---- the kind's settings ----


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({}, "repository is required"),
        ({"repository": "r"}, "starting with the registry's host"),
        ({"repository": "ghcr.io/r", "tag": "x"}, "unknown setting"),
        ({"repository": "ghcr.io/r", "insecure": "yes"}, "insecure must be"),
        ({"repository": "ghcr.io/r", "git_remote": 1}, "git_remote must be"),
        ({"repository": "ghcr.io/r", "annotations": "x"}, "annotations must be"),
        (
            {"repository": "ghcr.io/r", "annotations": {"a": 1}},
            "must map non-empty names to strings",
        ),
        (
            {"repository": "ghcr.io/r", "annotations": {"org.transluce.fxtr.job.id": "x"}},
            "fxtr writes",
        ),
        (
            {"repository": "ghcr.io/r", "annotations": {"org.opencontainers.image.created": "x"}},
            "fxtr",
        ),
    ],
)
def test_an_oci_remote_needs_valid_settings(
    tmp_path: Path, settings: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        remote_for(remote_project(tmp_path, settings), FakeEnvironment())


def remote_project(tmp_path: Path, settings: dict[str, object]) -> Project:
    return Project(
        root=tmp_path,
        name="test",
        modules=(),
        stale_after=STALE_AFTER,
        database_url="unused",
        schema="unused",
        remotes=RemotesConfig({"team": RemoteConfig("team", "oci", settings)}),
    )


def test_an_oci_remote_is_built_with_the_oras_client(tmp_path: Path) -> None:
    pytest.importorskip("oras")
    from fxtr.project.remotes.oras_client import OrasRegistryClient

    source = {"org.opencontainers.image.source": "https://github.com/team/project"}
    settings = {
        "repository": "localhost:5000/jobs",
        "git_remote": "upstream",
        "insecure": True,
        "annotations": source,
    }
    remote = remote_for(remote_project(tmp_path, settings), FakeEnvironment())
    assert isinstance(remote, OciRemote) and isinstance(remote.registry, OrasRegistryClient)
    assert (remote.repository, remote.git_remote) == ("localhost:5000/jobs", "upstream")
    assert remote.annotations == source
    assert remote.registry._registry.prefix == "http"  # insecure: plain HTTP


def test_an_oci_remote_without_the_extra_says_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A None entry makes importing the module fail, as it does without oras-py.
    monkeypatch.setitem(sys.modules, "fxtr.project.remotes.oras_client", None)
    with pytest.raises(RemoteError, match=r"fxtr\[oci\]"):
        remote_for(remote_project(tmp_path, {"repository": "ghcr.io/r"}), FakeEnvironment())


async def test_the_oras_client_reports_refused_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("oras")
    from fxtr.project.remotes.oras_client import OrasRegistryClient

    client = OrasRegistryClient()

    def refuse(*args: object, **kwargs: object) -> None:
        raise ValueError("Cannot respond to request for authentication.")

    # oras-py raises this ValueError when a registry refuses the credentials.
    monkeypatch.setattr(client._registry, "do_request", refuse)
    with pytest.raises(RemoteError, match="refused the request.*Log in"):
        await client.fetch_manifest("ghcr.io/team/jobs", "latest")


# ---- credentials ----


def test_the_oras_client_reads_the_credential_files_of_the_container_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("oras")
    from fxtr.project.remotes.oras_client import auth_files

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for variable in ("REGISTRY_AUTH_FILE", "XDG_RUNTIME_DIR", "DOCKER_CONFIG"):
        monkeypatch.delenv(variable, raising=False)
    assert auth_files() == []
    files = {
        "REGISTRY_AUTH_FILE": tmp_path / "chosen.json",
        "XDG_RUNTIME_DIR": tmp_path / "run" / "containers" / "auth.json",
        "podman": home / ".config" / "containers" / "auth.json",
        "DOCKER_CONFIG": tmp_path / "docker" / "config.json",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"auths": {}}')
    monkeypatch.setenv("REGISTRY_AUTH_FILE", str(files["REGISTRY_AUTH_FILE"]))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker"))
    assert auth_files() == [str(path) for path in files.values()]
    # A file that does not exist is left out.
    files["podman"].unlink()
    assert str(files["podman"]) not in auth_files()


def write_auth_file(path: Path, auths: dict[str, str]) -> Path:
    entries = {
        host: {"auth": base64.b64encode(credentials.encode()).decode()}
        for host, credentials in auths.items()
    }
    path.write_text(json.dumps({"auths": entries}))
    return path


def test_the_oras_client_reads_only_the_auth_file_when_given(tmp_path: Path) -> None:
    pytest.importorskip("oras")
    import oras.container

    from fxtr.project.remotes.oras_client import OrasRegistryClient

    container = oras.container.Container("ghcr.io/team/jobs:latest")
    auth_file = write_auth_file(tmp_path / "auth.json", {"ghcr.io": "someone:secret"})
    client = OrasRegistryClient(auth_file=auth_file)
    client._log_in(container)
    assert client._registry.auth._basic_auth == base64.b64encode(b"someone:secret").decode()
    other = write_auth_file(tmp_path / "other.json", {"registry.test": "someone:secret"})
    with pytest.raises(RemoteError, match="has no credentials for ghcr.io"):
        OrasRegistryClient(auth_file=other)._log_in(container)
    with pytest.raises(RemoteError, match="Could not read the auth_file"):
        OrasRegistryClient(auth_file=tmp_path / "missing.json")._log_in(container)


def test_an_oci_remote_finds_its_auth_file_from_the_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("oras")
    from fxtr.project.remotes.oras_client import OrasRegistryClient

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for auth_file, expected in [
        ("auth.json", tmp_path / "auth.json"),
        ("~/auth.json", tmp_path / "home" / "auth.json"),
    ]:
        settings: dict[str, object] = {"repository": "ghcr.io/team/jobs", "auth_file": auth_file}
        remote = remote_for(remote_project(tmp_path, settings), FakeEnvironment())
        assert isinstance(remote, OciRemote) and isinstance(remote.registry, OrasRegistryClient)
        assert remote.registry._auth_file == expected


@pytest.mark.parametrize("matching", [True, False])
async def test_the_oras_client_checks_manifest_bytes_against_the_requested_digest(
    monkeypatch: pytest.MonkeyPatch, matching: bool
) -> None:
    pytest.importorskip("oras")
    import requests

    from fxtr.project.remotes.oras_client import OrasRegistryClient

    content = b'{"artifactType":"test","layers":[]}'
    actual = "sha256:" + hashlib.sha256(content).hexdigest()
    requested = actual if matching else "sha256:" + "0" * 64
    response = requests.Response()
    response.status_code = 200
    monkeypatch.setattr(response, "_content", content)
    monkeypatch.setattr(response, "_content_consumed", True)
    client = OrasRegistryClient()

    def no_login(container: object) -> None:
        pass

    def answer(*args: object, **kwargs: object) -> requests.Response:
        return response

    monkeypatch.setattr(client, "_log_in", no_login)
    monkeypatch.setattr(client._registry, "do_request", answer)
    if matching:
        found = await client.fetch_manifest("registry.test/jobs", requested)
        assert found is not None and found.digest == actual
    else:
        with pytest.raises(RemoteError, match="does not match its digest"):
            await client.fetch_manifest("registry.test/jobs", requested)


@pytest.mark.parametrize("matching", [True, False])
async def test_the_oras_client_checks_streamed_layer_bytes_against_the_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, matching: bool
) -> None:
    pytest.importorskip("oras")
    import requests

    from fxtr.project.remotes.oras_client import OrasRegistryClient

    content = b"the archived bytes"
    actual = "sha256:" + hashlib.sha256(content).hexdigest()
    requested = actual if matching else "sha256:" + "0" * 64
    response = requests.Response()
    response.status_code = 200
    monkeypatch.setattr(response, "_content", content)
    monkeypatch.setattr(response, "_content_consumed", True)
    client = OrasRegistryClient()

    def no_login(container: object) -> None:
        pass

    def answer(*args: object, **kwargs: object) -> requests.Response:
        return response

    monkeypatch.setattr(client, "_log_in", no_login)
    monkeypatch.setattr(client._registry, "get_blob", answer)
    path = tmp_path / "record.car"
    if matching:
        await client.fetch_layer("registry.test/jobs", requested, path)
        assert path.read_bytes() == content
    else:
        with pytest.raises(RemoteError, match="does not match"):
            await client.fetch_layer("registry.test/jobs", requested, path)


async def test_a_pull_checks_the_job_named_by_the_artifact_annotations(
    database: Database, elsewhere: Database
) -> None:
    registry = FakeRegistry()
    async with a_client(database, double, doubled) as client:
        remote = oci_remote(database, registry, FakeEnvironment())
        job = (await client.run(doubled, {"x": Array.scalar(3, int)})).job_id
        pushed = await remote.push(client, job, tags=["latest"])
    manifest = registry.manifests[pushed.version]
    registry.manifests[pushed.version] = dataclasses.replace(
        manifest,
        annotations={**manifest.annotations, "org.transluce.fxtr.job.id": str(uuid.uuid4())},
    )
    async with a_client(elsewhere, double, doubled) as client:
        with pytest.raises(RemoteError, match="sent a record that is not"):
            await oci_remote(elsewhere, registry, FakeEnvironment()).pull(client, "latest")


@pytest.mark.parametrize(
    ("repository", "problem"),
    [
        ("ghcr.io/org/jobs", None),
        ("127.0.0.1:8199/acme/jobs", None),
        ("localhost/jobs", None),
        ("jobs", "registry's host"),
        ("org/jobs", "registry's host"),
        ("ghcr.io", "registry's host"),
        ("https://ghcr.io/org/jobs", "no scheme"),
        ("ghcr.io/org/jobs:latest", "no tag"),
        ("ghcr.io/org/jobs@sha256:abc", "no tag"),
    ],
)
def test_a_repository_starts_with_its_registry(repository: str, problem: str | None) -> None:
    """The registry client reads a name with no host as a repository on docker.io."""
    found = check_repository(repository)
    assert found is None if problem is None else problem in str(found)
