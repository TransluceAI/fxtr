"""Tests for fxtrconfig parsing functionality."""

import tempfile
from pathlib import Path

import pytest

from fxtr.api.fxtrconfig import (
    AuthorConfig,
    BlueprintConfig,
    DataConfig,
    FxtrConfig,
    ProviderConfig,
    ResolverConfig,
    SecretsConfig,
    SourceConfig,
    UvProviderConfig,
    parse_fxtr_config,
)


def test_parse_fxtr_config_complete():
    """Test parsing a complete fxtr.toml config file."""
    config_content = """
[author]
name = "Daniel Johnson"
email = "daniel@transluce.org"

[blueprint.sources.default]
module = "demo_09_15_user"

[blueprint.sources.demo_09_15_defns]
module = "demo_09_15_defns"

[data]
active = "../data_gitignore/"

[resolver]
trust_impl_code = true
default_py_provider = "uv"

[resolver.secrets]

[provider.uv]
virtual_env_dir = "../data_gitignore/tmp_venvs"
dynamic_source_packages = ["fxtr"]
local_source_packages = {demo_09_15 = "../lib/pyimpl"}
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(config_content)
        temp_path = f.name

    try:
        config = parse_fxtr_config(temp_path)

        # Test author config
        assert config.author.name == "Daniel Johnson"
        assert config.author.email == "daniel@transluce.org"

        # Test blueprint sources
        assert "default" in config.blueprint.sources
        assert "demo_09_15_defns" in config.blueprint.sources
        assert config.blueprint.sources["default"].module == "demo_09_15_user"
        assert config.blueprint.sources["demo_09_15_defns"].module == "demo_09_15_defns"

        # Test data config
        assert config.data.active == "../data_gitignore/"

        # Test resolver config
        assert config.resolver.trust_impl_code is True
        assert config.resolver.default_py_provider == "uv"

        # Test provider config
        assert config.provider.uv.virtual_env_dir == "../data_gitignore/tmp_venvs"
        assert config.provider.uv.dynamic_source_packages == ["fxtr"]
        assert config.provider.uv.local_source_packages == {"demo_09_15": "../lib/pyimpl"}

    finally:
        Path(temp_path).unlink()


def test_parse_fxtr_config_with_actual_demo_file():
    """Test parsing the actual demo fxtr.toml file."""
    demo_config_path = (
        Path(__file__).parent / "fxtr" / "examples" / "demo_09_15" / "user" / "fxtr.toml"
    )

    if demo_config_path.exists():
        config = parse_fxtr_config(demo_config_path)

        # Basic validation that it parses without error
        assert isinstance(config, FxtrConfig)
        assert isinstance(config.author, AuthorConfig)
        assert isinstance(config.blueprint, BlueprintConfig)
        assert isinstance(config.data, DataConfig)
        assert isinstance(config.resolver, ResolverConfig)
        assert isinstance(config.provider, ProviderConfig)
        assert isinstance(config.provider.uv, UvProviderConfig)


def test_parse_fxtr_config_missing_file():
    """Test that parsing a non-existent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        parse_fxtr_config("/path/that/does/not/exist.toml")


def test_parse_fxtr_config_invalid_toml():
    """Test that parsing invalid TOML raises an appropriate error."""
    invalid_content = """
[author
name = "Invalid TOML"
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(invalid_content)
        temp_path = f.name

    try:
        with pytest.raises(Exception):  # tomlkit will raise some parsing error
            parse_fxtr_config(temp_path)
    finally:
        Path(temp_path).unlink()


def test_parse_fxtr_config_missing_required_fields():
    """Test that parsing config with missing required fields raises ValidationError."""
    incomplete_content = """
[author]
name = "Test User"
# missing email field

[blueprint.sources.test]
# missing module field

[data]
active = "../data/"
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(incomplete_content)
        temp_path = f.name

    try:
        with pytest.raises(Exception):  # msgspec will raise ValidationError
            parse_fxtr_config(temp_path)
    finally:
        Path(temp_path).unlink()


def test_individual_struct_creation():
    """Test creating individual config structs directly."""
    author = AuthorConfig(name="Test User", email="test@example.com")
    assert author.name == "Test User"
    assert author.email == "test@example.com"

    source = SourceConfig(module="test_module")
    assert source.module == "test_module"

    blueprint = BlueprintConfig(sources={"test": source})
    assert "test" in blueprint.sources
    assert blueprint.sources["test"].module == "test_module"

    data = DataConfig(active="../data/")
    assert data.active == "../data/"

    secrets = SecretsConfig(use_env_file=False)
    resolver = ResolverConfig(trust_impl_code=False, default_py_provider="uv", secrets=secrets)

    assert resolver.trust_impl_code is False
    assert resolver.default_py_provider == "uv"
    assert resolver.secrets == secrets

    uv_provider = UvProviderConfig(
        virtual_env_dir="../venvs",
        dynamic_source_packages=["pkg1", "pkg2"],
        local_source_packages={"local": "../local"},
    )
    assert uv_provider.virtual_env_dir == "../venvs"
    assert uv_provider.dynamic_source_packages == ["pkg1", "pkg2"]
    assert uv_provider.local_source_packages == {"local": "../local"}

    provider = ProviderConfig(uv=uv_provider)
    assert provider.uv == uv_provider

    config = FxtrConfig(
        author=author,
        blueprint=blueprint,
        data=data,
        resolver=resolver,
        provider=provider,
        config_path=Path("/test/path.toml"),
    )
    assert config.author == author
    assert config.blueprint == blueprint
    assert config.data == data
    assert config.resolver == resolver
    assert config.provider == provider
    assert config.config_path == Path("/test/path.toml")


def test_parse_fxtr_config_with_custom_uv_config():
    """Test parsing fxtr.toml with custom UV configuration."""
    config_content = """
[author]
name = "Test User"
email = "test@example.com"

[blueprint.sources.test]
module = "test_module"

[data]
active = "../data/"

[resolver]
trust_impl_code = true
default_py_provider = "uv"

[resolver.secrets]
use_env_file = false

[provider.uv]
virtual_env_dir = "../venvs"
dynamic_source_packages = ["fxtr"]
local_source_packages = {test = "../test"}

[provider.uv.custom_uv_config]
override-dependencies = ["numpy==1.24.0"]
some-flag = true
sources.existing_package = {url = "git+https://github.com/example/repo.git"}
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(config_content)
        temp_path = f.name

    try:
        config = parse_fxtr_config(temp_path)

        # Test basic config parsing
        assert config.author.name == "Test User"
        assert config.provider.uv.virtual_env_dir == "../venvs"
        assert config.provider.uv.dynamic_source_packages == ["fxtr"]
        assert config.provider.uv.local_source_packages == {"test": "../test"}

        # Test custom UV config parsing
        assert "override-dependencies" in config.provider.uv.custom_uv_config
        assert "some-flag" in config.provider.uv.custom_uv_config
        assert "sources" in config.provider.uv.custom_uv_config

        assert config.provider.uv.custom_uv_config["override-dependencies"] == ["numpy==1.24.0"]
        assert config.provider.uv.custom_uv_config["some-flag"] is True

        sources = config.provider.uv.custom_uv_config["sources"]
        assert isinstance(sources, dict)
        assert "existing_package" in sources
        assert sources["existing_package"]["url"] == "git+https://github.com/example/repo.git"

    finally:
        Path(temp_path).unlink()
