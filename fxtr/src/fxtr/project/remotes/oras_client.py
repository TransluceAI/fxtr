"""The registry client of the ``oci`` extra, over oras-py.

:class:`OrasRegistryClient` implements
:class:`~fxtr.project.remotes.oci.RegistryClient` with the lower-level API of oras-py
(``oras.provider.Registry``). This module builds the manifest itself,
because the high-level push of oras-py cannot set ``artifactType``. oras-py does the token flow of
registries such as GHCR. Its calls are synchronous, so this client runs them in a worker thread.

Credentials come from the files that ``docker login``, ``podman login``, and ``oras login`` write
(:func:`auth_files`). oras-py reads them, with the credential helpers that Docker's file names. If
the remote names an ``auth_file``, the client reads only that file.

This module imports oras-py at module level. Only
:func:`fxtr.project.remotes.oci.build` imports this module, when it builds a remote.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio.to_thread
import oras.container
import oras.provider
import requests

from fxtr.project.job_record import MAX_RECORD_FILE_BYTES
from fxtr.project.remotes import RemoteError
from fxtr.project.remotes.oci import EMPTY_CONFIG_MEDIA_TYPE, Manifest, RegistryClient

MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
"""The media type of the manifests that this client writes and accepts."""

_EMPTY_CONFIG = b"{}"


class OrasRegistryClient(RegistryClient):
    """A client for an OCI registry, over oras-py.

    Args:
        insecure: If true, talk to the registry over plain HTTP, not HTTPS. Use it only for a
            local registry.
        auth_file: A credential file in the format of Docker's ``config.json``. If you give it,
            the client reads credentials only from this file, and only its ``auths`` entries
            (not credential helpers). If not, the client reads the files of :func:`auth_files`.
    """

    def __init__(self, *, insecure: bool = False, auth_file: Path | None = None) -> None:
        self._registry = oras.provider.Registry(insecure=insecure)
        self._auth_file = auth_file

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
        return await self._run(
            self._push, repository, layer, layer_media_type, artifact_type, annotations, tags
        )

    async def fetch_manifest(self, repository: str, ref: str) -> Manifest | None:
        return await self._run(self._fetch_manifest, repository, ref)

    async def fetch_layer(self, repository: str, digest: str, path: Path) -> None:
        await self._run(self._fetch_layer, repository, digest, path)

    # ---- the synchronous calls, run in a worker thread ----

    def _push(
        self,
        repository: str,
        layer: Path,
        layer_media_type: str,
        artifact_type: str,
        annotations: Mapping[str, str],
        tags: Sequence[str],
    ) -> str:
        first = self._container(repository, tags[0])
        self._log_in(first)
        layer_descriptor = _descriptor(layer.read_bytes(), layer_media_type)
        config_descriptor = _descriptor(_EMPTY_CONFIG, EMPTY_CONFIG_MEDIA_TYPE)
        with tempfile.TemporaryDirectory() as scratch:
            config = Path(scratch) / "config.json"
            config.write_bytes(_EMPTY_CONFIG)
            for path, descriptor in ((config, config_descriptor), (layer, layer_descriptor)):
                response = self._registry.upload_blob(str(path), first, descriptor)
                _check(response, f"upload a blob to {repository}")
        manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST_MEDIA_TYPE,
            "artifactType": artifact_type,
            "config": config_descriptor,
            "layers": [layer_descriptor],
            "annotations": dict(annotations),
        }
        digest = ""
        for tag in tags:
            response = self._registry.upload_manifest(manifest, self._container(repository, tag))
            _check(response, f"set {repository}:{tag}")
            digest = response.headers.get("Docker-Content-Digest", digest)
        if not digest:
            raise RemoteError(f"{repository} did not return the digest of the pushed manifest")
        return digest

    def _fetch_manifest(self, repository: str, ref: str) -> Manifest | None:
        container = self._container(repository, ref)
        self._log_in(container)
        response = self._registry.do_request(
            f"{self._registry.prefix}://{container.manifest_url()}",
            "GET",
            headers={"Accept": MANIFEST_MEDIA_TYPE},
        )
        if response.status_code == 404:
            return None
        _check(response, f"get {container}")
        digest = "sha256:" + hashlib.sha256(response.content).hexdigest()
        promised = response.headers.get("Docker-Content-Digest")
        if (ref.startswith("sha256:") and ref != digest) or (
            promised is not None and promised != digest
        ):
            raise RemoteError(f"The manifest from {repository} does not match its digest")
        body = response.json()
        return Manifest(
            digest=digest,
            artifact_type=body.get("artifactType"),
            layers=tuple(
                (layer["digest"], layer["mediaType"], int(layer.get("size", 0)))
                for layer in body.get("layers", [])
            ),
            annotations=dict(body.get("annotations") or {}),
        )

    def _fetch_layer(self, repository: str, digest: str, path: Path) -> None:
        """Write the blob to ``path``. A blob over :data:`MAX_RECORD_FILE_BYTES` is refused as it
        arrives (the manifest's size is what the registry says, not what it sends)."""
        container = self._container(repository, digest)
        self._log_in(container)
        try:
            with self._registry.get_blob(container, digest, stream=True) as response:
                _check(response, f"get {digest} from {repository}")
                written = 0
                hashed = hashlib.sha256()
                with path.open("wb") as out:
                    for chunk in response.iter_content(chunk_size=1 << 16):
                        written += len(chunk)
                        if written > MAX_RECORD_FILE_BYTES:
                            raise RemoteError(
                                f"{digest} from {repository} is over {MAX_RECORD_FILE_BYTES}"
                                " bytes, the most a job record file may be"
                            )
                        hashed.update(chunk)
                        out.write(chunk)
                if "sha256:" + hashed.hexdigest() != digest:
                    raise RemoteError(f"The layer from {repository} does not match {digest}")
        except (requests.RequestException, ValueError, OSError) as exc:
            raise RemoteError(f"Could not download {digest} from {repository}: {exc}") from exc

    def _log_in(self, container: oras.container.Container) -> None:
        """Load the credentials for the registry of ``container``.

        Raises:
            RemoteError: If the remote's ``auth_file`` does not exist, or has no credentials for
                the registry.
        """
        if self._auth_file is None:
            self._registry.auth.load_configs(container, configs=auth_files())
            return
        try:
            auths = json.loads(self._auth_file.read_text()).get("auths", {})
        except (OSError, ValueError) as exc:
            raise RemoteError(f"Could not read the auth_file {self._auth_file}: {exc}") from exc
        encoded = auths.get(container.registry, {}).get("auth")
        if not encoded:
            raise RemoteError(
                f"The auth_file {self._auth_file} has no credentials for {container.registry}"
            )
        username, _, password = base64.b64decode(encoded).decode().partition(":")
        self._registry.auth.set_basic_auth(username, password)

    @staticmethod
    def _container(repository: str, ref: str) -> oras.container.Container:
        separator = "@" if ref.startswith("sha256:") else ":"
        return oras.container.Container(f"{repository}{separator}{ref}")

    async def _run[T](self, call: Callable[..., T], *args: Any) -> T:
        """Run a synchronous call in a worker thread. Turn a connection failure, and a refusal
        of the credentials, into a :class:`RemoteError`."""
        try:
            return await anyio.to_thread.run_sync(
                functools.partial(call, *args), abandon_on_cancel=True
            )
        except requests.RequestException as exc:
            raise RemoteError(f"The registry is not available: {exc}") from exc
        except ValueError as exc:
            # oras-py raises ValueError when the registry refuses the credentials (or has none).
            raise RemoteError(
                f"The registry refused the request: {exc} Log in to it (for example, with"
                " docker login), with a token that may read or write the repository."
            ) from exc


def auth_files() -> list[str]:
    """Return the credential files that exist, other than Docker's default file.

    The files are, in the order that podman and the other containers tools search them:
    ``$REGISTRY_AUTH_FILE``, ``$XDG_RUNTIME_DIR/containers/auth.json``,
    ``~/.config/containers/auth.json`` (podman's file on macOS), and ``$DOCKER_CONFIG/config.json``.
    oras-py adds ``~/.docker/config.json`` itself. oras-py merges the files in no fixed order, so
    log in to one registry with one tool only.
    """
    candidates = [
        os.environ.get("REGISTRY_AUTH_FILE"),
        os.environ.get("XDG_RUNTIME_DIR")
        and f"{os.environ['XDG_RUNTIME_DIR']}/containers/auth.json",
        os.path.expanduser("~/.config/containers/auth.json"),
        os.environ.get("DOCKER_CONFIG") and f"{os.environ['DOCKER_CONFIG']}/config.json",
    ]
    return [path for path in candidates if path and os.path.isfile(path)]


def _descriptor(data: bytes, media_type: str) -> dict[str, Any]:
    """Return the OCI descriptor of a blob: its media type, digest, and size."""
    return {
        "mediaType": media_type,
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _check(response: requests.Response, what: str) -> None:
    """Refuse a response that is not a success.

    Raises:
        RemoteError: If the status of ``response`` is not 2xx.
    """
    if not 200 <= response.status_code < 300:
        raise RemoteError(f"Could not {what}: HTTP {response.status_code} {response.text[:200]}")


__all__ = ["MANIFEST_MEDIA_TYPE", "OrasRegistryClient", "auth_files"]
