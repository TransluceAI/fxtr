"""Tests for the fxtr-git-store CLI.

Uses Typer's CliRunner for in-process testing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from fxtr2.cli.git_store_cli import app
from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.git_store import GitNamespaceBackend
from fxtr2.data.git_store.config import store_exists
from fxtr2.data.git_store.dirty import dirty_dir_for_tsv
from fxtr2.data.memory_arena import MemoryArena, MemoryStore

runner = CliRunner()


def create_test_entity(memory_arena: MemoryArena, value: str) -> FxtrID:
    """Create a simple test entity and store it in the arena."""
    from fxtr2.data.entity_core import FxtrCbor

    ground_context = FxtrID.from_tag_and_payload(".", b"")
    type_id = FxtrID.builtin("test_entity")
    memory_arena.track_builtin(type_id)

    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        "value": value,
    }
    return memory_arena.store(entity)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repository."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


@pytest.fixture
def initialized_store(git_repo: Path) -> Path:
    """Create an initialized fxtr store in a git repo."""
    anyio.run(lambda: GitNamespaceBackend.init(git_repo))
    return git_repo


class TestInit:
    """Tests for the init command."""

    def test_init_creates_store(self, git_repo: Path) -> None:
        """Test that init creates a fxtr store."""
        result = runner.invoke(app, ["init", str(git_repo)])

        assert result.exit_code == 0
        assert "Initialized fxtr store" in result.output
        assert store_exists(git_repo)
        assert (git_repo / "fxtr_store.toml").exists()

    def test_init_with_reserved_prefix(self, git_repo: Path) -> None:
        """Test that init accepts a custom reserved prefix."""
        result = runner.invoke(app, ["init", "--reserved-prefix", "pins", str(git_repo)])

        assert result.exit_code == 0
        content = (git_repo / "fxtr_store.toml").read_text()
        assert 'reserved_prefix = "pins"' in content

    def test_init_with_name_and_description(self, git_repo: Path) -> None:
        """Test that init accepts name and description."""
        result = runner.invoke(
            app,
            ["init", "--name", "Test Store", "--description", "A test store", str(git_repo)],
        )

        assert result.exit_code == 0
        content = (git_repo / "fxtr_store.toml").read_text()
        assert 'name = "Test Store"' in content
        assert 'description = "A test store"' in content

    def test_init_fails_if_store_exists(self, initialized_store: Path) -> None:
        """Test that init fails if store already exists."""
        result = runner.invoke(app, ["init", str(initialized_store)])

        assert result.exit_code == 1
        assert "Store already exists" in result.output

    def test_init_fails_if_path_not_exists(self, tmp_path: Path) -> None:
        """Test that init fails if path doesn't exist."""
        nonexistent = tmp_path / "nonexistent"
        result = runner.invoke(app, ["init", str(nonexistent)])

        assert result.exit_code == 1
        assert "does not exist" in result.output

    def test_init_fails_if_not_in_git_repo(self, tmp_path: Path) -> None:
        """Test that init fails if not in a git repo."""
        # tmp_path is not a git repo
        result = runner.invoke(app, ["init", str(tmp_path)])

        assert result.exit_code == 1
        # Error message should indicate git detection failed


class TestSyncFromGit:
    """Tests for the sync-from-git command."""

    def test_sync_from_git_success(self, initialized_store: Path) -> None:
        """Test that sync-from-git succeeds on a valid store."""
        result = runner.invoke(app, ["sync-from-git", str(initialized_store)])

        assert result.exit_code == 0
        assert "Sync complete" in result.output

    def test_sync_from_git_fails_if_no_store(self, git_repo: Path) -> None:
        """Test that sync-from-git fails if no store exists."""
        result = runner.invoke(app, ["sync-from-git", str(git_repo)])

        assert result.exit_code == 1
        assert "No fxtr store found" in result.output


class TestFlushToGit:
    """Tests for the flush-to-git command."""

    def test_flush_to_git_success(self, initialized_store: Path) -> None:
        """Test that flush-to-git succeeds on a valid store."""
        result = runner.invoke(app, ["flush-to-git", str(initialized_store)])

        assert result.exit_code == 0
        assert "Flush complete" in result.output

    def test_flush_to_git_fails_if_no_store(self, git_repo: Path) -> None:
        """Test that flush-to-git fails if no store exists."""
        result = runner.invoke(app, ["flush-to-git", str(git_repo)])

        assert result.exit_code == 1
        assert "No fxtr store found" in result.output

    def test_flush_to_git_writes_dirty_tsvs(self, initialized_store: Path) -> None:
        """Test that flush-to-git writes dirty TSVs to disk."""

        async def setup_dirty_state() -> None:
            memory_store = MemoryStore()
            arena = memory_store.new_empty_arena()

            entity_id = create_test_entity(arena, "test_value")

            async with GitNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                # Pin an entity (this makes the TSV dirty)
                await ns.pin_entity_from_arena("testkey", entity_id, arena)

                # Verify it's dirty
                tsv_path = initialized_store / "index.fxtr.tsv"
                dirty_dir = dirty_dir_for_tsv(tsv_path)
                sentinel = dirty_dir / "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
                assert sentinel.exists(), "TSV should be dirty before flush"

            arena.close()

        anyio.run(setup_dirty_state)

        # Now run flush-to-git
        result = runner.invoke(app, ["flush-to-git", str(initialized_store)])

        assert result.exit_code == 0
        assert "Flush complete" in result.output

        # Verify the TSV is no longer dirty
        tsv_path = initialized_store / "index.fxtr.tsv"
        dirty_dir = dirty_dir_for_tsv(tsv_path)
        sentinel = dirty_dir / "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
        assert not sentinel.exists(), "TSV should not be dirty after flush"

        # Verify the TSV file exists with content
        assert tsv_path.exists()
        content = tsv_path.read_text()
        assert "testkey" in content


class TestExport:
    """Tests for the export command."""

    def test_export_creates_archive(self, initialized_store: Path, tmp_path: Path) -> None:
        """Test that export creates an archive file."""
        output_file = tmp_path / "archive.sqlite"

        result = runner.invoke(app, ["export", str(initialized_store), str(output_file)])

        assert result.exit_code == 0
        assert "Exported archive" in result.output
        assert output_file.exists()

    def test_export_fails_if_output_exists(self, initialized_store: Path, tmp_path: Path) -> None:
        """Test that export fails if output file already exists."""
        output_file = tmp_path / "archive.sqlite"
        output_file.touch()

        result = runner.invoke(app, ["export", str(initialized_store), str(output_file)])

        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_export_fails_if_no_store(self, git_repo: Path, tmp_path: Path) -> None:
        """Test that export fails if no store exists."""
        output_file = tmp_path / "archive.sqlite"

        result = runner.invoke(app, ["export", str(git_repo), str(output_file)])

        assert result.exit_code == 1
        assert "No fxtr store found" in result.output


class TestImport:
    """Tests for the import command."""

    def test_import_from_archive(self, initialized_store: Path, tmp_path: Path) -> None:
        """Test that import loads entities from an archive."""
        # First create an archive
        archive_file = tmp_path / "archive.sqlite"

        async def create_archive() -> None:
            async with GitNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                await ns.export_archive(archive_file)

        anyio.run(create_archive)

        # Now import it back (this should be a no-op since it's the same store)
        result = runner.invoke(app, ["import", str(initialized_store), str(archive_file)])

        assert result.exit_code == 0
        assert "Imported entities" in result.output

    def test_import_fails_if_archive_not_exists(self, initialized_store: Path) -> None:
        """Test that import fails if archive doesn't exist."""
        result = runner.invoke(
            app, ["import", str(initialized_store), "/nonexistent/archive.sqlite"]
        )

        assert result.exit_code == 1
        assert "does not exist" in result.output

    def test_import_fails_if_no_store(self, git_repo: Path, tmp_path: Path) -> None:
        """Test that import fails if no store exists."""
        archive_file = tmp_path / "archive.sqlite"
        archive_file.touch()  # Create empty file

        result = runner.invoke(app, ["import", str(git_repo), str(archive_file)])

        assert result.exit_code == 1
        assert "No fxtr store found" in result.output


class TestList:
    """Tests for the list command."""

    def test_list_empty_store(self, initialized_store: Path) -> None:
        """Test that list shows no pins for empty store."""
        result = runner.invoke(app, ["list", str(initialized_store)])

        assert result.exit_code == 0
        assert "No pins found" in result.output

    def test_list_shows_pins(self, initialized_store: Path) -> None:
        """Test that list shows pinned entities."""

        async def setup_pins() -> str:
            memory_store = MemoryStore()
            arena = memory_store.new_empty_arena()

            entity_id = create_test_entity(arena, "test_value")

            async with GitNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                await ns.pin_entity_from_arena("mykey", entity_id, arena)
                await ns.flush()

            arena.close()
            return entity_id.id_string()

        entity_id_str = anyio.run(setup_pins)

        result = runner.invoke(app, ["list", str(initialized_store)])

        assert result.exit_code == 0
        assert "mykey" in result.output
        assert entity_id_str in result.output

    def test_list_with_prefix_filter(self, initialized_store: Path) -> None:
        """Test that list filters by prefix."""

        async def setup_pins() -> None:
            memory_store = MemoryStore()
            arena = memory_store.new_empty_arena()

            entity_id = create_test_entity(arena, "test_value")

            async with GitNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                await ns.pin_entity_from_arena("models:encoder", entity_id, arena)
                await ns.pin_entity_from_arena("models:decoder", entity_id, arena)
                await ns.pin_entity_from_arena("other:key", entity_id, arena)
                await ns.flush()

            arena.close()

        anyio.run(setup_pins)

        result = runner.invoke(app, ["list", "--prefix", "models:", str(initialized_store)])

        assert result.exit_code == 0
        assert "models:encoder" in result.output
        assert "models:decoder" in result.output
        assert "other:key" not in result.output

    def test_list_fails_if_no_store(self, git_repo: Path) -> None:
        """Test that list fails if no store exists."""
        result = runner.invoke(app, ["list", str(git_repo)])

        assert result.exit_code == 1
        assert "No fxtr store found" in result.output


class TestStatus:
    """Tests for the status command."""

    def test_status_shows_config(self, initialized_store: Path) -> None:
        """Test that status shows store configuration."""
        result = runner.invoke(app, ["status", str(initialized_store)])

        assert result.exit_code == 0
        assert "Store root:" in result.output
        assert "Reserved prefix:" in result.output
        assert "Index namespace:" in result.output
        assert "Worktree namespace:" in result.output
        assert "SQLite store:" in result.output
        assert str(initialized_store) in result.output

    def test_status_shows_no_dirty_tsvs(self, initialized_store: Path) -> None:
        """Test that status shows no dirty TSVs when clean."""
        result = runner.invoke(app, ["status", str(initialized_store)])

        assert result.exit_code == 0
        assert "No dirty TSV files" in result.output

    def test_status_shows_dirty_tsvs(self, initialized_store: Path) -> None:
        """Test that status shows dirty TSV files."""
        # Create a dirty state manually by creating the sentinel file.
        # This simulates another process having an open GitNamespaceBackend with dirty marks.
        # (GitNamespaceBackend.aclose() flushes, so we can't test this with a real namespace
        # that we close before running status.)
        tsv_path = initialized_store / "index.fxtr.tsv"
        dirty_dir = dirty_dir_for_tsv(tsv_path)
        dirty_dir.mkdir(parents=True, exist_ok=True)
        sentinel = dirty_dir / "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
        sentinel.write_text("This file indicates the TSV is dirty.")

        result = runner.invoke(app, ["status", str(initialized_store)])

        assert result.exit_code == 0
        assert "Dirty TSV files" in result.output
        assert "index.fxtr.tsv" in result.output
        assert "flush-to-git" in result.output

    def test_status_fails_if_no_store(self, git_repo: Path) -> None:
        """Test that status fails if no store exists."""
        result = runner.invoke(app, ["status", str(git_repo)])

        assert result.exit_code == 1
        assert "No fxtr store found" in result.output


class TestNoArgsShowsHelp:
    """Test that running without arguments shows help."""

    def test_no_args_shows_help(self) -> None:
        """Test that running without arguments shows help."""
        result = runner.invoke(app, [])

        # Typer's no_args_is_help shows usage but may have non-zero exit code
        assert "Usage:" in result.output
        assert "init" in result.output
        assert "sync-from-git" in result.output
        assert "flush-to-git" in result.output


class TestCommandHelp:
    """Test that --help works for all commands."""

    @pytest.mark.parametrize(
        "command",
        ["init", "sync-from-git", "flush-to-git", "export", "import", "list", "status"],
    )
    def test_command_help(self, command: str) -> None:
        """Test that --help works for a command."""
        result = runner.invoke(app, [command, "--help"])

        assert result.exit_code == 0
        assert "Usage:" in result.output


class TestImportWithMissingBackingStore:
    """Tests for importing archives when backing storage is missing.

    This scenario occurs when:
    1. TSV files exist (committed to git)
    2. SQLite backing store is missing (deleted or on a new machine)
    3. User wants to import an archive to restore the backing store
    """

    def test_import_archive_after_deleting_backing_store(
        self, initialized_store: Path, tmp_path: Path
    ) -> None:
        """Test that we can import an archive even if the backing store was deleted.

        Steps:
        1. Create some pins in a git store
        2. Export them to an archive
        3. Delete the backing storage but keep the TSVs
        4. Try to import the archive

        This should work but currently fails because open() does a sync
        that fails when entities are missing from the backing store.
        """
        archive_file = tmp_path / "archive.sqlite"

        async def setup_and_export() -> tuple[str, Path]:
            """Pin an entity, export to archive, return entity ID and sqlite path."""
            memory_store = MemoryStore()
            arena = memory_store.new_empty_arena()

            entity_id = create_test_entity(arena, "test_value_for_import")

            async with GitNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                await ns.pin_entity_from_arena("mykey", entity_id, arena)
                await ns.flush()  # Ensure TSV is written
                await ns.export_archive(archive_file)
                sqlite_path = ns.config.sqlite_store_path

            arena.close()
            return entity_id.id_string(), sqlite_path

        entity_id_str, sqlite_path = anyio.run(setup_and_export)

        # Verify the TSV exists with the pin
        tsv_path = initialized_store / "index.fxtr.tsv"
        assert tsv_path.exists()
        assert entity_id_str in tsv_path.read_text()

        # Delete the backing store but keep the TSV
        assert sqlite_path.exists()
        sqlite_path.unlink()
        assert not sqlite_path.exists()

        # Now try to import the archive
        # This is the scenario: TSV files exist, backing store deleted, want to restore
        result = runner.invoke(app, ["import", str(initialized_store), str(archive_file)])

        # This should succeed - we have an archive with all the entities we need
        assert result.exit_code == 0, f"Import failed: {result.output}"
        assert "Imported entities" in result.output

        # Verify we can now use the store normally
        result = runner.invoke(app, ["list", str(initialized_store)])
        assert result.exit_code == 0
        assert "mykey" in result.output
        assert entity_id_str in result.output
