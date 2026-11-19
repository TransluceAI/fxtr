"""Tests of the ``oci`` kind against a real OCI registry, through the oras-py client.

They run only when ``FXTR_TEST_OCI_REGISTRY`` names a registry (``host:port``) that accepts
anonymous pushes over plain HTTP, and the ``oci`` extra is installed. To run them
locally, start a throwaway registry:

    podman run -d --rm --name fxtr-registry -p 127.0.0.1:55000:5000 docker.io/library/registry:3
    export FXTR_TEST_OCI_REGISTRY=127.0.0.1:55000

``podman stop fxtr-registry`` stops it and deletes what it holds. Each test pushes to its own
repository, so tests do not see each other's artifacts. The tests of
``test_project_remotes_oci`` cover the kind's logic over a fake client; these check
the client's requests.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from postgres_testing import FakeEnvironment, a_client
from test_project_remotes_oci import (  # noqa: F401
    double,
    doubled,
    elsewhere,
    oci_remote,
)

from fxtr.entity_defns.array import Array
from fxtr.project.remotes import RemoteError
from fxtr.project.remotes.oci import ARTIFACT_TYPE, LAYER_MEDIA_TYPE

REGISTRY = os.environ.get("FXTR_TEST_OCI_REGISTRY")

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(REGISTRY is None, reason="FXTR_TEST_OCI_REGISTRY is not set"),
]

pytest.importorskip("oras")
pytest.importorskip("sqlalchemy")

from fxtr.project.remotes.oras_client import OrasRegistryClient  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402


@pytest.fixture
def repository() -> str:
    """A repository of the test registry that only this test uses."""
    return f"{REGISTRY}/fxtr-test-{uuid.uuid4().hex}/jobs"


@pytest.fixture
def client() -> OrasRegistryClient:
    return OrasRegistryClient(insecure=True)


async def test_the_client_pushes_an_artifact_and_fetches_it_by_tag_and_digest(
    client: OrasRegistryClient, repository: str, tmp_path: Path
) -> None:
    layer = tmp_path / "job.car"
    layer.write_bytes(b"a record file")
    digest = await client.push_artifact(
        repository,
        layer=layer,
        layer_media_type=LAYER_MEDIA_TYPE,
        artifact_type=ARTIFACT_TYPE,
        annotations={"org.transluce.fxtr.job.id": "j"},
        tags=["one", "two"],
    )
    assert digest.startswith("sha256:")
    for ref in ("one", "two", digest):
        manifest = await client.fetch_manifest(repository, ref)
        assert manifest is not None and manifest.digest == digest
        assert manifest.artifact_type == ARTIFACT_TYPE
        assert manifest.annotations["org.transluce.fxtr.job.id"] == "j"
    assert manifest is not None
    ((layer_digest, media_type, size),) = manifest.layers
    assert media_type == LAYER_MEDIA_TYPE
    assert size == len(b"a record file")
    out = tmp_path / "out.car"
    await client.fetch_layer(repository, layer_digest, out)
    assert out.read_bytes() == b"a record file"
    assert await client.fetch_manifest(repository, "missing") is None


async def test_a_job_moves_between_projects_through_the_registry(
    database: Database,
    elsewhere: Database,  # noqa: F811 (the fixture of test_project_remotes_oci)
    client: OrasRegistryClient,
    repository: str,
) -> None:
    async with a_client(database, double, doubled) as here:
        remote = oci_remote(database, client, FakeEnvironment(), repository)
        first = (await here.run(doubled, {"x": Array.scalar(1, int)})).job_id
        # The same input: the second job takes the first one's step from the cache.
        second = (await here.run(doubled, {"x": Array.scalar(1, int)})).job_id
        (default,) = (await remote.push(here, first)).tags
        await remote.push(here, first, tags=["latest"])
        moved = await remote.push(here, second, tags=["latest"])
    async with a_client(elsewhere, double, doubled) as there:
        remote = oci_remote(elsewhere, client, FakeEnvironment(), repository)
        assert await remote.pull(there, default) == first
        assert await remote.pull(there, "latest") == second
        assert await remote.pull(there, "@" + moved.version, update=True) == second
        assert await there.jobs.result(second) == Array.scalar(2, int)


async def test_a_pull_refuses_an_artifact_fxtr_did_not_push(
    database: Database, client: OrasRegistryClient, repository: str, tmp_path: Path
) -> None:
    layer = tmp_path / "image.tar"
    layer.write_bytes(b"not a record")
    await client.push_artifact(
        repository,
        layer=layer,
        layer_media_type="application/vnd.oci.image.layer.v1.tar",
        artifact_type="application/vnd.example.other",
        annotations={},
        tags=["image"],
    )
    async with a_client(database, double, doubled) as here:
        remote = oci_remote(database, client, FakeEnvironment(), repository)
        with pytest.raises(RemoteError, match="not a job pushed by fxtr"):
            await remote.pull(here, "image")
