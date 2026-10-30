"""Configuration resolution for ``fxtr_nix_runner`` (see :func:`load_config`).

This config spans **both** the nix backend (host vs podman) *and* the container
runtime (the executable + behavior family used to run nix-built images) — it's
the runtime for nix builders and nix-built images, hence ``nix_runtime.toml``.

**No implicit default.** ``load_config`` is *total* — it always succeeds and
reports what's set vs unset. The required-but-unset fields (``nix_backend``,
``container_runtime_cmd``) fail only *at point of use*, via the ``require_*``
accessors, which raise :class:`ConfigError` pointing at the setup wizard. This
lets the wizard introspect (load + look at what's set) without catching.

Resolution (per field; env is read **only here**):

- ``FXTR_NIX_RUNTIME_CONFIG`` (env) — a path to a complete config file that
  **is** the config: it supersedes both the default-location file and every
  per-field env var. The hermetic test/CI override (and the only way to set
  ``[nix_backend_podman] mountable_paths`` for a run).
- Otherwise the default-location file (``nix_runtime.toml`` if present) sets
  all fields + ``[nix_backend_podman]``, then per-field env overrides win for
  their field: ``FXTR_NIX_BACKEND`` → ``nix_backend``,
  ``FXTR_CONTAINER_RUNTIME_CMD`` → ``container_runtime_cmd``,
  ``FXTR_CONTAINER_RUNTIME_TYPE`` → ``container_runtime_type``.

Schema (all keys optional; the choice fields have no safe default)::

    nix_backend            = "podman"   # "host" | "podman"
    container_runtime_cmd  = "podman"   # the executable to invoke
    container_runtime_type = "auto"     # "auto" | "podman" | "docker_compatible"
    [nix_backend_podman]
    image = "docker.io/nixos/nix:2.34.7"   # builder/deliverer pin (>=2.34.7 for multi_user)
    sandbox = true                          # escape hatch; default true
    multi_user = true                       # nix-daemon + nixbld build users; default true
    filter_syscalls = "auto"                # "auto" | true | false
    materialize_root = "~/.cache/..."       # where materialize deposits host-readable outputs
    mountable_paths = []                    # host dirs bind-mounted RO into the builder (abspath)

:class:`ConfigError` is raised for a malformed file or an invalid value; a
*missing* file is fine (the choice fields stay unset, the tuning fields default).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

Backend = Literal["host", "podman"]
FilterSyscalls = Literal["auto"] | bool
#: The container-runtime behavior family as written in config — ``"auto"``
#: infers the concrete family from the command basename.
RuntimeType = Literal["auto", "podman", "docker_compatible"]
#: The resolved, concrete behavior family (``auto`` already inferred away).
ConcreteRuntimeType = Literal["podman", "docker_compatible"]

# Env var names — read in exactly one place (load_config).
ENV_FULL_CONFIG = "FXTR_NIX_RUNTIME_CONFIG"
ENV_NIX_BACKEND = "FXTR_NIX_BACKEND"
ENV_RUNTIME_CMD = "FXTR_CONTAINER_RUNTIME_CMD"
ENV_RUNTIME_TYPE = "FXTR_CONTAINER_RUNTIME_TYPE"

#: Pointed at by the ``require_*`` ConfigError messages.
WIZARD_HINT = "Run 'fxtr_nix runtime setup'"

# The builder image floor: with multi_user, older nix daemons crash under
# Rosetta emulation (the monitor-fd bug). 2.34.7 is a confirmed-good floor.
# Context: projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage9_nix_runner.md
DEFAULT_IMAGE = "docker.io/nixos/nix:2.34.7"


class ConfigError(Exception):
    """The ``nix_runtime.toml`` file is malformed/invalid, or a required-but-
    unset field was requested via a ``require_*`` accessor."""


def _xdg_config_home() -> Path:
    env = os.environ.get("XDG_CONFIG_HOME")
    return Path(env) if env else Path.home() / ".config"


def _xdg_cache_home() -> Path:
    env = os.environ.get("XDG_CACHE_HOME")
    return Path(env) if env else Path.home() / ".cache"


def default_config_path() -> Path:
    """``$XDG_CONFIG_HOME/fxtr/nix_runtime.toml`` (``~/.config`` fallback)."""
    return _xdg_config_home() / "fxtr" / "nix_runtime.toml"


def default_materialize_root() -> Path:
    """``$XDG_CACHE_HOME/fxtr_nix_runner/store`` (``~/.cache`` fallback)."""
    return _xdg_cache_home() / "fxtr_nix_runner" / "store"


@dataclass(frozen=True)
class PodmanConfig:
    """Podman-backend builder knobs (the ``[nix_backend_podman]`` toml table).
    Secure defaults; every field is overridable.

    Attributes:
        image: The ``docker.io/nixos/nix:<tag>`` image the builder + deliverer
            containers run. Pinned ``>= 2.34.7`` by default — the floor for
            ``multi_user`` (older daemons crash under Rosetta emulation; see
            the plan's Chunk 1.5). A lower pin is only safe with
            ``multi_user = False``.
        sandbox: Whether builds run in nix's build sandbox
            (``sandbox = true``). Default ``True``; an escape hatch only —
            disabling it loses the virby-parity isolation (private per-build
            netns + read-only store) the security posture relies on.
        multi_user: Whether the builder runs a multi-user ``nix-daemon`` with
            ``nixbld`` build-user separation (a post-sandbox-escape lands as a
            non-root user). Default ``True``; requires ``image >= 2.34.7``.
            ``False`` runs single-user (build as container-root) and lifts the
            image floor.
        filter_syscalls: nix's own setuid-blocking seccomp filter. ``"auto"``
            (default) enables it only on a native-amd64 builder — it can't load
            under amd64-on-arm64 (Rosetta) emulation — by resolving against the
            podman machine's arch; ``True`` / ``False`` force it on/off.
        materialize_root: Host directory under which ``materialize`` deposits
            the host-readable bind-mount store (a real nix store at
            ``<materialize_root>/nix/store/...``). Defaults to
            :func:`default_materialize_root`. Must be a host path the podman
            machine can bind-mount (i.e. under the user's home on macOS).
        mountable_paths: Host directories the builder bind-mounts **read-only
            at their original absolute paths**, so a flake input referencing a
            local path *under* one of them (e.g. a local lib checkout pinned as
            ``git+file:///…``) resolves in-container. A whitelist — only listed
            dirs are mountable (nothing is inferred from a lockfile); a listed
            dir that doesn't exist is silently skipped. Default empty (network
            inputs need no entry). An input outside every listed dir surfaces
            as nix's own "path does not exist" error.
    """

    image: str = DEFAULT_IMAGE
    sandbox: bool = True
    multi_user: bool = True
    filter_syscalls: FilterSyscalls = "auto"
    materialize_root: Path = field(default_factory=default_materialize_root)
    mountable_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ContainerRuntime:
    """A resolved container runtime: the executable to invoke + its concrete
    behavior family. Returned by :meth:`NixRuntimeConfig.require_container_runtime`.

    Attributes:
        cmd: The executable name or path used in every ``[cmd, "run"/"image"/…]``
            argv (``"podman"``, ``"container"``, ``"docker"``,
            ``/opt/.../podman-remote``, …). Open — any executable.
        type: The behavior family — ``"podman"`` (tarless socket delivery +
            socket-file mounts + ``localhost/`` ref prefix) or
            ``"docker_compatible"`` (the docker-CLI subset + tar ``image
            load``; covers Docker, Apple Container, and most others). The
            ``"auto"`` config value has already been inferred away.
    """

    cmd: str
    type: ConcreteRuntimeType


def _infer_runtime_type(cmd: str) -> ConcreteRuntimeType:
    """Infer the concrete behavior family from a runtime command's basename:
    ``podman`` → ``podman``; everything else → ``docker_compatible`` (nearly
    every runtime speaks the docker CLI, so that's the safe "not podman"
    default)."""
    return "podman" if Path(cmd).name == "podman" else "docker_compatible"


def resolve_container_runtime(cmd: str, runtime_type: RuntimeType) -> ContainerRuntime:
    """Build a :class:`ContainerRuntime`, inferring the family from ``cmd``
    when ``runtime_type == "auto"`` (else taking the explicit family)."""
    concrete: ConcreteRuntimeType = (
        _infer_runtime_type(cmd) if runtime_type == "auto" else runtime_type
    )
    return ContainerRuntime(cmd=cmd, type=concrete)


@dataclass(frozen=True)
class ConfigSource:
    """Provenance of a resolved config — used only to make ``require_*`` error
    messages specific (which file was consulted, whether it existed)."""

    path: Path
    path_existed: bool
    full_override: bool
    env_overrides: tuple[str, ...]


def _default_source() -> ConfigSource:
    return ConfigSource(
        path=default_config_path(), path_existed=False, full_override=False, env_overrides=()
    )


@dataclass(frozen=True)
class NixRuntimeConfig:
    """The resolved nix-runtime configuration (see :func:`load_config`).

    Load is total; *use* is where a required-but-unset choice field fails.
    Consumers call the ``require_*`` accessors rather than reading the raw
    ``… | None`` fields, so the "how to fix" message lives in one place.

    Attributes:
        nix_backend: ``"host"`` / ``"podman"``, or ``None`` when not configured.
        container_runtime_cmd: The runtime executable, or ``None`` when not
            configured.
        container_runtime_type: The behavior family selector; defaults to
            ``"auto"`` (inferred from ``container_runtime_cmd`` at resolve time).
        podman: The podman-backend builder knobs (always populated with secure
            defaults; used only when ``nix_backend == "podman"``).
        source: Provenance, for ``require_*`` messages only.
    """

    nix_backend: Backend | None = None
    container_runtime_cmd: str | None = None
    container_runtime_type: RuntimeType = "auto"
    podman: PodmanConfig = field(default_factory=PodmanConfig)
    source: ConfigSource = field(default_factory=_default_source)

    def require_nix_backend(self) -> Backend:
        """The configured nix backend, or :class:`ConfigError` naming the fix."""
        if self.nix_backend is None:
            raise ConfigError(self._unset_message("nix_backend", f"{ENV_NIX_BACKEND}=host|podman"))
        return self.nix_backend

    def require_container_runtime(self) -> ContainerRuntime:
        """The resolved :class:`ContainerRuntime` (``cmd`` + concrete ``type``),
        or :class:`ConfigError` naming the fix when ``container_runtime_cmd`` is
        unset."""
        if self.container_runtime_cmd is None:
            raise ConfigError(
                self._unset_message("container_runtime_cmd", f"{ENV_RUNTIME_CMD}=podman")
            )
        return resolve_container_runtime(self.container_runtime_cmd, self.container_runtime_type)

    def _unset_message(self, field_name: str, env_hint: str) -> str:
        src = self.source
        where = f"{src.path} ({'present' if src.path_existed else 'no such file'})"
        return (
            f"{field_name} not configured. Looked in {where}. " f"{WIZARD_HINT}, or set {env_hint}."
        )


def _coerce_backend(value: Any, *, source: str) -> Backend:
    if value not in ("host", "podman"):
        raise ConfigError(f"{source}: nix_backend must be 'host' or 'podman', got {value!r}")
    return value


def _coerce_runtime_type(value: Any, *, source: str) -> RuntimeType:
    if value in ("auto", "podman", "docker_compatible"):
        return value
    raise ConfigError(
        f"{source}: container_runtime_type must be 'auto', 'podman', or "
        f"'docker_compatible', got {value!r}"
    )


def _coerce_filter_syscalls(value: Any) -> FilterSyscalls:
    if value == "auto" or isinstance(value, bool):
        return value
    raise ConfigError(
        f'[nix_backend_podman] filter_syscalls must be "auto", true, or false, got {value!r}'
    )


def _podman_from_table(table: dict[str, Any]) -> PodmanConfig:
    base = PodmanConfig()
    kwargs: dict[str, Any] = {}
    if "image" in table:
        if not isinstance(table["image"], str):
            raise ConfigError("[nix_backend_podman] image must be a string")
        kwargs["image"] = table["image"]
    if "sandbox" in table:
        if not isinstance(table["sandbox"], bool):
            raise ConfigError("[nix_backend_podman] sandbox must be a boolean")
        kwargs["sandbox"] = table["sandbox"]
    if "multi_user" in table:
        if not isinstance(table["multi_user"], bool):
            raise ConfigError("[nix_backend_podman] multi_user must be a boolean")
        kwargs["multi_user"] = table["multi_user"]
    if "filter_syscalls" in table:
        kwargs["filter_syscalls"] = _coerce_filter_syscalls(table["filter_syscalls"])
    if "materialize_root" in table:
        if not isinstance(table["materialize_root"], str):
            raise ConfigError("[nix_backend_podman] materialize_root must be a string path")
        kwargs["materialize_root"] = Path(table["materialize_root"]).expanduser()
    if "mountable_paths" in table:
        val = table["mountable_paths"]
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise ConfigError("[nix_backend_podman] mountable_paths must be a list of string paths")
        kwargs["mountable_paths"] = tuple(Path(x).expanduser() for x in val)
    return replace(base, **kwargs)


@dataclass(frozen=True)
class _FileFields:
    """Choice + tuning fields parsed from a config file (or defaults when the
    file is absent). The choice fields are ``None`` when the file omits them."""

    nix_backend: Backend | None
    container_runtime_cmd: str | None
    container_runtime_type: RuntimeType
    podman: PodmanConfig


def load_config(
    *,
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> NixRuntimeConfig:
    """Resolve the effective config (see the module docstring for precedence).

    Total: always succeeds, leaving unset choice fields ``None``. The
    ``require_*`` accessors are what raise :class:`ConfigError` for a
    required-but-unset field — and only the one a given consumer needs.

    Args:
        config_path: Explicit toml path (mainly for unit tests). When given,
            ``FXTR_NIX_RUNTIME_CONFIG`` is ignored but the per-field env vars
            still apply (default-mode semantics). A non-existent path yields
            unset/defaults.
        env: Environment mapping for the ``FXTR_*`` vars. Defaults to
            ``os.environ``. Empty/unset values do not override.

    Returns:
        The resolved :class:`NixRuntimeConfig`.

    Raises:
        ConfigError: The file can't be parsed, or holds an invalid value.
    """
    if env is None:
        env = dict(os.environ)

    # FXTR_NIX_RUNTIME_CONFIG (env) is the full-config override — but only when
    # the caller didn't pass an explicit config_path (which keeps unit-test
    # semantics: config_path + per-field env => env-wins-per-field).
    full_override = False
    if config_path is None:
        override = env.get(ENV_FULL_CONFIG)
        if override:
            config_path = Path(override)
            full_override = True
        else:
            config_path = default_config_path()

    existed = config_path.is_file()
    fields = _parse_config_file(config_path)

    if full_override:
        # The file IS the config; per-field env vars are ignored.
        source = ConfigSource(
            path=config_path, path_existed=existed, full_override=True, env_overrides=()
        )
        return NixRuntimeConfig(
            nix_backend=fields.nix_backend,
            container_runtime_cmd=fields.container_runtime_cmd,
            container_runtime_type=fields.container_runtime_type,
            podman=fields.podman,
            source=source,
        )

    backend = fields.nix_backend
    cmd = fields.container_runtime_cmd
    rtype = fields.container_runtime_type
    applied: list[str] = []

    env_backend = env.get(ENV_NIX_BACKEND)
    if env_backend:
        backend = _coerce_backend(env_backend, source=ENV_NIX_BACKEND)
        applied.append(ENV_NIX_BACKEND)
    env_cmd = env.get(ENV_RUNTIME_CMD)
    if env_cmd:
        cmd = env_cmd
        applied.append(ENV_RUNTIME_CMD)
    env_type = env.get(ENV_RUNTIME_TYPE)
    if env_type:
        rtype = _coerce_runtime_type(env_type, source=ENV_RUNTIME_TYPE)
        applied.append(ENV_RUNTIME_TYPE)

    source = ConfigSource(
        path=config_path, path_existed=existed, full_override=False, env_overrides=tuple(applied)
    )
    return NixRuntimeConfig(
        nix_backend=backend,
        container_runtime_cmd=cmd,
        container_runtime_type=rtype,
        podman=fields.podman,
        source=source,
    )


def _parse_config_file(config_path: Path) -> _FileFields:
    """Parse a (possibly absent) config file into its fields; unset choice
    fields + default tuning fields for a missing file. Raises ``ConfigError``
    on a malformed file."""
    nix_backend: Backend | None = None
    cmd: str | None = None
    rtype: RuntimeType = "auto"
    podman = PodmanConfig()
    if config_path.is_file():
        try:
            body = tomllib.loads(config_path.read_text())
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise ConfigError(f"{config_path}: cannot parse: {exc}") from exc
        if "nix_backend" in body:
            nix_backend = _coerce_backend(body["nix_backend"], source=str(config_path))
        if "container_runtime_cmd" in body:
            if not isinstance(body["container_runtime_cmd"], str):
                raise ConfigError(f"{config_path}: container_runtime_cmd must be a string")
            cmd = body["container_runtime_cmd"]
        if "container_runtime_type" in body:
            rtype = _coerce_runtime_type(body["container_runtime_type"], source=str(config_path))
        if "nix_backend_podman" in body:
            if not isinstance(body["nix_backend_podman"], dict):
                raise ConfigError(f"{config_path}: [nix_backend_podman] must be a table")
            podman = _podman_from_table(body["nix_backend_podman"])
    return _FileFields(
        nix_backend=nix_backend,
        container_runtime_cmd=cmd,
        container_runtime_type=rtype,
        podman=podman,
    )
