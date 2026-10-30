"""podman-specific machinery for the nix container runner.

Two pieces, all spawner-internal (driven by
:mod:`fxtr_sandboxing.nix_container_runner`). (Host-side API-socket
discovery for image delivery lives in ``fxtr_nix_runner._delivery``, which
owns delivery.)

1. **Remote detection** (:func:`podman_is_remote`) — whether the
   podman daemon runs in a VM / remote host rather than this kernel
   (``podman info .Host.ServiceIsRemote``; covers machine VMs *and*
   ``CONTAINERS_CONNECTION``-style remote setups). Remote podman can't
   reach host UDS listeners through ``-v`` socket-file mounts
   (virtiofs shares the inode, not the listener), so the runner
   switches to the sidecar tunnel below.

2. **The sshd-sidecar tunnel** (:func:`open_sidecar_tunnel`) — the
   remote-podman replacement for direct socket-file mounts, designed +
   validated in the stage-8 spike (see
   ``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage8_podman.md``):
   per spawn, a ``--network none`` pod holds a tiny sidecar container
   and the sandbox, sharing a named volume mounted ``:Z`` (the pod's
   podman-allocated SELinux label + ``:Z`` give each spawn a private
   MCS compartment for free). The host runs one ``ssh`` process whose
   transport is ``ProxyCommand = podman exec -i <sidecar>
   /usr/sbin/sshd -i`` (inetd mode — sshd speaks the protocol on the
   exec's stdio, riding the machine API socket; **nothing listens
   anywhere**, not even on the pod's isolated loopback) and one ``-R``
   per socket forwarding ``<volume>/<name>.sock`` back to the host
   listener. The ssh session's remote command waits for the forwarded
   sockets to exist and then prints ``READY`` — the positive readiness
   signal the runner blocks on before starting the sandbox (with
   ``ExitOnForwardFailure=yes`` so a failed bind kills the session
   rather than hanging the wait). The sidecar's host key is fetched
   via ``podman exec`` and pinned (``StrictHostKeyChecking=yes``);
   keys are baked at image build, so there's no generation race.

The sidecar image is **podman-built from the inline Containerfile**
below (no nix — it's glue, and a podman-native build path is what
``stage9_nix_runner.md`` needs anyway), tagged with a content hash of
the Containerfile so edits invalidate cleanly, and built on demand at
first spawn. Everything created per spawn carries a
``fxtr-spawn=<id>`` label so :func:`clear_spawn_debris` can sweep
orphans after a host crash (normal teardown leaves nothing).
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shlex
import subprocess
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Final

import anyio
import anyio.abc
import anyio.to_thread
from fxtr_nix_runner.perf import perf_span, proc_label

#: Repository part of the sidecar image reference. The tag is a content
#: hash of the Containerfile, so image edits rebuild cleanly and stale
#: tags are prunable alongside the service images.
SIDECAR_IMAGE_REPO: Final = "localhost/fxtr-sshd-sidecar"

#: Label key applied to every per-spawn pod + volume. Normal teardown
#: removes them; `clear_spawn_debris` sweeps anything left by a crash.
SPAWN_LABEL_KEY: Final = "fxtr-spawn"

#: Label key baked into images we *build* (currently just the sidecar;
#: value = the image's role). Labels live in image content, so they
#: survive untagging — which is exactly what name-based pruning can't
#: see: concurrent first-builds of the same tag are nondeterministic
#: (`ssh-keygen -A`), so every build that loses the final `-t` retag
#: race becomes a dangling `<none>` image. `clear_image_cache` sweeps
#: those by this label. (Delivered nix-service images don't need it:
#: their tag is the nix store hash, so racers produce identical
#: refs and nothing is ever orphaned.)
ROLE_LABEL_KEY: Final = "fxtr-role"

#: How long to wait for the tunnel's READY line before declaring the
#: spawn failed. Covers the sidecar's container start + ssh handshake
#: (the image build happens before this, with its own error surface).
TUNNEL_READY_TIMEOUT_S: Final = 60.0

# Host keys are baked at build (`ssh-keygen -A`) — no generation race;
# per-build keys are fine because the host pins the fetched key per
# spawn anyway. `AllowTcpForwarding yes` is required even in inetd
# mode (Alpine's hardened default also denies -R *streamlocal*
# forwards). `StreamLocalBindUnlink yes` + `StreamLocalBindMask 0111`
# eliminate the stale-socket and chmod dances. authorized_keys is
# written from the PUBKEY env at container start; the main process is
# just `sleep infinity` — nothing listens (sshd runs per-connection in
# inetd mode via the host's ProxyCommand).
_SIDECAR_CONTAINERFILE: Final = f"""
FROM docker.io/library/alpine
LABEL {ROLE_LABEL_KEY}="sshd-sidecar"
RUN apk add --no-cache openssh && ssh-keygen -A && \
    sed -i 's/^AllowTcpForwarding no/AllowTcpForwarding yes/' /etc/ssh/sshd_config && \
    printf 'PermitRootLogin prohibit-password\\nStreamLocalBindUnlink yes\\nStreamLocalBindMask 0111\\n' \
      >> /etc/ssh/sshd_config
CMD ["sh", "-c", "mkdir -p /root/.ssh && echo \\"$PUBKEY\\" > /root/.ssh/authorized_keys && exec sleep infinity"]
"""

#: Where the shared volume is mounted inside the *sidecar* (the ssh
#: ``-R`` bind targets live under this). The sandbox mounts the same
#: volume at ``/run/fxtr``, so the guest contract is unchanged.
_SIDECAR_VOLUME_MOUNT: Final = "/shared"

#: Host-liveness lock on the shared volume: the ssh session's remote command
#: holds an exclusive ``flock`` on it, so the lock is released (kernel, even on
#: SIGKILL) the instant the ssh session dies — i.e. when the host process goes
#: away. The sidecar's health check reports unhealthy when the lock is free, and
#: ``--health-on-failure=stop`` + ``--rm`` then tear the sidecar down. This is
#: how a crashed spawner stops leaking a running sidecar without any host-side
#: cleanup (the pod is ``--network none``, so a host TCP beacon isn't reachable
#: from inside it — the ssh session is the only host↔pod channel).
_HOSTLIVE_LOCK: Final = f"{_SIDECAR_VOLUME_MOUNT}/.hostlive"


async def _run_checked(cmd: list[str], *, what: str, input: bytes | None = None) -> str:
    """Run a host command; return stdout, or raise ``RuntimeError``."""
    async with perf_span(proc_label(cmd)):
        result = await anyio.run_process(cmd, check=False, input=input)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"{what} failed (exit {result.returncode}):\n{stderr}")
    return result.stdout.decode("utf-8", "replace")


# Memo for `podman_is_remote` — the answer is a property of the daemon and can't
# change during a process's life, so the per-spawn `podman info` is cached
# (keyed by the runtime cmd). Process-lifetime; never needs invalidation.
_is_remote_cache: dict[str, bool] = {}


async def podman_is_remote(runtime: str) -> bool:
    """Whether the podman daemon runs in a VM / remote host (cached per runtime).

    ``True`` ⇒ host UDS listeners aren't reachable through ``-v``
    socket-file mounts and the sidecar tunnel is required. ``False``
    (native Linux) ⇒ direct mounts work as with any local runtime.
    """
    if runtime not in _is_remote_cache:
        out = await _run_checked(
            [runtime, "info", "--format", "{{.Host.ServiceIsRemote}}"],
            what="podman remote detection (podman info)",
        )
        _is_remote_cache[runtime] = out.strip() == "true"
    return _is_remote_cache[runtime]


# Host-side podman API-socket discovery lives in `fxtr_nix_runner._delivery`
# (`resolve_podman_socket`), which owns image delivery; the run path here only
# needs `podman_is_remote`.


def _sidecar_image_ref() -> str:
    """Content-addressed reference for the sidecar image."""
    digest = hashlib.sha256(_SIDECAR_CONTAINERFILE.encode()).hexdigest()[:16]
    return f"{SIDECAR_IMAGE_REPO}:{digest}"


def _build_lock_path(tag: str) -> Path:
    """Lock file serializing same-host builds of one sidecar image tag.

    Per-user stable dir (the resource being protected — the user's
    image store — is per-user too): ``$XDG_CACHE_HOME`` if set, else
    ``~/.cache``. Keyed by tag so different Containerfile versions
    don't serialize against each other.
    """
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "fxtr_sandboxing"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"sidecar-build-{tag}.lock"


async def _image_exists(runtime: str, image_ref: str) -> bool:
    result = await anyio.run_process([runtime, "image", "inspect", image_ref], check=False)
    return result.returncode == 0


async def ensure_sidecar_image(runtime: str) -> str:
    """Build the sidecar image if its content-addressed tag is missing.

    Same cache model as the service images: the runtime's image store
    is the cache, keyed by the Containerfile's content hash. The first
    build pulls the alpine base (needs network once).

    The inspect+build is serialized across same-host processes by an
    ``flock`` (parallel spawns — e.g. xdist test workers — would
    otherwise all see the tag missing and all build; the builds are
    nondeterministic via ``ssh-keygen -A``, so every retag loser
    survives as a dangling ``<none>`` image). Waiters re-check the
    cache once they hold the lock, so N concurrent cold-store spawns
    do exactly one build. The baked ``fxtr-role`` label remains the
    backstop for any corpses that slip through (see ROLE_LABEL_KEY).
    """
    image_ref = _sidecar_image_ref()
    if await _image_exists(runtime, image_ref):
        return image_ref

    lock_file = open(_build_lock_path(image_ref.rsplit(":", 1)[1]), "w")
    try:
        # Blocking acquire off the event loop; released by close().
        await anyio.to_thread.run_sync(fcntl.flock, lock_file.fileno(), fcntl.LOCK_EX)
        # The winner built while we waited — re-check before building.
        if not await _image_exists(runtime, image_ref):
            await _run_checked(
                [runtime, "build", "-t", image_ref, "-f", "-", "."],
                what="sidecar image build",
                input=_SIDECAR_CONTAINERFILE.encode(),
            )
    finally:
        lock_file.close()
    return image_ref


# Memo for the sidecar host key — baked into the (content-addressed) image, so
# it's the same for every container from a given image ref. Cached per image ref
# to skip a per-spawn `podman exec cat`. Process-lifetime.
_host_key_cache: dict[str, str] = {}


async def _sidecar_host_key(runtime: str, image_ref: str, sidecar_name: str) -> str:
    """The sidecar image's baked ssh host public key (cached per image ref)."""
    if image_ref not in _host_key_cache:
        out = await _run_checked(
            [runtime, "exec", sidecar_name, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"],
            what="sidecar host-key fetch",
        )
        _host_key_cache[image_ref] = out.strip()
    return _host_key_cache[image_ref]


@asynccontextmanager
async def open_sidecar_tunnel(
    runtime: str,
    socket_paths: dict[str, str],
) -> AsyncIterator[list[str]]:
    """Open a per-spawn sidecar pod forwarding host UDSes into a volume.

    ``socket_paths`` maps socket file names (e.g. ``"task.sock"``) to
    host socket paths with live listeners. Yields the extra ``run``
    args for the sandbox container (``--pod`` membership + the shared
    volume mounted at ``/run/fxtr``, where the guest finds the
    forwarded sockets under the names given). On exit, kills the
    tunnel and removes the pod + volume.

    Note the sandbox must NOT pass ``--network`` itself: network
    config belongs to the pod (created ``--network none``).
    """
    spawn_id = uuid.uuid4().hex[:12]
    pod_name = f"fxtr-spawn-{spawn_id}"
    sidecar_name = f"{pod_name}-sidecar"
    volume_name = f"{pod_name}-vol"
    label = f"{SPAWN_LABEL_KEY}={spawn_id}"

    async with perf_span("sidecar.ensure_image"):
        image_ref = await ensure_sidecar_image(runtime)

    key_dir = Path(tempfile.mkdtemp(prefix="fxtr_sidecar_"))
    ssh_proc: anyio.abc.Process | None = None
    created_volume = False
    created_pod = False
    try:
        # Per-spawn throwaway keypair; the private key never leaves the
        # host tempdir, the sidecar gets only the public half.
        await _run_checked(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_dir / "id")],
            what="per-spawn ssh keypair generation",
        )

        await _run_checked(
            [runtime, "volume", "create", "--label", label, volume_name],
            what="sidecar volume create",
        )
        created_volume = True
        await _run_checked(
            [
                runtime,
                "pod",
                "create",
                "--name",
                pod_name,
                "--network",
                "none",
                "--label",
                label,
            ],
            what="sidecar pod create",
        )
        created_pod = True
        await _run_checked(
            [
                runtime,
                "run",
                "-d",
                "--name",
                sidecar_name,
                "--pod",
                pod_name,
                "--label",
                label,
                # Kill-on-stop: the sidecar's PID 1 is `sleep infinity` (ignores
                # SIGTERM), so without this `pod rm -f` (normal teardown *and*
                # `clear_spawn_debris`) would wait the 10s stop timeout before
                # SIGKILL. It's a stateless ssh relay — nothing to flush.
                "--stop-timeout",
                "0",
                # Self-teardown on host death: unhealthy once the host-liveness
                # flock is free (the ssh session — hence the host — is gone);
                # --health-on-failure=stop + --rm then remove the sidecar. The
                # start period covers the tunnel bring-up (the lock is taken by
                # the remote command below, after this container starts).
                "--rm",
                "--health-cmd",
                f"flock -n {_HOSTLIVE_LOCK} -c true && exit 1 || exit 0",
                "--health-interval",
                "5s",
                "--health-timeout",
                "2s",
                "--health-retries",
                "2",
                # Start period covers tunnel bring-up (container start → ssh
                # connect → READY → flock held); that's sub-second in practice,
                # so 10s is generous margin while keeping teardown latency low.
                "--health-start-period",
                "10s",
                "--health-on-failure",
                "stop",
                "-v",
                f"{volume_name}:{_SIDECAR_VOLUME_MOUNT}:Z",
                "-e",
                f"PUBKEY={(key_dir / 'id.pub').read_text().strip()}",
                image_ref,
            ],
            what="sidecar container start",
        )

        # Pin the sidecar's host key. It's baked into the (content-addressed)
        # sidecar image, so it's identical for every container from `image_ref`
        # — fetch it once per image and cache, skipping a per-spawn `podman exec`.
        hostkey = await _sidecar_host_key(runtime, image_ref, sidecar_name)
        (key_dir / "known_hosts").write_text(f"sidecar {hostkey}\n")

        # One -R per socket; remote bind targets live on the volume.
        vol_socks = [f"{_SIDECAR_VOLUME_MOUNT}/{name}" for name in socket_paths]
        forwards: list[str] = []
        for name, host_path in socket_paths.items():
            forwards += ["-R", f"{_SIDECAR_VOLUME_MOUNT}/{name}:{host_path}"]

        # Readiness: the session's remote command gates READY on every
        # forwarded socket existing, then holds the session open **holding the
        # host-liveness flock over a `cat` on the ssh-forwarded stdin**. The
        # `cat` is the dead-man's switch: when the host process dies, its end of
        # the ssh stdin pipe closes, ssh forwards EOF, `cat` exits, and the
        # kernel releases the flock — which the sidecar's health check turns into
        # a self-teardown (see _HOSTLIVE_LOCK). (A held flock alone is *not*
        # enough: the ssh client can orphan-survive the host's death on macOS,
        # keeping the session — and the lock — alive; the stdin EOF is what
        # actually crosses the boundary, per the stdin-EOF spike.)
        wait_clauses = " || ".join(f"[ ! -S {s} ]" for s in vol_socks)
        remote_cmd = (
            f"while {wait_clauses}; do sleep 0.05; done; echo READY; "
            f"exec flock -x {_HOSTLIVE_LOCK} -c 'cat >/dev/null'"
        )

        proxy = shlex.join([runtime, "exec", "-i", sidecar_name, "/usr/sbin/sshd", "-i"])
        ssh_cmd = [
            "ssh",
            *forwards,
            "-i",
            str(key_dir / "id"),
            "-o",
            f"ProxyCommand={proxy}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={key_dir / 'known_hosts'}",
            "-o",
            "ExitOnForwardFailure=yes",
            "root@sidecar",
            remote_cmd,
        ]
        # stdin piped and held open for the tunnel's life: it's the dead-man's
        # switch that the remote `cat` reads (see remote_cmd). When this process
        # dies the pipe closes → EOF → the sidecar self-tears-down. stdout piped
        # for the READY read; stderr inherited so ssh failures surface in logs.
        ssh_proc = await anyio.open_process(
            ssh_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None
        )

        assert ssh_proc.stdout is not None
        buf = b""
        try:
            async with perf_span("sidecar.ssh_ready"):
                with anyio.fail_after(TUNNEL_READY_TIMEOUT_S):
                    while b"READY" not in buf:
                        buf += await ssh_proc.stdout.receive(64)
        except anyio.EndOfStream:
            rc = await ssh_proc.wait()
            raise RuntimeError(
                f"sidecar tunnel: ssh exited (code {rc}) before READY — "
                "check the ssh stderr above for the underlying error."
            ) from None
        except TimeoutError:
            raise RuntimeError(
                f"sidecar tunnel: no READY within {TUNNEL_READY_TIMEOUT_S}s "
                "(forwarded sockets never appeared on the shared volume)."
            ) from None

        yield ["--pod", pod_name, "-v", f"{volume_name}:/run/fxtr:Z"]
    finally:
        async with perf_span("sidecar.teardown"):
            if ssh_proc is not None:
                ssh_proc.terminate()
                await ssh_proc.wait()
            if created_pod:
                # The sidecar is created with `--stop-timeout=0`, so `pod rm -f`
                # SIGKILLs it immediately instead of burning the 10s stop timeout
                # on `sleep infinity` (which ignores SIGTERM). Nothing in the pod
                # holds unflushed state.
                await anyio.run_process([runtime, "pod", "rm", "-f", pod_name], check=False)
            if created_volume:
                await anyio.run_process([runtime, "volume", "rm", volume_name], check=False)

        import shutil

        shutil.rmtree(key_dir, ignore_errors=True)


async def clear_spawn_debris(runtime: str) -> list[str]:
    """Remove orphaned per-spawn pods + volumes (crash recovery).

    Sweeps everything carrying the ``fxtr-spawn`` label. Normal spawn
    teardown leaves nothing for this to find. Returns the removed
    object names/ids for reporting.
    """
    removed: list[str] = []
    pods = await _run_checked(
        [runtime, "pod", "ls", "--filter", f"label={SPAWN_LABEL_KEY}", "-q"],
        what="orphaned-pod listing",
    )
    for pod in pods.split():
        await _run_checked([runtime, "pod", "rm", "-f", pod], what=f"pod rm {pod}")
        removed.append(pod)
    volumes = await _run_checked(
        [runtime, "volume", "ls", "--filter", f"label={SPAWN_LABEL_KEY}", "-q"],
        what="orphaned-volume listing",
    )
    for volume in volumes.split():
        await _run_checked([runtime, "volume", "rm", volume], what=f"volume rm {volume}")
        removed.append(volume)
    return removed
