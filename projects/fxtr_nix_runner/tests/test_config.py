"""Config resolution: per-field env precedence, full-file override, the
``require_*`` accessors (no implicit default), and the ContainerRuntime model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fxtr_nix_runner import (
    ConfigError,
    ContainerRuntime,
    PodmanConfig,
    load_config,
    resolve_container_runtime,
)
from fxtr_nix_runner._config import DEFAULT_IMAGE

# ---------------------------------------------------------------------------
# Total load: unset choice fields, default tuning fields
# ---------------------------------------------------------------------------


def test_defaults_when_no_file(tmp_path: Path):
    cfg = load_config(config_path=tmp_path / "missing.toml", env={})
    # No implicit default — the choice fields are unset.
    assert cfg.nix_backend is None
    assert cfg.container_runtime_cmd is None
    assert cfg.container_runtime_type == "auto"
    # Tuning fields always have their secure defaults.
    assert cfg.podman == PodmanConfig()
    assert cfg.podman.image == DEFAULT_IMAGE
    assert cfg.podman.sandbox is True
    assert cfg.podman.multi_user is True
    assert cfg.podman.filter_syscalls == "auto"


def test_file_sets_all_fields(tmp_path: Path):
    p = tmp_path / "nix_runtime.toml"
    p.write_text(
        'nix_backend = "podman"\n'
        'container_runtime_cmd = "podman"\n'
        'container_runtime_type = "podman"\n'
        "[nix_backend_podman]\n"
        'image = "docker.io/nixos/nix:2.99.0"\n'
        "sandbox = false\n"
        "multi_user = false\n"
        "filter_syscalls = true\n"
    )
    cfg = load_config(config_path=p, env={})
    assert cfg.nix_backend == "podman"
    assert cfg.container_runtime_cmd == "podman"
    assert cfg.container_runtime_type == "podman"
    assert cfg.podman.image == "docker.io/nixos/nix:2.99.0"
    assert cfg.podman.sandbox is False
    assert cfg.podman.multi_user is False
    assert cfg.podman.filter_syscalls is True


# ---------------------------------------------------------------------------
# Per-field env precedence (env wins per field; file wins for [nix_backend_podman])
# ---------------------------------------------------------------------------


def test_env_wins_per_field(tmp_path: Path):
    p = tmp_path / "nix_runtime.toml"
    p.write_text(
        'nix_backend = "host"\n'
        'container_runtime_cmd = "container"\n'
        '[nix_backend_podman]\nimage = "img:1"\n'
    )
    cfg = load_config(
        config_path=p,
        env={
            "FXTR_NIX_BACKEND": "podman",
            "FXTR_CONTAINER_RUNTIME_CMD": "podman",
            "FXTR_CONTAINER_RUNTIME_TYPE": "podman",
        },
    )
    # Each per-field env overrides the file's value for that field.
    assert cfg.nix_backend == "podman"
    assert cfg.container_runtime_cmd == "podman"
    assert cfg.container_runtime_type == "podman"
    # The [nix_backend_podman] table still comes from the file.
    assert cfg.podman.image == "img:1"


def test_env_fields_with_no_file(tmp_path: Path):
    cfg = load_config(
        config_path=tmp_path / "missing.toml",
        env={"FXTR_NIX_BACKEND": "podman", "FXTR_CONTAINER_RUNTIME_CMD": "podman"},
    )
    assert cfg.nix_backend == "podman"
    assert cfg.container_runtime_cmd == "podman"
    assert cfg.podman == PodmanConfig()


def test_empty_env_ignored(tmp_path: Path):
    # Empty strings are treated as unset and don't override.
    cfg = load_config(
        config_path=tmp_path / "missing.toml",
        env={"FXTR_NIX_BACKEND": "", "FXTR_CONTAINER_RUNTIME_CMD": ""},
    )
    assert cfg.nix_backend is None
    assert cfg.container_runtime_cmd is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_backend_in_file(tmp_path: Path):
    p = tmp_path / "nix_runtime.toml"
    p.write_text('nix_backend = "frobnicate"\n')
    with pytest.raises(ConfigError):
        load_config(config_path=p, env={})


def test_invalid_backend_in_env(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(config_path=tmp_path / "missing.toml", env={"FXTR_NIX_BACKEND": "frob"})


def test_invalid_runtime_type_in_file(tmp_path: Path):
    p = tmp_path / "nix_runtime.toml"
    p.write_text('container_runtime_type = "weird"\n')
    with pytest.raises(ConfigError):
        load_config(config_path=p, env={})


def test_invalid_runtime_type_in_env(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(
            config_path=tmp_path / "missing.toml", env={"FXTR_CONTAINER_RUNTIME_TYPE": "weird"}
        )


def test_invalid_filter_syscalls(tmp_path: Path):
    p = tmp_path / "nix_runtime.toml"
    p.write_text('[nix_backend_podman]\nfilter_syscalls = "sometimes"\n')
    with pytest.raises(ConfigError):
        load_config(config_path=p, env={})


def test_malformed_toml(tmp_path: Path):
    p = tmp_path / "nix_runtime.toml"
    p.write_text("this is not = = valid toml\n")
    with pytest.raises(ConfigError):
        load_config(config_path=p, env={})


def test_materialize_root_expands_user(tmp_path: Path):
    p = tmp_path / "nix_runtime.toml"
    p.write_text('[nix_backend_podman]\nmaterialize_root = "~/somewhere/store"\n')
    cfg = load_config(config_path=p, env={})
    assert cfg.podman.materialize_root == Path.home() / "somewhere" / "store"


def test_mountable_paths_parsed_and_expanded(tmp_path: Path):
    p = tmp_path / "nix_runtime.toml"
    p.write_text('[nix_backend_podman]\nmountable_paths = ["~/libs/foo", "/opt/bar"]\n')
    cfg = load_config(config_path=p, env={})
    assert cfg.podman.mountable_paths == (Path.home() / "libs" / "foo", Path("/opt/bar"))


def test_mountable_paths_must_be_string_list(tmp_path: Path):
    p = tmp_path / "nix_runtime.toml"
    p.write_text("[nix_backend_podman]\nmountable_paths = [1, 2]\n")
    with pytest.raises(ConfigError):
        load_config(config_path=p, env={})


# ---------------------------------------------------------------------------
# Full-file override
# ---------------------------------------------------------------------------


def test_full_config_override_via_env(tmp_path: Path):
    # FXTR_NIX_RUNTIME_CONFIG (when no explicit config_path) supersedes the
    # default file AND every per-field env var.
    override = tmp_path / "override.toml"
    override.write_text(
        'nix_backend = "podman"\n'
        'container_runtime_cmd = "podman"\n'
        '[nix_backend_podman]\nmountable_paths = ["/opt/lib"]\n'
    )
    cfg = load_config(
        env={
            "FXTR_NIX_RUNTIME_CONFIG": str(override),
            # All of these must be ignored by the full-file override.
            "FXTR_NIX_BACKEND": "host",
            "FXTR_CONTAINER_RUNTIME_CMD": "container",
        }
    )
    assert cfg.nix_backend == "podman"
    assert cfg.container_runtime_cmd == "podman"
    assert cfg.podman.mountable_paths == (Path("/opt/lib"),)
    assert cfg.source.full_override is True


def test_explicit_config_path_keeps_env_field_overrides(tmp_path: Path):
    # An explicit config_path is default-mode: per-field env still wins, and
    # FXTR_NIX_RUNTIME_CONFIG is ignored.
    p = tmp_path / "nix_runtime.toml"
    p.write_text('nix_backend = "host"\n')
    other = tmp_path / "other.toml"
    other.write_text('nix_backend = "host"\n')
    cfg = load_config(
        config_path=p,
        env={"FXTR_NIX_BACKEND": "podman", "FXTR_NIX_RUNTIME_CONFIG": str(other)},
    )
    assert cfg.nix_backend == "podman"


# ---------------------------------------------------------------------------
# require_* accessors (no implicit default)
# ---------------------------------------------------------------------------


def test_require_nix_backend_raises_when_unset(tmp_path: Path):
    cfg = load_config(config_path=tmp_path / "missing.toml", env={})
    with pytest.raises(ConfigError) as exc:
        cfg.require_nix_backend()
    msg = str(exc.value)
    # The message names the file consulted + the wizard.
    assert "nix_backend" in msg
    assert "fxtr_nix runtime setup" in msg
    assert "no such file" in msg


def test_require_nix_backend_returns_value(tmp_path: Path):
    cfg = load_config(config_path=tmp_path / "missing.toml", env={"FXTR_NIX_BACKEND": "podman"})
    assert cfg.require_nix_backend() == "podman"


def test_require_container_runtime_raises_when_unset(tmp_path: Path):
    cfg = load_config(config_path=tmp_path / "missing.toml", env={})
    with pytest.raises(ConfigError) as exc:
        cfg.require_container_runtime()
    msg = str(exc.value)
    assert "container_runtime_cmd" in msg
    assert "fxtr_nix runtime setup" in msg


def test_require_container_runtime_resolves(tmp_path: Path):
    cfg = load_config(
        config_path=tmp_path / "missing.toml", env={"FXTR_CONTAINER_RUNTIME_CMD": "podman"}
    )
    rt = cfg.require_container_runtime()
    assert rt == ContainerRuntime(cmd="podman", type="podman")


# ---------------------------------------------------------------------------
# ContainerRuntime model + auto inference
# ---------------------------------------------------------------------------


def test_auto_infers_podman_from_basename():
    assert resolve_container_runtime("podman", "auto") == ContainerRuntime("podman", "podman")
    # abspath still infers podman by basename
    assert resolve_container_runtime("/opt/podman/bin/podman", "auto").type == "podman"


def test_auto_infers_docker_compatible_for_everything_else():
    assert resolve_container_runtime("container", "auto").type == "docker_compatible"
    assert resolve_container_runtime("docker", "auto").type == "docker_compatible"
    assert resolve_container_runtime("/usr/bin/nerdctl", "auto").type == "docker_compatible"


def test_explicit_type_overrides_inference():
    # A podman wrapper not named "podman": cmd preserved, type explicit.
    rt = resolve_container_runtime("/opt/x/mypodman", "podman")
    assert rt == ContainerRuntime(cmd="/opt/x/mypodman", type="podman")


def test_cmd_round_trips():
    assert resolve_container_runtime("/weird/path/container", "auto").cmd == "/weird/path/container"
