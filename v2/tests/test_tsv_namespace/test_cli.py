"""Tests for the fxtr-git-ns CLI.

Uses Typer's CliRunner for in-process testing.
"""

from __future__ import annotations

import os
import subprocess
import typing
from pathlib import Path
from typing import Any

import anyio
import pytest
from typer.testing import CliRunner

from fxtr2.cli.tsv_ns_cli import app
from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.data.tsv_namespace import TsvNamespaceBackend
from fxtr2.data.tsv_namespace.config import namespace_exists
from fxtr2.data.tsv_namespace.dirty import dirty_dir_for_tsv
from fxtr2.leases.local_lease import UnconditionalLocalLifetime

runner = CliRunner()


def mem_l(memory_arena: MemoryArena) -> Any:
    return typing.cast(Any, memory_arena)._test_lease.clone()


def tsv_backend_lease(ns: TsvNamespaceBackend) -> Any:
    return ns._session.root_lease.clone()


def create_test_entity(memory_arena: MemoryArena, value: str) -> FxtrID:
    """Create a simple test entity and store it in the arena (requires ``_test_lease``)."""
    from fxtr2.data.entity_core import FxtrCbor

    ground_context = FxtrID.from_tag_and_payload(".", b"")
    memory_arena.track_builtin(mem_l(memory_arena), ground_context)
    type_id = FxtrID.builtin("test_entity")
    memory_arena.track_builtin(mem_l(memory_arena), type_id)

    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
        "value": value,
    }
    return memory_arena.store(mem_l(memory_arena), entity)


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
    anyio.run(lambda: TsvNamespaceBackend.init(git_repo))
    return git_repo


class TestInit:
    """Tests for the init command."""

    def test_init_creates_store(self, git_repo: Path) -> None:
        """Test that init creates a fxtr store."""
        result = runner.invoke(app, ["init", str(git_repo)])

        assert result.exit_code == 0
        assert "Initialized tsv namespace" in result.output
        assert namespace_exists(git_repo)
        assert (git_repo / "fxtr_namespace.toml").exists()

    def test_init_with_reserved_prefix(self, git_repo: Path) -> None:
        """Test that init accepts a custom reserved prefix."""
        result = runner.invoke(app, ["init", "--reserved-prefix", "pins", str(git_repo)])

        assert result.exit_code == 0
        content = (git_repo / "fxtr_namespace.toml").read_text()
        assert 'reserved_prefix = "pins"' in content

    def test_init_with_name_and_description(self, git_repo: Path) -> None:
        """Test that init accepts name and description."""
        result = runner.invoke(
            app,
            ["init", "--name", "Test Store", "--description", "A test store", str(git_repo)],
        )

        assert result.exit_code == 0
        content = (git_repo / "fxtr_namespace.toml").read_text()
        assert 'name = "Test Store"' in content
        assert 'description = "A test store"' in content

    def test_init_fails_if_store_exists(self, initialized_store: Path) -> None:
        """Test that init fails if store already exists."""
        result = runner.invoke(app, ["init", str(initialized_store)])

        assert result.exit_code == 1
        assert "tsv namespace already exists" in result.output

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
    """Tests for the sync-from-tsv command."""

    def test_sync_from_git_success(self, initialized_store: Path) -> None:
        """Test that sync-from-tsv succeeds on a valid store."""
        result = runner.invoke(app, ["sync-from-tsv", str(initialized_store)])

        assert result.exit_code == 0
        assert "Sync complete" in result.output

    def test_sync_from_git_fails_if_no_store(self, git_repo: Path) -> None:
        """Test that sync-from-tsv fails if no store exists."""
        result = runner.invoke(app, ["sync-from-tsv", str(git_repo)])

        assert result.exit_code == 1
        assert "No tsv namespace found" in result.output


class TestFlushToGit:
    """Tests for the flush-to-tsv command."""

    def test_flush_to_git_success(self, initialized_store: Path) -> None:
        """Test that flush-to-tsv succeeds on a valid store."""
        result = runner.invoke(app, ["flush-to-tsv", str(initialized_store)])

        assert result.exit_code == 0
        assert "Flush complete" in result.output

    def test_flush_to_git_fails_if_no_store(self, git_repo: Path) -> None:
        """Test that flush-to-tsv fails if no store exists."""
        result = runner.invoke(app, ["flush-to-tsv", str(git_repo)])

        assert result.exit_code == 1
        assert "No tsv namespace found" in result.output

    def test_flush_to_git_writes_dirty_tsvs(self, initialized_store: Path) -> None:
        """Test that flush-to-tsv writes dirty TSVs to disk."""

        async def setup_dirty_state() -> None:
            memory_store = MemoryStore()
            _ull = UnconditionalLocalLifetime("test")
            memory_store.register_token(_ull.token, name="test")
            root_lease = _ull.root_lease.take()
            arena = memory_store.new_arena(root_lease.clone())
            arena._test_lease = root_lease  # type: ignore[attr-defined]

            entity_id = create_test_entity(arena, "test_value")

            async with TsvNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                # Pin an entity (this makes the TSV dirty)
                await ns.pin_entity_from_arena("testkey", entity_id, arena, mem_l(arena))

                # Verify it's dirty
                tsv_path = initialized_store / "root.fxtr.tsv"
                dirty_dir = dirty_dir_for_tsv(str(tsv_path))
                sentinel = f"{dirty_dir}/DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
                assert os.path.exists(sentinel), "TSV should be dirty before flush"

            arena.close()
            root_lease.discard()

        anyio.run(setup_dirty_state)

        # Now run flush-to-tsv
        result = runner.invoke(app, ["flush-to-tsv", str(initialized_store)])

        assert result.exit_code == 0
        assert "Flush complete" in result.output

        # Verify the TSV is no longer dirty
        tsv_path = initialized_store / "root.fxtr.tsv"
        dirty_dir = dirty_dir_for_tsv(str(tsv_path))
        sentinel = f"{dirty_dir}/DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
        assert not os.path.exists(sentinel), "TSV should not be dirty after flush"

        # Verify the TSV file exists with content
        assert tsv_path.exists()
        content = tsv_path.read_text()
        assert "testkey" in content


class TestExport:
    """Tests for the export command."""

    def test_export_creates_tar(self, initialized_store: Path, tmp_path: Path) -> None:
        """Test that export creates a tarball by default."""
        output_file = tmp_path / "archive.tar"

        result = runner.invoke(app, ["export", str(initialized_store), str(output_file)])

        assert result.exit_code == 0
        assert "Exported archive" in result.output
        assert output_file.exists()

    def test_export_notar(self, initialized_store: Path, tmp_path: Path) -> None:
        """Test that --no-tar creates raw sqlite + mdb files."""
        output_file = tmp_path / "archive.sqlite"

        result = runner.invoke(
            app, ["export", str(initialized_store), str(output_file), "--no-tar"]
        )

        assert result.exit_code == 0
        assert "Exported archive" in result.output
        assert output_file.exists()
        assert (tmp_path / "archive.sqlite.data2.shards").is_dir()

    def test_export_gz(self, initialized_store: Path, tmp_path: Path) -> None:
        """Test that --gz creates a gzipped tarball."""
        output_file = tmp_path / "archive.tar.gz"

        result = runner.invoke(app, ["export", str(initialized_store), str(output_file), "--gz"])

        assert result.exit_code == 0
        assert output_file.exists()

    def test_export_fails_if_output_exists(self, initialized_store: Path, tmp_path: Path) -> None:
        """Test that export fails if output file already exists."""
        output_file = tmp_path / "archive.tar"
        output_file.touch()

        result = runner.invoke(app, ["export", str(initialized_store), str(output_file)])

        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_export_fails_if_no_store(self, git_repo: Path, tmp_path: Path) -> None:
        """Test that export fails if no store exists."""
        output_file = tmp_path / "archive.tar"

        result = runner.invoke(app, ["export", str(git_repo), str(output_file)])

        assert result.exit_code == 1
        assert "No tsv namespace found" in result.output


class TestImport:
    """Tests for the import command."""

    def test_import_from_sqlite(self, initialized_store: Path, tmp_path: Path) -> None:
        """Test that import loads entities from a raw sqlite archive."""
        archive_file = tmp_path / "archive.sqlite"

        async def create_archive() -> None:
            async with TsvNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                await ns.export_archive(archive_file, tar=False)

        anyio.run(create_archive)

        result = runner.invoke(app, ["import", str(initialized_store), str(archive_file)])

        assert result.exit_code == 0
        assert "Imported entities" in result.output

    def test_import_from_tar(self, initialized_store: Path, tmp_path: Path) -> None:
        """Test that import loads entities from a tarball."""
        archive_file = tmp_path / "archive.tar"

        async def create_archive() -> None:
            async with TsvNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                await ns.export_archive(archive_file, tar=True)

        anyio.run(create_archive)

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
        assert "No tsv namespace found" in result.output


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
            _ull = UnconditionalLocalLifetime("test")
            memory_store.register_token(_ull.token, name="test")
            root_lease = _ull.root_lease.take()
            arena = memory_store.new_arena(root_lease.clone())
            arena._test_lease = root_lease  # type: ignore[attr-defined]

            entity_id = create_test_entity(arena, "test_value")

            async with TsvNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                await ns.pin_entity_from_arena("mykey", entity_id, arena, mem_l(arena))
                await ns.flush()

            arena.close()
            root_lease.discard()
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
            _ull = UnconditionalLocalLifetime("test")
            memory_store.register_token(_ull.token, name="test")
            root_lease = _ull.root_lease.take()
            arena = memory_store.new_arena(root_lease.clone())
            arena._test_lease = root_lease  # type: ignore[attr-defined]

            entity_id = create_test_entity(arena, "test_value")

            async with TsvNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                await ns.pin_entity_from_arena("models:encoder", entity_id, arena, mem_l(arena))
                await ns.pin_entity_from_arena("models:decoder", entity_id, arena, mem_l(arena))
                await ns.pin_entity_from_arena("other:key", entity_id, arena, mem_l(arena))
                await ns.flush()

            arena.close()
            root_lease.discard()

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
        assert "No tsv namespace found" in result.output


class TestStatus:
    """Tests for the status command."""

    def test_status_shows_config(self, initialized_store: Path) -> None:
        """Test that status shows store configuration."""
        result = runner.invoke(app, ["status", str(initialized_store)])

        assert result.exit_code == 0
        assert "Namespace root:" in result.output
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
        # This simulates another process having an open TsvNamespaceBackend with dirty marks.
        # (TsvNamespaceBackend.aclose() flushes, so we can't test this with a real namespace
        # that we close before running status.)
        tsv_path = initialized_store / "root.fxtr.tsv"
        dirty_dir = dirty_dir_for_tsv(str(tsv_path))
        os.makedirs(dirty_dir, exist_ok=True)
        sentinel = f"{dirty_dir}/DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
        with open(sentinel, "w") as f:
            f.write("This file indicates the TSV is dirty.")

        result = runner.invoke(app, ["status", str(initialized_store)])

        assert result.exit_code == 0
        assert "Dirty TSV files" in result.output
        assert "root.fxtr.tsv" in result.output
        assert "flush-to-tsv" in result.output

    def test_status_fails_if_no_store(self, git_repo: Path) -> None:
        """Test that status fails if no store exists."""
        result = runner.invoke(app, ["status", str(git_repo)])

        assert result.exit_code == 1
        assert "No tsv namespace found" in result.output


class TestNoArgsShowsHelp:
    """Test that running without arguments shows help."""

    def test_no_args_shows_help(self) -> None:
        """Test that running without arguments shows help."""
        result = runner.invoke(app, [])

        # Typer's no_args_is_help shows usage but may have non-zero exit code
        assert "Usage:" in result.output
        assert "init" in result.output
        assert "sync-from-tsv" in result.output
        assert "flush-to-tsv" in result.output


class TestCommandHelp:
    """Test that --help works for all commands."""

    @pytest.mark.parametrize(
        "command",
        ["init", "sync-from-tsv", "flush-to-tsv", "export", "import", "list", "status"],
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
        1. Create some pins in a tsv namespace
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
            _ull = UnconditionalLocalLifetime("test")
            memory_store.register_token(_ull.token, name="test")
            root_lease = _ull.root_lease.take()
            arena = memory_store.new_arena(root_lease.clone())
            arena._test_lease = root_lease  # type: ignore[attr-defined]

            entity_id = create_test_entity(arena, "test_value_for_import")

            async with TsvNamespaceBackend.open(initialized_store, auto_flush_seconds=None) as ns:
                await ns.pin_entity_from_arena("mykey", entity_id, arena, mem_l(arena))
                await ns.flush()  # Ensure TSV is written
                await ns.export_archive(archive_file, tar=False)
                sqlite_path = ns.config.sqlite_store_path

            arena.close()
            root_lease.discard()
            return entity_id.id_string(), sqlite_path

        entity_id_str, sqlite_path = anyio.run(setup_and_export)

        # Verify the TSV exists with the pin
        tsv_path = initialized_store / "root.fxtr.tsv"
        assert tsv_path.exists()
        assert entity_id_str in tsv_path.read_text()

        # Delete the backing store (sqlite + segment files) but keep the TSV
        assert sqlite_path.exists()
        sqlite_path.unlink()
        # Also remove the segment files directory
        import shutil

        shards_dir = sqlite_path.parent / (sqlite_path.name + ".data2.shards")
        if shards_dir.exists():
            shutil.rmtree(shards_dir)
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
