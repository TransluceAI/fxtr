"""Configuration management for git-backed fxtr stores.

This module handles parsing and creating configuration files for git-backed
fxtr stores. Configuration is layered across three levels:

1. User-level (~/.fxtr/config.toml): Global settings like VCS root detection
2. Per-repo (.fxtr/config.toml): Repo-wide settings shared across worktrees
3. Per-store (fxtr_store.toml + fxtr_store.local.toml): Store-specific settings

Configuration files:
- ~/.fxtr/config.toml: Optional user-level config (vcs_root_command)
- .fxtr/fxtr_store_repo_config.toml: Per-repo config (index_namespace_id, sqlite_store_path)
- fxtr_store.toml: Main store configuration (git-tracked)
- fxtr_store.local.toml: Worktree-specific configuration (gitignored)
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


# File names
STORE_CONFIG_FILENAME = "fxtr_store.toml"
LOCAL_CONFIG_FILENAME = "fxtr_store.local.toml"
FXTR_DIR_NAME = ".fxtr"
REPO_CONFIG_FILENAME = "fxtr_store_repo_config.toml"
USER_CONFIG_FILENAME = "config.toml"
DEFAULT_SQLITE_STORE_FILENAME = "repo_backing_storage.sqlite"

# Default values
DEFAULT_RESERVED_PREFIX = "index"


# Namespace ID prefix for index namespace (used when generating new IDs)
INDEX_NAMESPACE_PREFIX = "index:"


def _generate_index_namespace_id() -> str:
    """Generate a new index namespace ID."""
    return f"{INDEX_NAMESPACE_PREFIX}{uuid.uuid4()}"


def _default_worktree_namespace_id(store_root: Path) -> str:
    """Generate the default worktree namespace ID based on store path."""
    return f"worktree:{store_root}"


@dataclass(frozen=True)
class GitStoreConfig:
    """Parsed configuration for a git-backed fxtr store.

    This combines information from all configuration layers:
    - ~/.fxtr/config.toml (vcs_root_command)
    - .fxtr/fxtr_store_repo_config.toml (index_namespace_id, sqlite_store_path)
    - fxtr_store.toml (reserved_prefix, name, description)
    - fxtr_store.local.toml (worktree_namespace_id, repo_config_path)
    """

    store_root: Path
    """Path to the directory containing fxtr_store.toml."""

    reserved_prefix: str
    """The reserved prefix for keys without colon (default: "index")."""

    worktree_namespace_id: str
    """Full namespace ID for this worktree's namespace in sqlite."""

    index_namespace_id: str
    """Full namespace ID for the index namespace (shared across worktrees)."""

    sqlite_store_path: Path
    """Path to the sqlite store file."""

    fxtr_dir: Path
    """Path to the .fxtr directory (sibling to VCS root)."""

    # Optional metadata (not used programmatically yet)
    name: str | None = None
    description: str | None = None


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
                f"  3. Set repo_config_path explicitly in fxtr_store.local.toml\n"
                f'     Example: repo_config_path = "/path/to/.fxtr/config.toml"\n\n'
                f"Original error: {e}"
            ) from e


async def find_fxtr_dir(store_root: Path, user_config: dict[str, Any] | None = None) -> Path:
    """Find the .fxtr directory for a store.

    The .fxtr directory is located as a sibling to the VCS root directory.
    This ensures that linked worktrees share the same .fxtr directory.

    Args:
        store_root: Path to the directory containing fxtr_store.toml.
        user_config: Optional pre-loaded user config. If None, loads from disk.

    Returns:
        The absolute path to the .fxtr directory.

    Raises:
        FxtrConfigError: If the VCS root cannot be found.
    """
    vcs_root = await find_vcs_root(store_root, user_config)
    return vcs_root / FXTR_DIR_NAME


def _parse_store_config(config_path: Path) -> dict[str, Any]:
    """Parse fxtr_store.toml file.

    Args:
        config_path: Path to fxtr_store.toml.

    Returns:
        Dict with parsed configuration values.

    Raises:
        FxtrConfigError: If the file cannot be parsed.
    """
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise FxtrConfigError(f"Store config file not found: {config_path}")
    except tomllib.TOMLDecodeError as e:
        raise FxtrConfigError(f"Invalid TOML in {config_path}: {e}") from e


def _parse_local_config(config_path: Path) -> dict[str, Any]:
    """Parse fxtr_store.local.toml file.

    Args:
        config_path: Path to fxtr_store.local.toml.

    Returns:
        Dict with parsed configuration values, or empty dict if file doesn't exist.

    Raises:
        FxtrConfigError: If the file exists but cannot be parsed.
    """
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise FxtrConfigError(f"Invalid TOML in {config_path}: {e}") from e


def _parse_repo_config(config_path: Path) -> dict[str, Any]:
    """Parse .fxtr/config.toml file.

    Args:
        config_path: Path to .fxtr/config.toml.

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
    """Write .fxtr/config.toml file.

    Args:
        config_path: Path to .fxtr/config.toml.
        config: Configuration dict to write.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        tomlkit.dump(config, f)


async def load_config(store_root: Path) -> GitStoreConfig:
    """Load configuration for an existing git-backed fxtr store.

    Reads configuration from all layers:
    - ~/.fxtr/config.toml (user-level)
    - .fxtr/fxtr_store_repo_config.toml (per-repo)
    - fxtr_store.toml (store)
    - fxtr_store.local.toml (store local, optional)

    Args:
        store_root: Path to the directory containing fxtr_store.toml.

    Returns:
        GitStoreConfig with all configuration values.

    Raises:
        FxtrConfigError: If configuration is invalid or missing required files.
    """
    store_root = store_root.resolve()
    config_path = store_root / STORE_CONFIG_FILENAME

    # Parse main store config
    store_config = _parse_store_config(config_path)

    # Parse local config (optional - may not exist)
    local_config_path = store_root / LOCAL_CONFIG_FILENAME
    local_config = _parse_local_config(local_config_path)

    # Load user config (for VCS detection)
    user_config = _load_user_config()

    # Find or use explicit repo config path
    repo_config_path_str = local_config.get("repo_config_path")
    if repo_config_path_str is not None:
        # Explicit path - skip VCS detection
        repo_config_path = Path(repo_config_path_str)
        if not repo_config_path.is_absolute():
            repo_config_path = store_root / repo_config_path
        fxtr_dir = repo_config_path.parent
    else:
        # Use VCS detection
        fxtr_dir = await find_fxtr_dir(store_root, user_config)
        repo_config_path = fxtr_dir / REPO_CONFIG_FILENAME

    # Parse per-repo config
    repo_config = _parse_repo_config(repo_config_path)

    # Get index namespace ID - required (accept any string, no prefix validation)
    index_ns_id = repo_config.get("index_namespace_id")
    if index_ns_id is None:
        raise FxtrConfigError(
            f"index_namespace_id not found in {repo_config_path}\n"
            "The per-repo config file is corrupted. Re-initialize the store or "
            "restore from backup."
        )
    if not isinstance(index_ns_id, str):
        raise FxtrConfigError(
            f"Invalid index_namespace_id in {repo_config_path}: {index_ns_id!r}\n"
            "Expected a string."
        )

    # Get worktree namespace ID: use override from local config, or default to path-based
    worktree_ns_id = local_config.get("worktree_namespace_id")
    if worktree_ns_id is not None:
        if not isinstance(worktree_ns_id, str):
            raise FxtrConfigError(
                f"Invalid worktree_namespace_id in {local_config_path}: {worktree_ns_id!r}\n"
                "Expected a string."
            )
    else:
        # Use path-based default (don't create local config file)
        worktree_ns_id = _default_worktree_namespace_id(store_root)

    # Resolve sqlite store path from per-repo config
    sqlite_store_path_str = repo_config.get("sqlite_store_path")
    if sqlite_store_path_str is not None:
        sqlite_store_path = Path(sqlite_store_path_str)
        if not sqlite_store_path.is_absolute():
            # Relative paths are relative to the .fxtr directory
            sqlite_store_path = fxtr_dir / sqlite_store_path
    else:
        # Default: .fxtr/repo_backing_storage.sqlite
        sqlite_store_path = fxtr_dir / DEFAULT_SQLITE_STORE_FILENAME

    return GitStoreConfig(
        store_root=store_root,
        reserved_prefix=store_config.get("reserved_prefix", DEFAULT_RESERVED_PREFIX),
        worktree_namespace_id=worktree_ns_id,
        index_namespace_id=index_ns_id,
        sqlite_store_path=sqlite_store_path,
        fxtr_dir=fxtr_dir,
        name=store_config.get("name"),
        description=store_config.get("description"),
    )


# .gitignore content for the store root directory
_STORE_ROOT_GITIGNORE = """\
# Fxtr git store local files (auto-generated)
fxtr_store.local.toml

# Sync lock files
*.sync_lock

# Dirty mark directories - ignore contents except the sentinel file
*.fxtr.tsv.dirty/*
!*.fxtr.tsv.dirty/DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md
"""

# .gitignore content for the .fxtr directory
_FXTR_DIR_GITIGNORE = """\
# Ignore everything in this directory
*
"""


async def init_store(
    store_root: Path,
    *,
    reserved_prefix: str = DEFAULT_RESERVED_PREFIX,
    name: str | None = None,
    description: str | None = None,
) -> None:
    """Initialize a new git-backed fxtr store.

    Creates the necessary configuration files and directories:
    - fxtr_store.toml (main config)
    - .gitignore (ignores local files and dirty mark contents)
    - .fxtr/ directory with config.toml

    Args:
        store_root: Path where the store should be created.
        reserved_prefix: Reserved prefix for keys without colon (default: "index").
        name: Optional human-readable name for the store.
        description: Optional description for the store.

    Raises:
        FxtrConfigError: If the store already exists or initialization fails.
    """
    store_root = store_root.resolve()
    config_path = store_root / STORE_CONFIG_FILENAME

    # Check if store already exists
    if config_path.exists():
        raise FxtrConfigError(
            f"Store already exists at {store_root}\nUse load_config() to open an existing store."
        )

    # Load user config and find .fxtr directory (validates VCS setup)
    user_config = _load_user_config()
    fxtr_dir = await find_fxtr_dir(store_root, user_config)

    # Create .fxtr directory
    fxtr_dir.mkdir(parents=True, exist_ok=True)

    # Create or update per-repo config
    repo_config_path = fxtr_dir / REPO_CONFIG_FILENAME
    if repo_config_path.exists():
        # Config exists (another worktree already initialized), just read it
        repo_config = _parse_repo_config(repo_config_path)
        if "index_namespace_id" not in repo_config:
            # Config exists but is missing the ID - add it
            repo_config["index_namespace_id"] = _generate_index_namespace_id()
            _write_repo_config(repo_config_path, repo_config)
    else:
        # Create new per-repo config with generated namespace ID
        repo_config = {"index_namespace_id": _generate_index_namespace_id()}
        _write_repo_config(repo_config_path, repo_config)

    # Write .fxtr/.gitignore
    fxtr_gitignore_path = fxtr_dir / ".gitignore"
    if not fxtr_gitignore_path.exists():
        fxtr_gitignore_path.write_text(_FXTR_DIR_GITIGNORE)

    # Create store root directory if needed
    store_root.mkdir(parents=True, exist_ok=True)

    # Write fxtr_store.toml
    store_config: dict[str, Any] = {}
    if reserved_prefix != DEFAULT_RESERVED_PREFIX:
        store_config["reserved_prefix"] = reserved_prefix
    if name is not None:
        store_config["name"] = name
    if description is not None:
        store_config["description"] = description

    with open(config_path, "w") as f:
        tomlkit.dump(store_config, f)

    # Write .gitignore in store root
    gitignore_path = store_root / ".gitignore"
    # Append to existing or create new
    if gitignore_path.exists():
        existing = gitignore_path.read_text()
        if "fxtr_store.local.toml" not in existing:
            with open(gitignore_path, "a") as f:
                f.write("\n" + _STORE_ROOT_GITIGNORE)
    else:
        gitignore_path.write_text(_STORE_ROOT_GITIGNORE)


def store_exists(store_root: Path) -> bool:
    """Check if a fxtr store exists at the given path.

    Args:
        store_root: Path to check.

    Returns:
        True if fxtr_store.toml exists at the path.
    """
    return (store_root / STORE_CONFIG_FILENAME).exists()
