"""Tests for tsv namespace configuration management."""

import uuid
from pathlib import Path

import anyio
import pytest

from fxtr2.data.repo_storage_config import (
    DEFAULT_SQLITE_STORE_FILENAME,
    FXTR_DIR_NAME,
    INDEX_NAMESPACE_PREFIX,
    REPO_CONFIG_FILENAME,
    find_fxtr_dir,
    find_vcs_root,
)
from fxtr2.data.tsv_namespace.config import (
    DEFAULT_RESERVED_PREFIX,
    LEGACY_RESERVED_PREFIX,
    NAMESPACE_CONFIG_FILENAME,
    NAMESPACE_LOCAL_CONFIG_FILENAME,
    FxtrConfigError,
    TsvNamespaceConfig,
    TsvNamespaceMarker,
    init_namespace,
    load_config,
    load_namespace_marker,
    namespace_exists,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Monkeypatch Path.home() to return a temp directory.

    This prevents tests from creating files in the real home directory.
    """
    fake_home_dir = tmp_path / "fake_home"
    fake_home_dir.mkdir()

    # Monkeypatch Path.home to return our fake home
    monkeypatch.setattr(Path, "home", lambda: fake_home_dir)

    return fake_home_dir


async def create_git_repo(path: Path) -> None:
    """Initialize a git repository at the given path."""
    path.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=path, check=True)
    # Configure git so commits work
    await anyio.run_process(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path,
        check=True,
    )
    await anyio.run_process(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
    )


class TestFindVcsRoot:
    """Tests for find_vcs_root (VCS root detection)."""

    async def test_find_vcs_root_in_git_repo(self, tmp_path: Path):
        """Should find the VCS root (parent of .git) in a simple git repo."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        vcs_root = await find_vcs_root(repo_path)
        assert vcs_root == repo_path

    async def test_find_vcs_root_in_subdirectory(self, tmp_path: Path):
        """Should find VCS root from a subdirectory."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        subdir = repo_path / "src" / "lib"
        subdir.mkdir(parents=True)

        vcs_root = await find_vcs_root(subdir)
        assert vcs_root == repo_path

    async def test_find_vcs_root_not_in_repo_gives_helpful_error(self, tmp_path: Path):
        """Should raise error with helpful suggestions when not in a VCS."""
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()

        with pytest.raises(FxtrConfigError) as exc_info:
            await find_vcs_root(not_a_repo)

        error_msg = str(exc_info.value).lower()
        # Should suggest solutions
        assert "git repository" in error_msg or "vcs root" in error_msg
        assert "vcs_root_command" in error_msg
        assert "repo_config_path" in error_msg

    async def test_find_vcs_root_with_custom_command(self, tmp_path: Path, fake_home: Path):
        """Should use custom vcs_root_command if configured."""
        # Create a user config with a custom command
        user_config_dir = fake_home / FXTR_DIR_NAME
        user_config_dir.mkdir()

        # Create a script that outputs a specific path
        expected_root = tmp_path / "custom_vcs_root"
        expected_root.mkdir()

        user_config = user_config_dir / "config.toml"
        # Use echo to output the path (this works on most systems)
        user_config.write_text(f'vcs_root_command = ["echo", "{expected_root}"]\n')

        test_path = tmp_path / "some_path"
        test_path.mkdir()

        vcs_root = await find_vcs_root(test_path)
        assert vcs_root == expected_root

    async def test_find_vcs_root_custom_command_must_output_absolute_path(
        self, tmp_path: Path, fake_home: Path
    ):
        """Custom vcs_root_command must output an absolute path."""
        user_config_dir = fake_home / FXTR_DIR_NAME
        user_config_dir.mkdir()

        user_config = user_config_dir / "config.toml"
        # Output a relative path
        user_config.write_text('vcs_root_command = ["echo", "relative/path"]\n')

        test_path = tmp_path / "some_path"
        test_path.mkdir()

        with pytest.raises(FxtrConfigError) as exc_info:
            await find_vcs_root(test_path)

        assert "absolute path" in str(exc_info.value).lower()

    async def test_find_vcs_root_command_must_be_list(self, tmp_path: Path, fake_home: Path):
        """vcs_root_command must be a list, not a string."""
        user_config_dir = fake_home / FXTR_DIR_NAME
        user_config_dir.mkdir()

        user_config = user_config_dir / "config.toml"
        user_config.write_text('vcs_root_command = "hg root"\n')

        test_path = tmp_path / "some_path"
        test_path.mkdir()

        with pytest.raises(FxtrConfigError) as exc_info:
            await find_vcs_root(test_path)

        assert "list" in str(exc_info.value).lower()


class TestFindFxtrDir:
    """Tests for find_fxtr_dir."""

    async def test_find_fxtr_dir_is_sibling_to_vcs_root(self, tmp_path: Path):
        """The .fxtr dir should be sibling to the VCS root."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        fxtr_dir = await find_fxtr_dir(store_path)
        assert fxtr_dir == repo_path / FXTR_DIR_NAME


class TestInitStore:
    """Tests for init_namespace."""

    async def test_init_creates_store_config(self, tmp_path: Path):
        """init_namespace should create fxtr_namespace.toml."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        config_path = store_path / NAMESPACE_CONFIG_FILENAME
        assert config_path.exists()

    async def test_init_creates_gitignore(self, tmp_path: Path):
        """init_namespace should create .gitignore in store directory."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        gitignore_path = store_path / ".gitignore"
        assert gitignore_path.exists()

        content = gitignore_path.read_text()
        assert "fxtr_namespace.local.toml" in content
        assert "*.sync_lock" in content
        assert "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md" in content

    async def test_init_creates_fxtr_dir_with_config_toml(self, tmp_path: Path):
        """init_namespace should create .fxtr directory with config.toml."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        fxtr_dir = repo_path / FXTR_DIR_NAME
        assert fxtr_dir.exists()

        config_path = fxtr_dir / REPO_CONFIG_FILENAME
        assert config_path.exists()

        # Should contain a valid namespace ID
        import tomllib

        with open(config_path, "rb") as f:
            repo_config = tomllib.load(f)

        ns_id = repo_config.get("index_namespace_id")
        assert ns_id is not None
        assert ns_id.startswith(INDEX_NAMESPACE_PREFIX)

    async def test_init_creates_fxtr_dir_gitignore(self, tmp_path: Path):
        """init_namespace should create .gitignore in .fxtr directory."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        fxtr_dir = repo_path / FXTR_DIR_NAME
        gitignore_path = fxtr_dir / ".gitignore"
        assert gitignore_path.exists()

        content = gitignore_path.read_text()
        assert "*" in content  # Should ignore everything

    async def test_init_fails_if_already_exists(self, tmp_path: Path):
        """init_namespace should fail if store already exists."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        with pytest.raises(FxtrConfigError) as exc_info:
            await init_namespace(store_path)

        assert "already exists" in str(exc_info.value).lower()

    async def test_init_with_custom_reserved_prefix(self, tmp_path: Path):
        """init_namespace should respect custom reserved_prefix."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path, reserved_prefix="default")

        config = await load_config(store_path)
        assert config.reserved_prefix == "default"

    async def test_init_with_name_and_description(self, tmp_path: Path):
        """init_namespace should save name and description."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(
            store_path,
            name="Test Store",
            description="A test store for testing",
        )

        config = await load_config(store_path)
        assert config.name == "Test Store"
        assert config.description == "A test store for testing"

    async def test_init_reuses_existing_index_namespace_id(self, tmp_path: Path):
        """init_namespace should reuse existing index_namespace_id from config.toml."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        # Create .fxtr dir with existing config containing namespace ID
        fxtr_dir = repo_path / FXTR_DIR_NAME
        fxtr_dir.mkdir()
        existing_id = f"{INDEX_NAMESPACE_PREFIX}{uuid.uuid4()}"
        config_path = fxtr_dir / REPO_CONFIG_FILENAME
        config_path.write_text(f'index_namespace_id = "{existing_id}"\n')

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        config = await load_config(store_path)
        assert config.index_namespace_id == existing_id


class TestLoadConfig:
    """Tests for load_config."""

    async def test_load_config_returns_config_object(self, tmp_path: Path):
        """load_config should return a TsvNamespaceConfig object."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)
        config = await load_config(store_path)

        assert isinstance(config, TsvNamespaceConfig)
        assert config.namespace_root == store_path.resolve()
        assert config.reserved_prefix == DEFAULT_RESERVED_PREFIX

    async def test_load_config_uses_path_based_worktree_namespace_id(self, tmp_path: Path):
        """load_config should use path-based worktree_namespace_id by default."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        # Ensure local config doesn't exist
        local_config_path = store_path / NAMESPACE_LOCAL_CONFIG_FILENAME
        assert not local_config_path.exists()

        config = await load_config(store_path)

        # Should NOT have created local config
        assert not local_config_path.exists()
        # Should use path-based namespace ID
        assert config.worktree_namespace_id == f"worktree:{store_path.resolve()}"

    async def test_load_config_worktree_namespace_id_is_consistent(self, tmp_path: Path):
        """load_config should return same worktree_namespace_id across loads."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        # Multiple loads should return same ID (path-based default)
        config1 = await load_config(store_path)
        config2 = await load_config(store_path)

        assert config1.worktree_namespace_id == config2.worktree_namespace_id

    async def test_load_config_respects_worktree_namespace_id_override(self, tmp_path: Path):
        """load_config should respect worktree_namespace_id override in local config."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        # Create local config with custom worktree namespace ID
        local_config_path = store_path / NAMESPACE_LOCAL_CONFIG_FILENAME
        local_config_path.write_text('worktree_namespace_id = "my_custom_namespace"\n')

        config = await load_config(store_path)

        # Should use the override
        assert config.worktree_namespace_id == "my_custom_namespace"

    async def test_load_config_fails_if_no_store(self, tmp_path: Path):
        """load_config should fail if fxtr_namespace.toml doesn't exist."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        with pytest.raises(FxtrConfigError) as exc_info:
            await load_config(store_path)

        assert "not found" in str(exc_info.value).lower()

    async def test_load_config_fails_if_repo_config_missing(self, tmp_path: Path):
        """load_config should fail if .fxtr/config.toml is missing."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        # Delete the repo config
        fxtr_dir = repo_path / FXTR_DIR_NAME
        config_path = fxtr_dir / REPO_CONFIG_FILENAME
        config_path.unlink()

        with pytest.raises(FxtrConfigError) as exc_info:
            await load_config(store_path)

        assert "config" in str(exc_info.value).lower()

    async def test_load_config_fails_if_index_namespace_id_missing(self, tmp_path: Path):
        """load_config should fail if index_namespace_id is missing from config."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        # Overwrite the config without the ID
        fxtr_dir = repo_path / FXTR_DIR_NAME
        config_path = fxtr_dir / REPO_CONFIG_FILENAME
        config_path.write_text("# empty config\n")

        with pytest.raises(FxtrConfigError) as exc_info:
            await load_config(store_path)

        assert "index_namespace_id" in str(exc_info.value).lower()

    async def test_load_config_namespace_ids(self, tmp_path: Path):
        """load_config should provide proper namespace IDs."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)
        config = await load_config(store_path)

        # Check namespace ID format
        assert config.index_namespace_id.startswith(INDEX_NAMESPACE_PREFIX)
        assert config.worktree_namespace_id.startswith("worktree:")

    async def test_load_config_sqlite_store_path_default(self, tmp_path: Path):
        """load_config should use default sqlite store path in .fxtr directory."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)
        config = await load_config(store_path)

        # Default sqlite path is now in .fxtr directory
        expected_path = repo_path / FXTR_DIR_NAME / DEFAULT_SQLITE_STORE_FILENAME
        assert config.sqlite_store_path == expected_path

    async def test_load_config_sqlite_store_path_from_repo_config(self, tmp_path: Path):
        """load_config should read sqlite_store_path from .fxtr/config.toml."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        # Modify repo config to add custom sqlite path
        fxtr_dir = repo_path / FXTR_DIR_NAME
        config_path = fxtr_dir / REPO_CONFIG_FILENAME

        import tomllib

        with open(config_path, "rb") as f:
            repo_config = tomllib.load(f)

        custom_path = tmp_path / "custom" / "store.sqlite"
        repo_config["sqlite_store_path"] = str(custom_path)

        import tomlkit

        with open(config_path, "w") as f:
            tomlkit.dump(repo_config, f)

        config = await load_config(store_path)
        assert config.sqlite_store_path == custom_path

    async def test_load_config_legacy_backfill_reserved_prefix(self, tmp_path: Path):
        """load_config should backfill reserved_prefix='index' for legacy stores missing it."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        # Create a store normally (which will have reserved_prefix set)
        await init_namespace(store_path)

        # Manually remove the reserved_prefix key from fxtr_namespace.toml to simulate a legacy store
        config_path = store_path / NAMESPACE_CONFIG_FILENAME
        import tomlkit

        with open(config_path) as f:
            doc = tomlkit.load(f)
        del doc["reserved_prefix"]
        with open(config_path, "w") as f:
            tomlkit.dump(doc, f)

        # Verify reserved_prefix is gone
        import tomllib

        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        assert "reserved_prefix" not in raw

        # load_config should return the legacy prefix
        config = await load_config(store_path)
        assert config.reserved_prefix == LEGACY_RESERVED_PREFIX
        assert config.reserved_prefix == "index"

        # The backfill should have written reserved_prefix back to fxtr_namespace.toml
        with open(config_path, "rb") as f:
            raw_after = tomllib.load(f)
        assert raw_after["reserved_prefix"] == "index"

    async def test_load_config_sqlite_store_path_relative_to_fxtr_dir(self, tmp_path: Path):
        """Relative sqlite_store_path should be relative to .fxtr dir."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        # Modify repo config to add relative sqlite path
        fxtr_dir = repo_path / FXTR_DIR_NAME
        config_path = fxtr_dir / REPO_CONFIG_FILENAME

        import tomllib

        with open(config_path, "rb") as f:
            repo_config = tomllib.load(f)

        repo_config["sqlite_store_path"] = "my_store.sqlite"

        import tomlkit

        with open(config_path, "w") as f:
            tomlkit.dump(repo_config, f)

        config = await load_config(store_path)
        assert config.sqlite_store_path == fxtr_dir / "my_store.sqlite"


class TestLoadNamespaceMarker:
    """Tests for load_namespace_marker (the marker-only parse).

    These deliberately do **not** create a git repo or a ``.fxtr/`` dir — the
    whole point is that the marker resolves with only ``fxtr_namespace.toml``
    present (the situation inside a pure/FOD nix build sandbox).
    """

    def test_reads_prefix_without_fxtr_dir_or_vcs(self, tmp_path: Path):
        """Resolves reserved_prefix from the marker alone — no .fxtr/, no git."""
        ns = tmp_path / "ns"
        ns.mkdir()
        (ns / NAMESPACE_CONFIG_FILENAME).write_text(
            'reserved_prefix = "data"\nname = "demo"\ndescription = "d"\n'
        )

        marker = load_namespace_marker(ns)

        assert isinstance(marker, TsvNamespaceMarker)
        assert marker.reserved_prefix == "data"
        assert marker.name == "demo"
        assert marker.description == "d"
        # Sanity: no .fxtr/ was created or required.
        assert not (ns / ".fxtr").exists()

    def test_missing_prefix_reads_as_legacy_without_writeback(self, tmp_path: Path):
        """A marker missing reserved_prefix reads as the legacy default and is
        NOT rewritten (unlike load_config, which self-heals)."""
        ns = tmp_path / "ns"
        ns.mkdir()
        config_path = ns / NAMESPACE_CONFIG_FILENAME
        config_path.write_text('name = "legacy"\n')

        marker = load_namespace_marker(ns)
        assert marker.reserved_prefix == LEGACY_RESERVED_PREFIX

        # The file must be untouched — no write-back side effect.
        import tomllib

        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        assert "reserved_prefix" not in raw

    def test_matches_init_default(self, tmp_path: Path):
        """The marker prefix matches what init_namespace wrote (no divergence)."""
        ns = tmp_path / "ns"
        ns.mkdir()
        (ns / NAMESPACE_CONFIG_FILENAME).write_text(
            f'reserved_prefix = "{DEFAULT_RESERVED_PREFIX}"\n'
        )

        marker = load_namespace_marker(ns)
        assert marker.reserved_prefix == DEFAULT_RESERVED_PREFIX

    def test_missing_marker_file_raises(self, tmp_path: Path):
        """A missing fxtr_namespace.toml raises FxtrConfigError."""
        ns = tmp_path / "ns"
        ns.mkdir()

        with pytest.raises(FxtrConfigError) as exc_info:
            load_namespace_marker(ns)

        assert "not found" in str(exc_info.value).lower()


class TestRepoConfigPath:
    """Tests for explicit repo_config_path in local config."""

    async def test_load_config_with_explicit_repo_config_path(self, tmp_path: Path):
        """load_config should use repo_config_path if set, skipping VCS detection."""
        # Create a directory that is NOT a git repo
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()

        # Create store config manually
        (not_a_repo / NAMESPACE_CONFIG_FILENAME).write_text("")

        # Create per-repo config in a custom location
        custom_fxtr_dir = tmp_path / "custom_fxtr"
        custom_fxtr_dir.mkdir()
        repo_config_path = custom_fxtr_dir / REPO_CONFIG_FILENAME
        index_ns_id = f"{INDEX_NAMESPACE_PREFIX}{uuid.uuid4()}"
        repo_config_path.write_text(f'index_namespace_id = "{index_ns_id}"\n')

        # Create local config pointing to the custom repo config
        local_config_path = not_a_repo / NAMESPACE_LOCAL_CONFIG_FILENAME
        local_config_path.write_text(f'repo_config_path = "{repo_config_path}"\n')

        # This should work even though not_a_repo is not a git repo
        config = await load_config(not_a_repo)

        assert config.index_namespace_id == index_ns_id
        assert config.fxtr_dir == custom_fxtr_dir

    async def test_load_config_repo_config_path_relative(self, tmp_path: Path):
        """repo_config_path can be relative to namespace_root."""
        # Create a directory that is NOT a git repo
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()

        # Create store config manually
        (not_a_repo / NAMESPACE_CONFIG_FILENAME).write_text("")

        # Create per-repo config in a subdirectory
        custom_fxtr_dir = not_a_repo / "custom_fxtr"
        custom_fxtr_dir.mkdir()
        repo_config_path = custom_fxtr_dir / REPO_CONFIG_FILENAME
        index_ns_id = f"{INDEX_NAMESPACE_PREFIX}{uuid.uuid4()}"
        repo_config_path.write_text(f'index_namespace_id = "{index_ns_id}"\n')

        # Create local config with relative path
        local_config_path = not_a_repo / NAMESPACE_LOCAL_CONFIG_FILENAME
        local_config_path.write_text(
            'repo_config_path = "custom_fxtr/fxtr_store_repo_config.toml"\n'
        )

        config = await load_config(not_a_repo)

        assert config.index_namespace_id == index_ns_id
        assert config.fxtr_dir == custom_fxtr_dir


class TestStoreExists:
    """Tests for namespace_exists."""

    async def test_store_exists_returns_true_for_existing_store(self, tmp_path: Path):
        """namespace_exists should return True for initialized stores."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        assert namespace_exists(store_path) is True

    def test_store_exists_returns_false_for_nonexistent(self, tmp_path: Path):
        """namespace_exists should return False for directories without store."""
        store_path = tmp_path / "not_a_store"
        store_path.mkdir()

        assert namespace_exists(store_path) is False

    def test_store_exists_returns_false_for_missing_directory(self, tmp_path: Path):
        """namespace_exists should return False for missing directories."""
        store_path = tmp_path / "does_not_exist"

        assert namespace_exists(store_path) is False


class TestGitignoreContent:
    """Tests for the generated .gitignore content."""

    async def test_gitignore_ignores_local_config(self, tmp_path: Path):
        """The .gitignore should ignore fxtr_namespace.local.toml."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        gitignore_content = (store_path / ".gitignore").read_text()
        assert "fxtr_namespace.local.toml" in gitignore_content

    async def test_gitignore_ignores_sync_lock(self, tmp_path: Path):
        """The .gitignore should ignore sync lock files."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        gitignore_content = (store_path / ".gitignore").read_text()
        assert "*.sync_lock" in gitignore_content

    async def test_gitignore_ignores_dirty_dir_contents_except_sentinel(self, tmp_path: Path):
        """The .gitignore should ignore dirty dir contents but not sentinel."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        await init_namespace(store_path)

        gitignore_content = (store_path / ".gitignore").read_text()
        # Should ignore dirty dir contents
        assert "*.fxtr.tsv.dirty/*" in gitignore_content
        # But not the sentinel
        assert "!*.fxtr.tsv.dirty/DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md" in gitignore_content

    async def test_gitignore_appends_to_existing(self, tmp_path: Path):
        """init_namespace should append to existing .gitignore."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        # Create existing .gitignore
        existing_content = "# My existing rules\n*.pyc\n"
        (store_path / ".gitignore").write_text(existing_content)

        await init_namespace(store_path)

        gitignore_content = (store_path / ".gitignore").read_text()
        # Should contain both old and new content
        assert "*.pyc" in gitignore_content
        assert "fxtr_namespace.local.toml" in gitignore_content

    async def test_gitignore_not_duplicated(self, tmp_path: Path):
        """init_namespace should not duplicate gitignore content if already present."""
        repo_path = tmp_path / "repo"
        await create_git_repo(repo_path)

        store_path = repo_path / "data"
        store_path.mkdir()

        # Create existing .gitignore that already has fxtr content
        existing_content = "fxtr_namespace.local.toml\n"
        (store_path / ".gitignore").write_text(existing_content)

        await init_namespace(store_path)

        gitignore_content = (store_path / ".gitignore").read_text()
        # Should not have duplicated the content
        assert gitignore_content.count("fxtr_namespace.local.toml") == 1
