"""Configuration + discovery for the repo entity store (the lower layer).

This module owns the **repo-store / VCS-discovery** layer of configuration:
locating the VCS root and the ``.fxtr/`` directory, parsing
``.fxtr/fxtr_store_repo_config.toml``, and the standalone :class:`RepoStoreConfig`
+ :func:`load_repo_store_config` the repo entity store consumes.

It deliberately knows **nothing** about the tsv-namespace marker
(``fxtr_namespace.toml``), pins, TSVs, or :class:`TsvNamespaceBackend`. The
tsv-namespace layer (``fxtr2.data.tsv_namespace.config``) composes this module and
adds the marker-specific config on top. Keeping these helpers here (rather than
inside the ``tsv_namespace`` package) is what lets ``fxtr2.data.repo_storage`` import
them without a circular import: ``repo_storage`` is the lower layer and
``tsv_namespace`` is a view over it, so the dependency only ever points
``tsv_namespace → repo_storage → repo_storage_config``.

Configuration files this layer touches:
- ~/.fxtr/config.toml: optional user-level config (``vcs_root_command``)
- .fxtr/fxtr_store_repo_config.toml: per-repo store config (``index_namespace_id``,
  ``sqlite_store_path``)
"""

from __future__ import annotations

import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import tomlkit


class FxtrConfigError(Exception):
    """Error related to fxtr store configuration."""


# File names (repo-store / discovery layer)
FXTR_DIR_NAME = ".fxtr"
REPO_CONFIG_FILENAME = "fxtr_store_repo_config.toml"
USER_CONFIG_FILENAME = "config.toml"
DEFAULT_SQLITE_STORE_FILENAME = "repo_backing_storage.sqlite"

# Namespace ID prefix for the repo entity store (used when generating new IDs).
# The on-disk config key remains ``index_namespace_id``; value format ``index:{uuid}``.
INDEX_NAMESPACE_PREFIX = "index:"


def _generate_index_namespace_id() -> str:
    """Generate a new repo-store (index) namespace ID."""
    return f"{INDEX_NAMESPACE_PREFIX}{uuid.uuid4()}"


@dataclass(frozen=True)
class RepoStoreConfig:
    """Configuration for a repo entity store (the by-id store under ``.fxtr/``).

    This is the store-only subset of configuration: it can be loaded on its own
    from ``.fxtr/fxtr_store_repo_config.toml`` via :func:`load_repo_store_config`,
    independent of any namespace-marker / tsv-namespace view. The standalone
    repo entity store (``fxtr2.data.repo_storage``) consumes this directly;
    ``TsvNamespaceConfig`` composes it.
    """

    fxtr_dir: Path
    """Path to the .fxtr directory (sibling to VCS root)."""

    sqlite_store_path: Path
    """Path to the sqlite store file."""

    store_namespace_id: str
    """Full namespace ID for the repo entity store (shared across worktrees).

    Conceptually the "store namespace"; the on-disk key in
    ``fxtr_store_repo_config.toml`` remains ``index_namespace_id`` and the value
    format is unchanged (``index:{uuid}``)."""


def _load_user_config() -> dict[str, Any]:
    """Load the user-level config from ~/.fxtr/config.toml.

    Returns:
        Dict with parsed configuration values, or empty dict if file doesn't exist.

    Raises:
        FxtrConfigError: If the file exists but cannot be parsed.
    """
    config_path = Path.home() / FXTR_DIR_NAME / USER_CONFIG_FILENAME
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise FxtrConfigError(f"Invalid TOML in {config_path}: {e}") from e


async def _find_vcs_root_git(path: Path) -> Path:
    """Find the VCS root using git rev-parse --git-common-dir.

    This is the default VCS root detection for git repositories.
    Runs `git rev-parse --git-common-dir` and returns its parent directory.

    Args:
        path: A path inside the git repository.

    Returns:
        The absolute path to the VCS root (parent of git-common-dir).

    Raises:
        FxtrConfigError: If the path is not inside a git repository.
    """
    try:
        result = await anyio.run_process(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=path,
            check=True,
        )
        git_common_dir = result.stdout.decode().strip()
        # The path may be relative, make it absolute, then get parent
        return (path / git_common_dir).resolve().parent
    except anyio.get_cancelled_exc_class():
        raise
    except Exception as e:
        raise FxtrConfigError(f"Failed to find git common directory for {path}.\nError: {e}") from e


async def _find_vcs_root_custom(path: Path, command: list[str]) -> Path:
    """Find the VCS root using a custom command.

    Args:
        path: The working directory to run the command from.
        command: List of command arguments (NOT a shell string).

    Returns:
        The absolute path output by the command.

    Raises:
        FxtrConfigError: If the command fails or outputs invalid data.
    """
    try:
        result = await anyio.run_process(
            command,
            cwd=path,
            check=True,
        )
        vcs_root = result.stdout.decode().strip()
        vcs_root_path = Path(vcs_root)
        if not vcs_root_path.is_absolute():
            raise FxtrConfigError(
                "VCS root command must output an absolute path.\n"
                f"Command: {command}\n"
                f"Output: {vcs_root!r}"
            )
        return vcs_root_path
    except anyio.get_cancelled_exc_class():
        raise
    except FxtrConfigError:
        raise
    except Exception as e:
        raise FxtrConfigError(f"VCS root command failed.\nCommand: {command}\nError: {e}") from e


async def find_vcs_root(path: Path, user_config: dict[str, Any] | None = None) -> Path:
    """Find the VCS root directory for the given path.

    Uses the following priority:
    1. If vcs_root_command is set in user config, run it
    2. Otherwise, use default git detection

    Args:
        path: A path inside the VCS repository.
        user_config: Optional pre-loaded user config. If None, loads from disk.

    Returns:
        The absolute path to the VCS root directory.

    Raises:
        FxtrConfigError: If VCS root cannot be determined.
    """
    if user_config is None:
        user_config = _load_user_config()

    vcs_root_command = user_config.get("vcs_root_command")

    if vcs_root_command is not None:
        if not isinstance(vcs_root_command, list):
            raise FxtrConfigError(
                "vcs_root_command must be a list of strings, not"
                f" {type(vcs_root_command).__name__}.\n"
                f'Example: vcs_root_command = ["hg", "root"]'
            )
        return await _find_vcs_root_custom(path, vcs_root_command)
    else:
        try:
            return await _find_vcs_root_git(path)
        except FxtrConfigError as e:
            raise FxtrConfigError(
                f"Could not detect VCS root for {path}.\n\n"
                f"Possible solutions:\n"
                f"  1. Run inside a git repository\n"
                f"  2. Configure vcs_root_command in ~/.fxtr/config.toml\n"
                f'     Example: vcs_root_command = ["hg", "root"]\n'
                f"  3. Set repo_config_path explicitly in fxtr_namespace.local.toml\n"
                f'     Example: repo_config_path = "/path/to/.fxtr/config.toml"\n\n'
                f"Original error: {e}"
            ) from e


async def find_fxtr_dir(store_root: Path, user_config: dict[str, Any] | None = None) -> Path:
    """Find the .fxtr directory for a store.

    The .fxtr directory is located as a sibling to the VCS root directory.
    This ensures that linked worktrees share the same .fxtr directory.

    Args:
        store_root: A path inside the repository (e.g. the namespace store root).
        user_config: Optional pre-loaded user config. If None, loads from disk.

    Returns:
        The absolute path to the .fxtr directory.

    Raises:
        FxtrConfigError: If the VCS root cannot be found.
    """
    vcs_root = await find_vcs_root(store_root, user_config)
    return vcs_root / FXTR_DIR_NAME


def _parse_repo_config(config_path: Path) -> dict[str, Any]:
    """Parse .fxtr/fxtr_store_repo_config.toml file.

    Args:
        config_path: Path to the per-repo config.

    Returns:
        Dict with parsed configuration values.

    Raises:
        FxtrConfigError: If the file doesn't exist or cannot be parsed.
    """
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise FxtrConfigError(
            f"Per-repo config file not found: {config_path}\n"
            "The .fxtr directory may have been deleted. Re-initialize the store or "
            "restore from backup."
        )
    except tomllib.TOMLDecodeError as e:
        raise FxtrConfigError(f"Invalid TOML in {config_path}: {e}") from e


def _write_repo_config(config_path: Path, config: dict[str, Any]) -> None:
    """Write .fxtr/fxtr_store_repo_config.toml file.

    Args:
        config_path: Path to the per-repo config.
        config: Configuration dict to write.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        tomlkit.dump(config, f)


def _resolve_store_namespace_id(repo_config: dict[str, Any], repo_config_path: Path) -> str:
    """Validate + return the store (index) namespace ID from a parsed repo config.

    The on-disk key remains ``index_namespace_id``. Accepts any non-empty string
    (no prefix validation). Shared by ``load_config`` and
    :func:`load_repo_store_config`.
    """
    index_ns_id = repo_config.get("index_namespace_id")
    if index_ns_id is None:
        raise FxtrConfigError(
            f"index_namespace_id not found in {repo_config_path}\n"
            "The per-repo config file is corrupted. Re-initialize the store or "
            "restore from backup."
        )
    if not isinstance(index_ns_id, str):
        raise FxtrConfigError(
            f"Invalid index_namespace_id in {repo_config_path}: {index_ns_id!r}\nExpected a string."
        )
    return index_ns_id


def _resolve_sqlite_store_path(repo_config: dict[str, Any], fxtr_dir: Path) -> Path:
    """Resolve the sqlite store path from a parsed repo config.

    Relative ``sqlite_store_path`` values are interpreted relative to the
    ``.fxtr`` directory; absent values default to
    ``.fxtr/repo_backing_storage.sqlite``. Shared by ``load_config`` and
    :func:`load_repo_store_config`.
    """
    sqlite_store_path_str = repo_config.get("sqlite_store_path")
    if sqlite_store_path_str is not None:
        sqlite_store_path = Path(sqlite_store_path_str)
        if not sqlite_store_path.is_absolute():
            sqlite_store_path = fxtr_dir / sqlite_store_path
        return sqlite_store_path
    return fxtr_dir / DEFAULT_SQLITE_STORE_FILENAME


async def load_repo_store_config(
    repo_path: Path,
    *,
    create: bool = False,
    user_config: dict[str, Any] | None = None,
) -> RepoStoreConfig:
    """Load the repo-entity-store config for a repo, independent of any namespace view.

    Discovers the ``.fxtr`` directory (sibling of the VCS root) for ``repo_path``
    and reads ``fxtr_store_repo_config.toml``. Unlike ``load_config``, this does
    NOT require a ``fxtr_namespace.toml`` marker — it is the entry point the
    standalone repo entity store uses.

    Args:
        repo_path: Any path inside the repository (the VCS root is discovered).
        create: If True, initialize ``.fxtr/`` + ``fxtr_store_repo_config.toml``
            (minting a fresh store namespace id) if absent.
        user_config: Optional pre-loaded user config (for VCS detection).

    Raises:
        FxtrConfigError: If discovery fails or the config is missing/corrupt
            (when ``create`` is False).
    """
    if user_config is None:
        user_config = _load_user_config()
    fxtr_dir = await find_fxtr_dir(repo_path, user_config)
    if create:
        ensure_repo_config_initialized(fxtr_dir)
    repo_config_path = fxtr_dir / REPO_CONFIG_FILENAME
    repo_config = _parse_repo_config(repo_config_path)
    return RepoStoreConfig(
        fxtr_dir=fxtr_dir,
        sqlite_store_path=_resolve_sqlite_store_path(repo_config, fxtr_dir),
        store_namespace_id=_resolve_store_namespace_id(repo_config, repo_config_path),
    )


# .gitignore content for the .fxtr directory
_FXTR_DIR_GITIGNORE = """\
# Ignore everything in this directory
*
"""


def ensure_repo_config_initialized(fxtr_dir: Path) -> None:
    """Create ``.fxtr/`` + ``fxtr_store_repo_config.toml`` (with a fresh store
    namespace id) and the ``.fxtr/.gitignore`` if absent. Idempotent.

    Shared by ``init_namespace`` and :func:`load_repo_store_config` (create=True).
    """
    fxtr_dir.mkdir(parents=True, exist_ok=True)

    repo_config_path = fxtr_dir / REPO_CONFIG_FILENAME
    if repo_config_path.exists():
        # Config exists (another worktree already initialized); ensure the id is present.
        repo_config = _parse_repo_config(repo_config_path)
        if "index_namespace_id" not in repo_config:
            repo_config["index_namespace_id"] = _generate_index_namespace_id()
            _write_repo_config(repo_config_path, repo_config)
    else:
        repo_config = {"index_namespace_id": _generate_index_namespace_id()}
        _write_repo_config(repo_config_path, repo_config)

    fxtr_gitignore_path = fxtr_dir / ".gitignore"
    if not fxtr_gitignore_path.exists():
        fxtr_gitignore_path.write_text(_FXTR_DIR_GITIGNORE)
