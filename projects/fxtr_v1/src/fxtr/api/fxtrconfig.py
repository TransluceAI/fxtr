import pathlib
from pathlib import Path
from typing import Any, Literal

import msgspec
import tomlkit


class AuthorConfig(msgspec.Struct, kw_only=True):
    """Author information configuration."""

    name: str
    email: str


class SourceConfig(msgspec.Struct, kw_only=True):
    """Source configuration with Python module entry-point."""

    module: str  # Python entry-point string like "my.module" or "my.module:attr"


class BlueprintConfig(msgspec.Struct, kw_only=True):
    """Blueprint configuration containing sources."""

    sources: dict[str, SourceConfig]


class DataConfig(msgspec.Struct, kw_only=True):
    """Data configuration with active path."""

    active: str
    # How to log materialization records to the state directory:
    # - "per_file" (default): one `{timestamp}.json` pointer file per materialization request.
    # - "jsonl": append a summary line to a single `materializations.jsonl`, skip cache-only
    #   requests entirely, and dump full tracebacks of failed requests to their own files.
    materialization_log_mode: Literal["per_file", "jsonl"] = "per_file"
    # How entity data is stored on disk:
    # - "cbor_files" (default): one CID-addressed `.cbor` file per entity under `<active>/entities`.
    # - "lmdb": a single LMDB database at `<active>/entities.lmdb` (avoids inode exhaustion on large
    #   stores). Switch by migrating with scripts/migrate_entities_to_lmdb.py, then setting this.
    entity_store_format: Literal["cbor_files", "lmdb"] = "cbor_files"
    # Maximum LMDB map size in GiB (only used when entity_store_format == "lmdb"). Address space
    # only (sparse), so over-provisioning is cheap; bump it if a store outgrows it.
    lmdb_map_size_gb: int = 512


class SecretsConfig(msgspec.Struct, kw_only=True):
    """Secrets configuration."""

    use_env_file: bool = False


class ResolverConfig(msgspec.Struct, kw_only=True):
    """Resolver configuration with various options."""

    trust_impl_code: bool
    default_py_provider: str
    secrets: SecretsConfig = msgspec.field(default_factory=lambda: SecretsConfig())


class UvProviderConfig(msgspec.Struct, kw_only=True):
    """UV provider configuration."""

    virtual_env_dir: str
    dynamic_source_packages: list[str]
    local_source_packages: dict[str, str]
    custom_uv_config: dict[str, Any] = msgspec.field(default_factory=dict)


class ProviderConfig(msgspec.Struct, kw_only=True):
    """Provider configuration containing UV settings."""

    uv: UvProviderConfig


class FxtrConfig(msgspec.Struct, kw_only=True):
    """Complete fxtr configuration structure."""

    author: AuthorConfig
    blueprint: BlueprintConfig
    data: DataConfig
    resolver: ResolverConfig
    provider: ProviderConfig
    config_path: Path


def parse_fxtr_config(config_path: str | Path) -> FxtrConfig:
    """Parse a fxtr TOML config file into a FxtrConfig struct.

    Args:
        config_path: Path to the TOML config file

    Returns:
        Parsed FxtrConfig struct with config_path field populated
    """
    config_path = Path(config_path).resolve()

    with open(config_path, "rb") as f:
        toml_data = tomlkit.load(f)

    # Convert the TOML data to the struct, then add the config path
    config_dict = toml_data.unwrap()
    config_dict["config_path"] = config_path

    return msgspec.convert(config_dict, FxtrConfig)


def get_active_entity_store(config: FxtrConfig) -> pathlib.Path:
    """Get the path to the active entity store (the CBOR-file directory)."""
    return pathlib.Path(config.data.active) / "entities"


def get_active_entity_store_lmdb(config: FxtrConfig) -> pathlib.Path:
    """Get the path to the active LMDB entity store directory.

    Distinct from `get_active_entity_store` so a migrated LMDB store and the legacy `entities/`
    directory can coexist on disk during a cutover.
    """
    return pathlib.Path(config.data.active) / "entities.lmdb"


def get_active_state_store(config: FxtrConfig) -> pathlib.Path:
    """Get the path to the active state store."""
    return pathlib.Path(config.data.active) / "state"


def get_default_collection_name(config: FxtrConfig) -> str:
    """Get the default collection name from config.

    Args:
        config: The fxtr config

    Returns:
        The name of the default collection source

    Raises:
        RuntimeError: If no "default" source exists in the config
    """
    if "default" in config.blueprint.sources:
        return "default"

    # No default source found - provide a helpful error
    available_sources = list(config.blueprint.sources.keys())
    if not available_sources:
        raise RuntimeError(
            "No 'default' collection source found in config, and no other sources available.\n"
            "Please add a collection source named 'default' to your fxtr.toml config file."
        )
    else:
        sources_list = ", ".join(f"'{s}'" for s in available_sources)
        raise RuntimeError(
            "No 'default' collection source found in config.\n"
            f"Available sources: {sources_list}\n"
            "Please add a collection source named 'default' to your fxtr.toml config file,\n"
            "or specify a collection explicitly with -c/--collection."
        )
