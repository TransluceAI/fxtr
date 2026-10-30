"""Host-death self-teardown E2E: when the owning process dies, the podman
builder + sidecar containers tear *themselves* down (podman
`--health-on-failure=stop` + `--rm`) instead of leaking as running containers.

See `plans/stage2/2p4_persistent_spawner/plan2_host_death_teardown.md`. The
builder self-stops when its health check can't reach the host liveness beacon
(the beacon socket dies with the process); the sidecar self-stops when the
host-liveness `flock` is released (the ssh session's `cat` gets EOF once the
host's stdin pipe closes). Empty pods/volumes are left behind by design (cheap).

Podman-only (the sidecar/beacon are podman-backend machinery); skips on the host
nix backend. Slow (a real nix container build) — hence the long deadline.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from fxtr_nix_runner import load_config
from fxtr_workdir_harness import prepare_workdir_repo

pytestmark = pytest.mark.nix_runtime

_CONTAINER_GREETER_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "container_greeter"
_CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"

# Container-name substrings for the long-lived boxes that must self-teardown.
_LEAK_PREFIXES = ("fxtr-nix-builder", "fxtr-nix-deliverer", "fxtr-spawn-")

# Runs in a *separate* process we SIGKILL to simulate the owner dying.
_SPAWN_AND_BLOCK = """
import sys, anyio
from pathlib import Path
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_sandbox_types import NixContainerServiceDefn
from fxtr_sandboxing.spawner import create_sandbox_environment

async def main():
    repo, commit, cache = sys.argv[1], sys.argv[2], sys.argv[3]
    async with FxtrScope():
        async with (
            create_sandbox_environment(cache_dir=Path(cache), git_repo_path=Path(repo)) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()
            defn = NixContainerServiceDefn(
                git_commit=commit, flake_attr="fxtr.services.greeter"
            ).store()
            await spawner.spawn(defn)
            print("SPAWNED", flush=True)
            await anyio.sleep_forever()

anyio.run(main)
"""


def _running_leak_containers() -> list[str]:
    out = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}"], capture_output=True, text=True
    ).stdout
    return [n for n in out.split() if any(p in n for p in _LEAK_PREFIXES)]


def _sweep_debris() -> None:
    """Best-effort cleanup so a failure doesn't leave the machine dirty."""
    for name in _running_leak_containers():
        subprocess.run(["podman", "rm", "-f", "-t", "0", name], capture_output=True)
    pods = subprocess.run(
        ["podman", "pod", "ls", "--filter", "label=fxtr-spawn", "-q"],
        capture_output=True,
        text=True,
    ).stdout.split()
    for pod in pods:
        subprocess.run(["podman", "pod", "rm", "-f", "-t", "0", pod], capture_output=True)


@pytest.mark.deadline(seconds=900)
def test_host_death_self_teardown(tmp_path: Path) -> None:
    if load_config().nix_backend != "podman":
        pytest.skip("host-death self-teardown is a podman-backend feature")

    prepared = anyio.run(prepare_workdir_repo, _CONTAINER_GREETER_TEMPLATE, tmp_path / "wd")

    # Spawn in a child process (sys.executable = this venv's python, so a plain
    # SIGKILL kills the actual host process — no `uv run` wrapper in between).
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SPAWN_AND_BLOCK,
            str(prepared.path),
            prepared.head_commit,
            str(_CACHE_DIR),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for SPAWNED (a cold build can take minutes; the per-test deadline
        # bounds it). readline blocks; the SIGALRM deadline interrupts a hang.
        assert proc.stdout is not None
        deadline = time.monotonic() + 850
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise AssertionError(
                    f"spawn child exited before SPAWNED:\n{(proc.stderr.read() if proc.stderr else '')}"
                )
            if "SPAWNED" in line:
                break
        else:
            raise AssertionError("timed out waiting for SPAWNED")

        running = _running_leak_containers()
        assert any("fxtr-nix-builder" in n for n in running), f"no builder running: {running}"

        # Simulate host death: SIGKILL the owner. Its beacon socket + ssh stdin
        # pipe close, and the containers must self-teardown.
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)

        # Poll for self-teardown. builder ~10-15s (beacon), sidecar ~15-20s
        # (flock health after its start period) — allow generous margin.
        teardown_deadline = time.monotonic() + 90
        while time.monotonic() < teardown_deadline:
            if not _running_leak_containers():
                break
            time.sleep(3)
        leftover = _running_leak_containers()
        assert (
            not leftover
        ), f"containers still running {int(time.monotonic() - (teardown_deadline - 90))}s after host death: {leftover}"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
        _sweep_debris()
