"""The `fxtr_nix runtime` wizard: detection → recommendation → nudges → write.

The system probes are pure-mocked (no real podman/nix); we assert the
recommendation, which nudges fire, the `--detect` JSON shape, and the
`init-podman` argv.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fxtr_nix_runner import _wizard
from fxtr_nix_runner._wizard import (
    AppleContainerStatus,
    Detection,
    HostNixStatus,
    PodmanStatus,
    _init_podman_steps,
    _nudges,
    _recommend,
    detection_to_json,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _podman(
    *, present: bool = True, exists: bool = True, running: bool = True, rosetta: bool | None = True
) -> PodmanStatus:
    return PodmanStatus(
        present=present, machine_exists=exists, machine_running=running, rosetta=rosetta
    )


# --------------------------------------------------------------------------
# Recommendation
# --------------------------------------------------------------------------


def test_recommend_podman_when_machine_usable():
    rec = _recommend(HostNixStatus(False, False), _podman(), AppleContainerStatus(False))
    assert rec.nix_backend == "podman"
    assert rec.container_runtime_cmd == "podman"


def test_recommend_host_when_nix_builds_linux_podman_present():
    # podman present but no machine → host backend, but cmd can still be podman.
    rec = _recommend(
        HostNixStatus(True, True),
        _podman(running=False, exists=False, rosetta=None),
        AppleContainerStatus(False),
    )
    assert rec.nix_backend == "host"
    assert rec.container_runtime_cmd == "podman"


def test_recommend_host_falls_back_to_apple_container_cmd():
    rec = _recommend(
        HostNixStatus(True, True),
        PodmanStatus(False, False, False, None),
        AppleContainerStatus(True),
    )
    assert rec.nix_backend == "host"
    assert rec.container_runtime_cmd == "container"


def test_recommend_none_when_nothing_usable():
    rec = _recommend(
        HostNixStatus(False, False),
        PodmanStatus(False, False, False, None),
        AppleContainerStatus(False),
    )
    assert rec.nix_backend is None
    assert rec.container_runtime_cmd is None


# --------------------------------------------------------------------------
# Nudges
# --------------------------------------------------------------------------


def test_nudge_nix_present_no_linux_builder():
    nudges = _nudges(
        HostNixStatus(True, False),
        PodmanStatus(False, False, False, None),
        AppleContainerStatus(False),
    )
    assert any("linux builder" in n for n in nudges)


def test_nudge_podman_no_machine_points_at_init():
    nudges = _nudges(
        HostNixStatus(False, False),
        _podman(running=False, exists=False, rosetta=None),
        AppleContainerStatus(False),
    )
    assert any("init-podman" in n for n in nudges)


def test_nudge_rosetta_off():
    nudges = _nudges(
        HostNixStatus(False, False),
        _podman(rosetta=False),
        AppleContainerStatus(False),
    )
    assert any("Rosetta is off" in n for n in nudges)


def test_nudge_neither_podman_nor_apple_mentions_manual_and_docker():
    nudges = _nudges(
        HostNixStatus(True, True),
        PodmanStatus(False, False, False, None),
        AppleContainerStatus(False),
    )
    joined = " ".join(nudges)
    assert "container_runtime_cmd" in joined
    assert "docker" in joined and "untested" in joined


def test_nudge_apple_container_fallback_when_no_podman():
    nudges = _nudges(
        HostNixStatus(True, True),
        PodmanStatus(False, False, False, None),
        AppleContainerStatus(True),
    )
    assert any("Apple Container" in n for n in nudges)


# --------------------------------------------------------------------------
# --detect JSON shape
# --------------------------------------------------------------------------


def test_detection_to_json_shape():
    d = Detection(
        host_nix=HostNixStatus(True, False),
        podman=_podman(rosetta=None),
        apple_container=AppleContainerStatus(True),
        recommendation=_recommend(
            HostNixStatus(True, False), _podman(), AppleContainerStatus(True)
        ),
        nudges=("a", "b"),
    )
    j = detection_to_json(d)
    assert set(j) == {"host_nix", "podman", "apple_container", "recommendation", "nudges"}
    assert j["host_nix"] == {"present": True, "builds_linux": False}
    assert set(j["podman"]) == {"present", "machine_exists", "machine_running", "rosetta"}
    assert set(j["recommendation"]) == {"nix_backend", "container_runtime_cmd", "reason"}
    assert j["nudges"] == ["a", "b"]
    # Round-trips through json (no non-serializable values).
    assert json.loads(json.dumps(j)) == j


# --------------------------------------------------------------------------
# detect() composition (probes mocked)
# --------------------------------------------------------------------------


async def test_detect_composes(monkeypatch: pytest.MonkeyPatch):
    async def fake_nix() -> HostNixStatus:
        return HostNixStatus(True, True)

    async def fake_podman() -> PodmanStatus:
        return _podman(running=False, exists=False, rosetta=None)

    async def fake_apple() -> AppleContainerStatus:
        return AppleContainerStatus(False)

    monkeypatch.setattr(_wizard, "_detect_host_nix", fake_nix)
    monkeypatch.setattr(_wizard, "_detect_podman", fake_podman)
    monkeypatch.setattr(_wizard, "_detect_apple_container", fake_apple)

    d = await _wizard.detect()
    assert d.host_nix.builds_linux is True
    assert d.recommendation.nix_backend == "host"
    assert any("init-podman" in n for n in d.nudges)


# --------------------------------------------------------------------------
# init-podman argv
# --------------------------------------------------------------------------


def test_init_podman_steps_with_rosetta():
    steps = _init_podman_steps(rosetta=True)
    argvs = [argv for _, argv in steps]
    assert ["podman", "machine", "init"] in argvs
    assert ["podman", "machine", "start"] in argvs
    assert any("enable-rosetta" in " ".join(argv) for argv in argvs)


def test_init_podman_steps_without_rosetta():
    steps = _init_podman_steps(rosetta=False)
    assert [argv for _, argv in steps] == [
        ["podman", "machine", "init"],
        ["podman", "machine", "start"],
    ]


# --------------------------------------------------------------------------
# render/write config
# --------------------------------------------------------------------------


def test_render_config():
    body = _wizard.render_config("podman", "podman")
    assert 'nix_backend = "podman"' in body
    assert 'container_runtime_cmd = "podman"' in body
    # container_runtime_type is left at its auto default (omitted).
    assert "container_runtime_type" not in body


def test_write_config_round_trips_through_load(tmp_path: Path):
    from fxtr_nix_runner import load_config

    p = tmp_path / "nix_runtime.toml"
    _wizard.write_config(nix_backend="podman", container_runtime_cmd="podman", path=p)
    cfg = load_config(config_path=p, env={})
    assert cfg.nix_backend == "podman"
    assert cfg.container_runtime_cmd == "podman"


# --------------------------------------------------------------------------
# run_setup
# --------------------------------------------------------------------------


async def test_run_setup_detect_only_prints_json_no_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    async def fake_detect() -> Detection:
        hn, p, ac = HostNixStatus(False, False), _podman(), AppleContainerStatus(False)
        return Detection(hn, p, ac, _recommend(hn, p, ac), _nudges(hn, p, ac))

    monkeypatch.setattr(_wizard, "detect", fake_detect)
    lines: list[str] = []
    target = tmp_path / "nix_runtime.toml"
    rc = await _wizard.run_setup(detect_only=True, config_path=target, out=lines.append)
    assert rc == 0
    assert not target.exists()
    parsed = json.loads("\n".join(lines))
    assert parsed["recommendation"]["nix_backend"] == "podman"


async def test_run_setup_writes_recommendation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    async def fake_detect() -> Detection:
        hn, p, ac = HostNixStatus(False, False), _podman(), AppleContainerStatus(False)
        return Detection(hn, p, ac, _recommend(hn, p, ac), ())

    monkeypatch.setattr(_wizard, "detect", fake_detect)
    target = tmp_path / "nix_runtime.toml"
    # Empty input → accept the defaults (recommendation).
    rc = await _wizard.run_setup(
        detect_only=False, config_path=target, input_fn=lambda _: "", out=lambda _: None
    )
    assert rc == 0
    assert target.exists()
    assert 'nix_backend = "podman"' in target.read_text()


async def test_run_setup_no_usable_setup_returns_1_no_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    async def fake_detect() -> Detection:
        hn = HostNixStatus(False, False)
        p = PodmanStatus(False, False, False, None)
        ac = AppleContainerStatus(False)
        return Detection(hn, p, ac, _recommend(hn, p, ac), _nudges(hn, p, ac))

    monkeypatch.setattr(_wizard, "detect", fake_detect)
    target = tmp_path / "nix_runtime.toml"
    rc = await _wizard.run_setup(
        detect_only=False, config_path=target, input_fn=lambda _: "", out=lambda _: None
    )
    assert rc == 1
    assert not target.exists()
