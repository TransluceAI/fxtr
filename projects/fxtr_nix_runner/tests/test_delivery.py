"""Unit tests for the store-side delivery helpers (no container runtime).

Ref qualification/sanitization is pure; `host_load_into_runtime`'s two
branches are exercised with the subprocess layer mocked. The live podman
deliverer path is covered in `test_podman_backend.py`.
"""

from __future__ import annotations

from typing import Sequence

import pytest
from fxtr_nix_runner import ContainerRuntime, _delivery

pytestmark = pytest.mark.anyio

_IMAGE_JSON = "/nix/store/abc123hash-image-fxtr-test.json"
_SKOPEO = "/nix/store/skopeohash-skopeo/bin/skopeo"

# Resolved runtimes (the `auto` inference is unit-tested in test_config.py).
_PODMAN = ContainerRuntime(cmd="podman", type="podman")
_PODMAN_ABS = ContainerRuntime(cmd="/opt/podman/bin/podman", type="podman")
_CONTAINER = ContainerRuntime(cmd="container", type="docker_compatible")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_image_ref_qualification() -> None:
    name = "abc123hash-image-fxtr-test.json"
    assert _delivery.image_ref_for(name, _PODMAN) == (
        "localhost/fxtr-nix-service:abc123hash-image-fxtr-test"
    )
    # A podman-type runtime at an absolute path still gets the localhost prefix.
    assert _delivery.image_ref_for(name, _PODMAN_ABS) == (
        "localhost/fxtr-nix-service:abc123hash-image-fxtr-test"
    )
    # docker_compatible runtimes keep the bare ref.
    assert _delivery.image_ref_for(name, _CONTAINER) == (
        "fxtr-nix-service:abc123hash-image-fxtr-test"
    )


def test_image_ref_tag_sanitization() -> None:
    # Chars outside the docker tag alphabet (`+`) map to `_`; overlong names
    # cap at 128 chars with the content-addressing hash prefix intact.
    plus = "abc123hash-image-libstdc++-svc.json"
    assert _delivery.image_ref_for(plus, _PODMAN).endswith(":abc123hash-image-libstdc__-svc")
    long = f"abc123hash-image-{'x' * 200}.json"
    tag = _delivery.image_ref_for(long, _PODMAN).rsplit(":", 1)[1]
    assert len(tag) == 128 and tag.startswith("abc123hash-image-")


def test_platform_from_manifest() -> None:
    assert _delivery.platform_from_manifest('{"arch": "amd64"}') == "linux/amd64"
    assert _delivery.platform_from_manifest('{"arch": "arm64"}') == "linux/arm64"


class _Recorder:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    async def __call__(self, argv: Sequence[str], *, what: str) -> str:
        self.commands.append(list(argv))
        return ""


async def test_host_load_podman_is_tarless(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(_delivery, "_run_checked", rec)

    async def fake_socket(cmd: str) -> str:
        return "unix:///tmp/fake-podman-api.sock"

    monkeypatch.setattr(_delivery, "resolve_podman_socket", fake_socket)

    ref = "localhost/fxtr-nix-service:abc123hash-image-fxtr-test"
    await _delivery.host_load_into_runtime(_SKOPEO, _IMAGE_JSON, ref, _PODMAN)

    assert rec.commands == [
        [
            _SKOPEO,
            "--insecure-policy",
            "copy",
            "--dest-daemon-host",
            "unix:///tmp/fake-podman-api.sock",
            f"nix:{_IMAGE_JSON}",
            f"docker-daemon:{ref}",
        ]
    ]


async def test_host_load_other_runtime_uses_tar(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(_delivery, "_run_checked", rec)

    ref = "fxtr-nix-service:abc123hash-image-fxtr-test"
    await _delivery.host_load_into_runtime(_SKOPEO, _IMAGE_JSON, ref, _CONTAINER)

    [copy_cmd, load_cmd] = rec.commands
    assert copy_cmd[0] == _SKOPEO
    assert "--dest-daemon-host" not in copy_cmd
    assert copy_cmd[3] == f"nix:{_IMAGE_JSON}"
    assert copy_cmd[4].startswith("oci-archive:") and copy_cmd[4].endswith(f":{ref}")
    assert load_cmd[:3] == ["container", "image", "load"]
