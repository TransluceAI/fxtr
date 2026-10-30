"""The ``fxtr_nix runtime`` setup wizard: detect what the machine has, explain
the security/convenience tradeoff, nudge through any gaps, and write
``nix_runtime.toml`` — so the backend/runtime choice is explicit + informed,
made once.

Design: ``stage9post1_backend_config_wizard.md``. The detection probes are the
only thing that touches the real system in ``setup``/``--detect`` (they're
mocked in unit tests); ``init-podman`` is the one subcommand that *changes* the
system, kept separate so ``setup`` stays detect-and-write only.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import anyio
from fxtr_nix_runner._config import Backend, default_config_path

# A minimal ``x86_64-linux`` derivation — no nixpkgs/channel, so the only thing
# it exercises is "is there a working linux builder" (succeeds locally on
# linux, via a remote builder on macOS, fails when none is configured).
_LINUX_PROBE_EXPR = (
    'derivation { name = "fxtr-linux-probe"; system = "x86_64-linux"; '
    'builder = "/bin/sh"; args = [ "-c" "echo ok > $out" ]; }'
)

# The machine-VM marker that enables amd64-under-Rosetta (see stage 8).
_ROSETTA_MARKER = "/etc/containers/enable-rosetta"

TRADEOFF_BLURB = """\
fxtr builds are x86_64-linux. Pick how this machine runs them:

  • podman  — self-contained (`podman machine init`); no host nix/skopeo, no
              linux-builder setup. Builds at the moderate sandbox rung
              (per-build network denial + read-only store, virby first-line
              parity; second line is the container boundary). Same on macOS and
              Linux. RECOMMENDED unless you already run a linux-capable nix.
  • host    — convenient if you already run nix, but needs a configured
              x86_64-linux builder (a remote builder — virby or otherwise;
              macOS can't build linux locally). Sandbox strength is whatever
              your nix is configured for.
"""


@dataclass(frozen=True)
class HostNixStatus:
    """``nix`` on PATH, and whether it can actually build ``x86_64-linux``
    (distinguishes "nix present" from "nix present + linux builder")."""

    present: bool
    builds_linux: bool


@dataclass(frozen=True)
class PodmanStatus:
    """podman CLI + machine state. ``rosetta`` is ``None`` when undetermined
    (machine not running, so the in-VM marker can't be checked)."""

    present: bool
    machine_exists: bool
    machine_running: bool
    rosetta: bool | None

    @property
    def usable(self) -> bool:
        """Ready to run builds now (CLI present + machine up)."""
        return self.present and self.machine_running


@dataclass(frozen=True)
class AppleContainerStatus:
    present: bool


@dataclass(frozen=True)
class Recommendation:
    """What ``setup`` proposes writing. ``nix_backend``/``container_runtime_cmd``
    are ``None`` when no usable setup was found (the nudges say what to fix)."""

    nix_backend: Backend | None
    container_runtime_cmd: str | None
    reason: str


@dataclass(frozen=True)
class Detection:
    host_nix: HostNixStatus
    podman: PodmanStatus
    apple_container: AppleContainerStatus
    recommendation: Recommendation
    nudges: tuple[str, ...]


async def _run(argv: list[str], *, timeout: float = 120.0) -> tuple[int, str, str]:
    try:
        with anyio.fail_after(timeout):
            result = await anyio.run_process(argv, check=False)
    except (TimeoutError, OSError):
        return 1, "", "probe failed"
    return result.returncode, result.stdout.decode(), result.stderr.decode()


async def _detect_host_nix() -> HostNixStatus:
    if shutil.which("nix") is None:
        return HostNixStatus(present=False, builds_linux=False)
    rc, _, _ = await _run(["nix", "build", "--no-link", "--impure", "--expr", _LINUX_PROBE_EXPR])
    return HostNixStatus(present=True, builds_linux=rc == 0)


async def _detect_podman() -> PodmanStatus:
    if shutil.which("podman") is None:
        return PodmanStatus(
            present=False, machine_exists=False, machine_running=False, rosetta=None
        )
    rc, _, _ = await _run(["podman", "machine", "inspect"], timeout=30)
    machine_exists = rc == 0
    machine_running = False
    rosetta: bool | None = None
    if machine_exists:
        rc2, out, _ = await _run(
            ["podman", "machine", "inspect", "--format", "{{.State}}"], timeout=30
        )
        machine_running = rc2 == 0 and out.strip() == "running"
        if machine_running:
            rc3, _, _ = await _run(
                ["podman", "machine", "ssh", f"test -f {_ROSETTA_MARKER}"], timeout=30
            )
            rosetta = rc3 == 0
    return PodmanStatus(
        present=True,
        machine_exists=machine_exists,
        machine_running=machine_running,
        rosetta=rosetta,
    )


async def _detect_apple_container() -> AppleContainerStatus:
    return AppleContainerStatus(present=shutil.which("container") is not None)


def _recommend(
    host_nix: HostNixStatus, podman: PodmanStatus, apple: AppleContainerStatus
) -> Recommendation:
    if podman.usable:
        return Recommendation(
            nix_backend="podman",
            container_runtime_cmd="podman",
            reason="podman machine is up — self-contained (no host nix or linux builder needed).",
        )
    if host_nix.builds_linux:
        cmd = "podman" if podman.present else ("container" if apple.present else None)
        return Recommendation(
            nix_backend="host",
            container_runtime_cmd=cmd,
            reason="host nix can build x86_64-linux (a linux builder is configured).",
        )
    return Recommendation(
        nix_backend=None,
        container_runtime_cmd=None,
        reason="no usable setup found — install/start podman (recommended), "
        "or configure an x86_64-linux builder for host nix.",
    )


def _nudges(
    host_nix: HostNixStatus, podman: PodmanStatus, apple: AppleContainerStatus
) -> tuple[str, ...]:
    out: list[str] = []
    if host_nix.present and not host_nix.builds_linux:
        out.append(
            "nix is installed but can't build x86_64-linux: the host backend needs a "
            "configured linux builder (a remote builder — e.g. virby)."
        )
    if podman.present and not podman.machine_exists:
        out.append(
            "podman is installed but has no machine: run `fxtr_nix runtime init-podman` "
            "(wraps `podman machine init` the recommended way, with the Rosetta choice)."
        )
    if podman.machine_exists and not podman.machine_running:
        out.append("podman machine exists but isn't running: `podman machine start`.")
    if podman.machine_running and podman.rosetta is False:
        out.append(
            "podman machine is up but Rosetta is off: re-create with Rosetta "
            "(`fxtr_nix runtime init-podman`) for near-native amd64 builds, or accept "
            "slower emulation."
        )
    if not podman.present:
        if apple.present:
            out.append(
                "no podman: Apple Container (`container`) is present and works as the "
                "runtime fallback for the host backend."
            )
        else:
            out.append(
                "neither podman nor Apple Container found: install podman (recommended), "
                "or set `container_runtime_cmd` manually in nix_runtime.toml to any "
                "docker-CLI-compatible runtime — plain `docker` may work but is untested."
            )
    return tuple(out)


async def detect() -> Detection:
    """Probe the machine and compute the recommendation + nudges."""
    host_nix = await _detect_host_nix()
    podman = await _detect_podman()
    apple = await _detect_apple_container()
    return Detection(
        host_nix=host_nix,
        podman=podman,
        apple_container=apple,
        recommendation=_recommend(host_nix, podman, apple),
        nudges=_nudges(host_nix, podman, apple),
    )


def detection_to_json(d: Detection) -> dict:
    """The stable, documented ``--detect`` JSON shape."""
    return {
        "host_nix": {"present": d.host_nix.present, "builds_linux": d.host_nix.builds_linux},
        "podman": {
            "present": d.podman.present,
            "machine_exists": d.podman.machine_exists,
            "machine_running": d.podman.machine_running,
            "rosetta": d.podman.rosetta,
        },
        "apple_container": {"present": d.apple_container.present},
        "recommendation": {
            "nix_backend": d.recommendation.nix_backend,
            "container_runtime_cmd": d.recommendation.container_runtime_cmd,
            "reason": d.recommendation.reason,
        },
        "nudges": list(d.nudges),
    }


def render_config(nix_backend: Backend, container_runtime_cmd: str) -> str:
    """The ``nix_runtime.toml`` body for a chosen backend + runtime cmd
    (``container_runtime_type`` is left at its ``auto`` default, omitted)."""
    return (
        "# Written by `fxtr_nix runtime setup`. Edit freely.\n"
        f'nix_backend = "{nix_backend}"\n'
        f'container_runtime_cmd = "{container_runtime_cmd}"\n'
    )


def write_config(
    *, nix_backend: Backend, container_runtime_cmd: str, path: Path | None = None
) -> Path:
    """Write ``nix_runtime.toml`` (default location) and return the path."""
    target = path if path is not None else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(nix_backend, container_runtime_cmd))
    return target


def _format_detection(d: Detection) -> str:
    """Human-readable detection summary (the top of `setup`'s output)."""
    n = d.host_nix
    p = d.podman
    lines = [
        "Detected:",
        f"  host nix:        {'present' if n.present else 'absent'}"
        + (f", builds x86_64-linux: {n.builds_linux}" if n.present else ""),
        f"  podman:          {'present' if p.present else 'absent'}"
        + (
            f", machine: {'running' if p.machine_running else ('stopped' if p.machine_exists else 'none')}"
            f", rosetta: {p.rosetta}"
            if p.present
            else ""
        ),
        f"  apple container: {'present' if d.apple_container.present else 'absent'}",
    ]
    return "\n".join(lines)


async def run_setup(
    *,
    detect_only: bool,
    config_path: Path | None = None,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    """``fxtr_nix runtime setup`` (and ``--detect``).

    ``--detect`` prints the detection JSON and stops (no prompts, no writes).
    Otherwise: print the tradeoff + detection + nudges + recommendation, prompt
    for the backend + runtime (defaulting to the recommendation), and write
    ``nix_runtime.toml``. Detect-and-write only — it never runs system setup
    (points at ``init-podman`` instead). Returns a process exit code.
    """
    d = await detect()

    if detect_only:
        out(json.dumps(detection_to_json(d), indent=2))
        return 0

    out(TRADEOFF_BLURB)
    out(_format_detection(d))
    if d.nudges:
        out("\nNudges:")
        for nudge in d.nudges:
            out(f"  - {nudge}")
    out(f"\nRecommendation: {d.recommendation.reason}")

    rec = d.recommendation
    if rec.nix_backend is None:
        out(
            "\nNo usable setup to write. Follow a nudge above (e.g. "
            "`fxtr_nix runtime init-podman`), then re-run `fxtr_nix runtime setup`."
        )
        return 1

    backend = _prompt_backend(input_fn, out, rec.nix_backend)
    runtime_cmd = _prompt_runtime_cmd(input_fn, out, rec.container_runtime_cmd, backend)
    target = write_config(nix_backend=backend, container_runtime_cmd=runtime_cmd, path=config_path)
    out(f"\nWrote {target}: nix_backend={backend!r}, container_runtime_cmd={runtime_cmd!r}.")
    return 0


def _prompt_backend(
    input_fn: Callable[[str], str], out: Callable[[str], None], default: Backend
) -> Backend:
    while True:
        ans = input_fn(f"nix backend [host|podman] ({default}): ").strip()
        if not ans:
            return default
        if ans in ("host", "podman"):
            return ans  # type: ignore[return-value]
        out("  please enter 'host' or 'podman'.")


def _prompt_runtime_cmd(
    input_fn: Callable[[str], str],
    out: Callable[[str], None],
    default: str | None,
    backend: Backend,
) -> str:
    # The podman nix backend requires a podman runtime.
    suggested = "podman" if backend == "podman" else (default or "podman")
    while True:
        ans = input_fn(f"container runtime command ({suggested}): ").strip()
        cmd = ans or suggested
        if cmd:
            return cmd
        out("  please enter a runtime command (e.g. podman, container).")


async def run_init_podman(
    *,
    rosetta: bool,
    out: Callable[[str], None] = print,
) -> int:
    """``fxtr_nix runtime init-podman`` — automate ``podman machine init``.

    Rosetta is a choice, not forced: ``rosetta`` (default ``True``, matching
    stage 8's recommended applehv-under-Rosetta config) decides whether the
    in-VM marker is flipped on for near-native amd64 builds. The body is a thin
    wrapper over ``podman machine init`` + ``start`` (+ the Rosetta ssh step);
    re-run ``setup`` afterward.
    """
    if shutil.which("podman") is None:
        out(
            "podman is not installed. Install it first, then re-run `fxtr_nix runtime init-podman`."
        )
        return 1

    for step, argv in _init_podman_steps(rosetta):
        out(f"$ {' '.join(argv)}")
        rc, stdout, stderr = await _run(argv, timeout=600)
        if stdout.strip():
            out(stdout.rstrip())
        if rc != 0:
            out(f"{step} failed (rc={rc}):\n{stderr}")
            return rc
    out("\npodman machine is ready. Re-run `fxtr_nix runtime setup` to write the config.")
    return 0


def _init_podman_steps(rosetta: bool) -> list[tuple[str, list[str]]]:
    """The ``init-podman`` command sequence (pure → testable argv)."""
    steps: list[tuple[str, list[str]]] = [
        ("machine init", ["podman", "machine", "init"]),
        ("machine start", ["podman", "machine", "start"]),
    ]
    if rosetta:
        steps.append(
            (
                "enable rosetta",
                [
                    "podman",
                    "machine",
                    "ssh",
                    f"sudo touch {_ROSETTA_MARKER} && "
                    "sudo systemctl start rosetta-activation.service",
                ],
            )
        )
    return steps
