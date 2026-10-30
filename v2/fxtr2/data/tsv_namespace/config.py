"""Configuration for tsv namespaces (the marker layer).

This module owns the **tsv-namespace** layer of configuration: the committed
marker (``fxtr_namespace.toml`` + ``fxtr_namespace.local.toml``), the
reserved-prefix / worktree-namespace settings, :class:`TsvNamespaceConfig`,
:func:`load_config`, and :func:`init_namespace` / :func:`namespace_exists`.

The **repo-store / VCS-discovery** layer (``RepoStoreConfig``,
``find_vcs_root`` / ``find_fxtr_dir``, ``.fxtr/`` repo-config parsing,
``load_repo_store_config``, ``ensure_repo_config_initialized``, ``FxtrConfigError``)
lives in :mod:`fxtr2.data.repo_storage_config`, which this module composes. That
split keeps ``fxtr2.data.repo_storage`` (the lower layer) importable without a
circular import. Those names are re-exported here for backward compatibility, so
existing ``from fxtr2.data.tsv_namespace.config import …`` call sites keep working.

Configuration layers:
1. User-level (~/.fxtr/config.toml): VCS root detection
2. Per-repo (.fxtr/fxtr_store_repo_config.toml): index_namespace_id, sqlite_store_path
3. Per-namespace (fxtr_namespace.toml + fxtr_namespace.local.toml): marker settings
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from fxtr2.data.repo_storage_config import (
    REPO_CONFIG_FILENAME,
    FxtrConfigError,
    RepoStoreConfig,
    _load_user_config,
    _parse_repo_config,
    _resolve_sqlite_store_path,
    _resolve_store_namespace_id,
    ensure_repo_config_initialized,
    find_fxtr_dir,
)

# The repo-store / discovery layer lives in ``repo_storage_config``; consumers
# that need those names (RepoStoreConfig, find_vcs_root/find_fxtr_dir, the .fxtr
# constants, load_repo_store_config, …) import them from there directly. The one
# exception re-exported here is ``FxtrConfigError`` — the shared config error
# that's co-imported with the git-namespace API throughout the codebase.
__all__ = [
    "FxtrConfigError",
    "NAMESPACE_CONFIG_FILENAME",
    "NAMESPACE_LOCAL_CONFIG_FILENAME",
    "DEFAULT_RESERVED_PREFIX",
    "LEGACY_RESERVED_PREFIX",
    "TsvNamespaceConfig",
    "TsvNamespaceMarker",
    "load_namespace_marker",
    "resolve_repo_config_path",
    "load_config",
    "init_namespace",
    "namespace_exists",
]


# Committed namespace-marker file names
NAMESPACE_CONFIG_FILENAME = "fxtr_namespace.toml"
NAMESPACE_LOCAL_CONFIG_FILENAME = "fxtr_namespace.local.toml"

# Default values for the reserved-prefix (keys without a colon)
DEFAULT_RESERVED_PREFIX = "root"
LEGACY_RESERVED_PREFIX = "index"


def _default_worktree_namespace_id(namespace_root: Path) -> str:
    """Generate the default worktree namespace ID based on store path."""
    return f"worktree:{namespace_root}"


@dataclass(frozen=True)
class TsvNamespaceConfig:
    """Parsed configuration for a tsv namespace.

    This combines information from all configuration layers:
    - ~/.fxtr/config.toml (vcs_root_command)
    - .fxtr/fxtr_store_repo_config.toml (index_namespace_id, sqlite_store_path)
    - fxtr_namespace.toml (reserved_prefix, name, description)
    - fxtr_namespace.local.toml (worktree_namespace_id, repo_config_path)

    The store-only fields (``fxtr_dir``, ``sqlite_store_path``,
    ``index_namespace_id``) live on a composed :class:`RepoStoreConfig` and are
    forwarded via properties so existing call sites keep working.
    """

    namespace_root: Path
    """Path to the directory containing fxtr_namespace.toml."""

    namespace_root_str: str
    """String representation of namespace_root (pre-resolved, avoids repeated str() conversions)."""

    reserved_prefix: str
    """The reserved prefix for keys without colon (default: "root", legacy: "index")."""

    worktree_namespace_id: str
    """Full namespace ID for this worktree's namespace in sqlite."""

    repo_store: RepoStoreConfig
    """The composed repo-entity-store configuration."""

    # Optional metadata (not used programmatically yet)
    name: str | None = None
    description: str | None = None

    @property
    def fxtr_dir(self) -> Path:
        """Path to the .fxtr directory (forwarded from the repo store config)."""
        return self.repo_store.fxtr_dir

    @property
    def sqlite_store_path(self) -> Path:
        """Path to the sqlite store file (forwarded from the repo store config)."""
        return self.repo_store.sqlite_store_path

    @property
    def index_namespace_id(self) -> str:
        """Full namespace ID for the repo entity store (forwarded; the value
        formerly known as the "index namespace" id)."""
        return self.repo_store.store_namespace_id


@dataclass(frozen=True)
class TsvNamespaceMarker:
    """The committed namespace marker (``fxtr_namespace.toml``) on its own.

    This is the subset of :class:`TsvNamespaceConfig` resolvable from the marker
    file *alone* — no ``.fxtr/`` repo-store discovery, no VCS probing, no user
    config, no ``fxtr_namespace.local.toml``. It exists for contexts that only
    have the committed flake source and no repo store: e.g. a pure / fixed-output
    nix build sandbox, where ``.fxtr/`` is gitignored and therefore absent. When
    you also need the repo store (sqlite path, store namespace id), use
    :func:`load_config` instead.
    """

    reserved_prefix: str
    """Reserved prefix for keys without a colon (the only field needed to map
    pin keys to TSV files via :mod:`fxtr2.data.tsv_namespace.key_utils`)."""

    name: str | None = None
    description: str | None = None


def load_namespace_marker(namespace_root: Path) -> TsvNamespaceMarker:
    """Parse ``fxtr_namespace.toml`` alone and return its marker settings.

    Unlike :func:`load_config`, this touches **only** the marker file — it does
    not discover ``.fxtr/``, resolve the sqlite / store-namespace id, read
    ``fxtr_namespace.local.toml``, or probe the VCS root. Safe to call where the
    repo store is unavailable (a nix build sees only the committed flake source).

    Read-only: a legacy marker missing ``reserved_prefix`` reads as the legacy
    default but is **not** rewritten — the self-healing write-back lives in
    :func:`load_config`, which has a writable checkout.

    Raises:
        FxtrConfigError: if ``fxtr_namespace.toml`` is missing or invalid.
    """
    config_path = namespace_root / NAMESPACE_CONFIG_FILENAME
    store_config = _parse_store_config(config_path)
    reserved_prefix = store_config.get("reserved_prefix")
    if reserved_prefix is None:
        reserved_prefix = LEGACY_RESERVED_PREFIX
    return TsvNamespaceMarker(
        reserved_prefix=reserved_prefix,
        name=store_config.get("name"),
        description=store_config.get("description"),
    )


def _parse_store_config(config_path: Path) -> dict[str, Any]:
    """Parse fxtr_namespace.toml file.

    Args:
        config_path: Path to fxtr_namespace.toml.

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
    """Parse fxtr_namespace.local.toml file.

    Args:
        config_path: Path to fxtr_namespace.local.toml.

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


async def resolve_repo_config_path(
    namespace_root: Path,
    local_config: dict[str, Any] | None = None,
    user_config: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Resolve the .fxtr dir and repo config path for a store.

    Checks for an explicit ``repo_config_path`` in local config first,
    falling back to VCS-root discovery.

    Returns:
        (fxtr_dir, repo_config_path)
    """
    if local_config is None:
        local_config_path = namespace_root / NAMESPACE_LOCAL_CONFIG_FILENAME
        local_config = _parse_local_config(local_config_path)

    repo_config_path_str = local_config.get("repo_config_path")
    if repo_config_path_str is not None:
        repo_config_path = Path(repo_config_path_str)
        if not repo_config_path.is_absolute():
            repo_config_path = namespace_root / repo_config_path
        return repo_config_path.parent, repo_config_path

    if user_config is None:
        user_config = _load_user_config()
    fxtr_dir = await find_fxtr_dir(namespace_root, user_config)
    return fxtr_dir, fxtr_dir / REPO_CONFIG_FILENAME


async def load_config(namespace_root: Path) -> TsvNamespaceConfig:
    """Load configuration for an existing tsv namespace.

    Reads configuration from all layers:
    - ~/.fxtr/config.toml (user-level)
    - .fxtr/fxtr_store_repo_config.toml (per-repo)
    - fxtr_namespace.toml (namespace marker)
    - fxtr_namespace.local.toml (namespace marker local, optional)

    Args:
        namespace_root: Path to the directory containing fxtr_namespace.toml.

    Returns:
        TsvNamespaceConfig with all configuration values.

    Raises:
        FxtrConfigError: If configuration is invalid or missing required files.
    """
    namespace_root = namespace_root.resolve()
    config_path = namespace_root / NAMESPACE_CONFIG_FILENAME

    # Parse main namespace-marker config
    store_config = _parse_store_config(config_path)

    # Parse local config (optional - may not exist)
    local_config_path = namespace_root / NAMESPACE_LOCAL_CONFIG_FILENAME
    local_config = _parse_local_config(local_config_path)

    # Load user config (for VCS detection)
    user_config = _load_user_config()

    # Find or use explicit repo config path
    fxtr_dir, repo_config_path = await resolve_repo_config_path(
        namespace_root,
        local_config,
        user_config,
    )

    # Parse per-repo config
    repo_config = _parse_repo_config(repo_config_path)

    # Resolve the store namespace ID + sqlite path (shared with load_repo_store_config)
    index_ns_id = _resolve_store_namespace_id(repo_config, repo_config_path)
    sqlite_store_path = _resolve_sqlite_store_path(repo_config, fxtr_dir)

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
        worktree_ns_id = _default_worktree_namespace_id(namespace_root)

    # Resolve reserved prefix with legacy backfill
    reserved_prefix = store_config.get("reserved_prefix")
    if reserved_prefix is None:
        # Legacy store without explicit prefix — use the old default and write it back
        # so future loads find it. This makes the legacy compat self-healing.
        reserved_prefix = LEGACY_RESERVED_PREFIX
        # Re-read with tomlkit to preserve formatting when writing back
        with open(config_path) as f:
            doc = tomlkit.load(f)
        doc["reserved_prefix"] = reserved_prefix
        with open(config_path, "w") as f:
            tomlkit.dump(doc, f)

    return TsvNamespaceConfig(
        namespace_root=namespace_root,
        namespace_root_str=str(namespace_root),
        reserved_prefix=reserved_prefix,
        worktree_namespace_id=worktree_ns_id,
        repo_store=RepoStoreConfig(
            fxtr_dir=fxtr_dir,
            sqlite_store_path=sqlite_store_path,
            store_namespace_id=index_ns_id,
        ),
        name=store_config.get("name"),
        description=store_config.get("description"),
    )


# .gitignore content for the namespace root directory
_NAMESPACE_ROOT_GITIGNORE = """\
# Fxtr tsv namespace local files (auto-generated)
fxtr_namespace.local.toml

# Sync lock files
*.sync_lock

# Dirty mark directories - ignore contents except the sentinel file
*.fxtr.tsv.dirty/*
!*.fxtr.tsv.dirty/DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md
"""


async def init_namespace(
    namespace_root: Path,
    *,
    reserved_prefix: str = DEFAULT_RESERVED_PREFIX,
    name: str | None = None,
    description: str | None = None,
) -> None:
    """Initialize a new tsv namespace.

    Creates the necessary configuration files and directories:
    - fxtr_namespace.toml (marker config)
    - .gitignore (ignores local files and dirty mark contents)
    - .fxtr/ directory with the per-repo store config

    Args:
        namespace_root: Path where the namespace should be created.
        reserved_prefix: Reserved prefix for keys without colon (default: "root").
        name: Optional human-readable name for the namespace.
        description: Optional description for the namespace.

    Raises:
        FxtrConfigError: If the namespace already exists or initialization fails.
    """
    namespace_root = namespace_root.resolve()
    namespace_root.mkdir(parents=True, exist_ok=True)
    config_path = namespace_root / NAMESPACE_CONFIG_FILENAME

    # Check if namespace already exists
    if config_path.exists():
        raise FxtrConfigError(
            f"Namespace already exists at {namespace_root}\n"
            "Use load_config() to open an existing namespace."
        )

    # Load user config and find .fxtr directory (validates VCS setup)
    user_config = _load_user_config()
    fxtr_dir = await find_fxtr_dir(namespace_root, user_config)

    # Create .fxtr directory + per-repo config + .fxtr/.gitignore (idempotent)
    ensure_repo_config_initialized(fxtr_dir)

    # Create store root directory if needed
    namespace_root.mkdir(parents=True, exist_ok=True)

    # Write fxtr_namespace.toml
    store_config: dict[str, Any] = {}
    store_config["reserved_prefix"] = reserved_prefix
    if name is not None:
        store_config["name"] = name
    if description is not None:
        store_config["description"] = description

    with open(config_path, "w") as f:
        tomlkit.dump(store_config, f)

    # Write .gitignore in store root
    gitignore_path = namespace_root / ".gitignore"
    # Append to existing or create new
    if gitignore_path.exists():
        existing = gitignore_path.read_text()
        if "fxtr_namespace.local.toml" not in existing:
            with open(gitignore_path, "a") as f:
                f.write("\n" + _NAMESPACE_ROOT_GITIGNORE)
    else:
        gitignore_path.write_text(_NAMESPACE_ROOT_GITIGNORE)


def namespace_exists(namespace_root: Path) -> bool:
    """Check if a fxtr namespace exists at the given path.

    Args:
        namespace_root: Path to check.

    Returns:
        True if fxtr_namespace.toml exists at the path.
    """
    return (namespace_root / NAMESPACE_CONFIG_FILENAME).exists()
