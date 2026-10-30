"""CLI for managing git-backed fxtr stores.

This module provides the `fxtr-git-store` command-line interface for initializing,
syncing, and managing git-backed fxtr stores.

Commands:
    init          Initialize a new git-backed fxtr store
    sync-from-git Sync TSV files into SQLite (git -> sqlite)
    flush-to-git  Flush all dirty TSVs to disk (sqlite -> git)
    export        Export store to a standalone SQLite archive
    import        Import entities from an archive
    list          List all pins in the store
    status        Show store status (namespace IDs, dirty TSVs, etc.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import anyio
import typer

from fxtr2.data.git_store import GitNamespaceBackend
from fxtr2.data.git_store.config import FxtrConfigError, load_config, store_exists
from fxtr2.data.git_store.dirty import dirty_dir_for_tsv
from fxtr2.data.git_store.sync import FxtrSyncError

app = typer.Typer(
    name="fxtr-git-store",
    help="Manage git-backed fxtr stores.",
    no_args_is_help=True,
)


def error(message: str) -> None:
    """Print an error message to stderr and exit with code 1."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def resolve_store_root(path: Path) -> Path:
    """Resolve and validate the store root path."""
    store_root = path.resolve()
    if not store_root.exists():
        error(f"Path does not exist: {store_root}")
    if not store_root.is_dir():
        error(f"Path is not a directory: {store_root}")
    return store_root


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Path to initialize as a fxtr store")],
    reserved_prefix: Annotated[
        str,
        typer.Option(
            "--reserved-prefix",
            "-p",
            help="Reserved prefix for keys without colon (default: 'index')",
        ),
    ] = "index",
    name: Annotated[
        Optional[str],
        typer.Option("--name", "-n", help="Human-readable name for the store"),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option("--description", "-d", help="Description for the store"),
    ] = None,
) -> None:
    """Initialize a new git-backed fxtr store.

    Creates fxtr_store.toml and related configuration files.
    The path must be within a git repository.
    """
    store_root = path.resolve()

    if not store_root.exists():
        error(f"Path does not exist: {store_root}")
    if not store_root.is_dir():
        error(f"Path is not a directory: {store_root}")

    if store_exists(store_root):
        error(f"Store already exists at {store_root}")

    async def run_init() -> None:
        try:
            await GitNamespaceBackend.init(
                store_root,
                reserved_prefix=reserved_prefix,
                name=name,
                description=description,
            )
        except FxtrConfigError as e:
            error(str(e))

    anyio.run(run_init)
    typer.echo(f"Initialized fxtr store at {store_root}")


@app.command("sync-from-git")
def sync_from_git(
    path: Annotated[Path, typer.Argument(help="Path to the fxtr store")],
) -> None:
    """Sync TSV files into SQLite (git -> sqlite).

    Use after `git checkout` or `git pull` to pre-warm the SQLite store.
    Skips dirty TSVs (SQLite is already authoritative for those).
    """
    store_root = resolve_store_root(path)

    if not store_exists(store_root):
        error(f"No fxtr store found at {store_root}")

    async def run_sync() -> None:
        try:
            async with GitNamespaceBackend.open(store_root) as _ns:
                # Opening the namespace performs a full sync automatically
                # We just need to open and close it
                pass
        except FxtrConfigError as e:
            error(str(e))
        except FxtrSyncError as e:
            error(str(e))

    anyio.run(run_sync)
    typer.echo("Sync complete.")


@app.command("flush-to-git")
def flush_to_git(
    path: Annotated[Path, typer.Argument(help="Path to the fxtr store")],
) -> None:
    """Flush all dirty TSVs to disk (sqlite -> git).

    Use before `git commit` or `git checkout` to ensure TSV files are up to date.
    Also cleans up orphaned temp directories from crashed processes.
    """
    store_root = resolve_store_root(path)

    if not store_exists(store_root):
        error(f"No fxtr store found at {store_root}")

    async def run_flush() -> None:
        try:
            async with GitNamespaceBackend.open(store_root, auto_flush_seconds=None) as ns:
                await ns.full_sync_state_to_git()
        except FxtrConfigError as e:
            error(str(e))
        except FxtrSyncError as e:
            error(str(e))

    anyio.run(run_flush)
    typer.echo("Flush complete. TSV files are up to date.")


@app.command("export")
def export_archive(
    path: Annotated[Path, typer.Argument(help="Path to the fxtr store")],
    output_file: Annotated[Path, typer.Argument(help="Output archive file path")],
) -> None:
    """Export store to a standalone SQLite archive.

    Flushes all dirty TSVs first to ensure consistency.
    The archive can be imported into other stores.
    """
    store_root = resolve_store_root(path)

    if not store_exists(store_root):
        error(f"No fxtr store found at {store_root}")

    output_path = output_file.resolve()
    if output_path.exists():
        error(f"Output file already exists: {output_path}")

    async def run_export() -> None:
        try:
            async with GitNamespaceBackend.open(store_root, auto_flush_seconds=None) as ns:
                await ns.export_archive(output_path)
        except FxtrConfigError as e:
            error(str(e))
        except FxtrSyncError as e:
            error(str(e))

    anyio.run(run_export)
    typer.echo(f"Exported archive to {output_path}")


@app.command("import")
def import_archive(
    path: Annotated[Path, typer.Argument(help="Path to the fxtr store")],
    archive_file: Annotated[Path, typer.Argument(help="Archive file to import")],
) -> None:
    """Import entities from an archive into the index namespace.

    Use this to restore missing entities referenced by TSV files.
    """
    store_root = resolve_store_root(path)

    if not store_exists(store_root):
        error(f"No fxtr store found at {store_root}")

    archive_path = archive_file.resolve()
    if not archive_path.exists():
        error(f"Archive file does not exist: {archive_path}")

    async def run_import() -> None:
        try:
            # Use skip_sync=True to allow importing when backing store is missing/empty.
            # The import will restore entities, then we sync afterward.
            async with GitNamespaceBackend.open(
                store_root, auto_flush_seconds=None, skip_sync=True
            ) as ns:
                await ns.import_archive(archive_path)
                # Now sync to update worktree namespace from TSVs
                await ns.sync_from_git()
        except FxtrConfigError as e:
            error(str(e))
        except FxtrSyncError as e:
            error(str(e))

    anyio.run(run_import)
    typer.echo(f"Imported entities from {archive_path}")


@app.command("list")
def list_pins(
    path: Annotated[Path, typer.Argument(help="Path to the fxtr store")],
    prefix: Annotated[
        str,
        typer.Option("--prefix", "-p", help="Filter pins by key prefix"),
    ] = "",
) -> None:
    """List all pins in the store.

    Outputs one pin per line in the format: key -> fxtr_id
    """
    store_root = resolve_store_root(path)

    if not store_exists(store_root):
        error(f"No fxtr store found at {store_root}")

    async def run_list() -> dict[str, str]:
        try:
            async with GitNamespaceBackend.open(store_root, auto_flush_seconds=None) as ns:
                pins = await ns.list_pins(prefix)
                return {k: v.id_string() for k, v in pins.items()}
        except FxtrConfigError as e:
            error(str(e))
        except FxtrSyncError as e:
            error(str(e))
        return {}  # unreachable, but makes type checker happy

    pins = anyio.run(run_list)

    if not pins:
        typer.echo("No pins found.")
    else:
        for key in sorted(pins.keys()):
            typer.echo(f"{key} -> {pins[key]}")


@app.command("status")
def status(
    path: Annotated[Path, typer.Argument(help="Path to the fxtr store")],
) -> None:
    """Show store status.

    Displays configuration info and lists any dirty TSV files.
    """
    store_root = resolve_store_root(path)

    if not store_exists(store_root):
        error(f"No fxtr store found at {store_root}")

    async def run_status() -> None:
        try:
            config = await load_config(store_root)
        except FxtrConfigError as e:
            error(str(e))
            return  # unreachable

        typer.echo(f"Store root:           {config.store_root}")
        typer.echo(f"Reserved prefix:      {config.reserved_prefix}")
        typer.echo(f"Index namespace:      {config.index_namespace_id}")
        typer.echo(f"Worktree namespace:   {config.worktree_namespace_id}")
        typer.echo(f"SQLite store:         {config.sqlite_store_path}")
        typer.echo(f"Fxtr directory:       {config.fxtr_dir}")

        # Find dirty TSVs by scanning for dirty directories with sentinels.
        # A dirty TSV might not have a file on disk yet (entirely in sqlite),
        # so we scan for both existing TSVs with dirty dirs AND dirty dirs
        # that indicate TSVs that don't exist yet.
        dirty_tsvs: list[Path] = []

        # Method 1: Scan existing TSV files
        for tsv_file in store_root.rglob("*.fxtr.tsv"):
            dirty_dir = dirty_dir_for_tsv(tsv_file)
            sentinel = dirty_dir / "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
            if sentinel.exists():
                dirty_tsvs.append(tsv_file.relative_to(store_root))

        # Method 2: Scan dirty directories (for TSVs that don't exist on disk yet)
        for dirty_dir in store_root.rglob("*.fxtr.tsv.dirty"):
            sentinel = dirty_dir / "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
            if sentinel.exists():
                # Infer TSV path from dirty dir path (remove ".dirty" suffix)
                tsv_name = dirty_dir.name.removesuffix(".dirty")
                tsv_path = dirty_dir.parent / tsv_name
                relative_path = tsv_path.relative_to(store_root)
                if relative_path not in dirty_tsvs:
                    dirty_tsvs.append(relative_path)

        typer.echo("")
        if dirty_tsvs:
            typer.echo(f"Dirty TSV files ({len(dirty_tsvs)}):")
            for tsv in sorted(dirty_tsvs):
                typer.echo(f"  {tsv}")
            typer.echo("")
            typer.echo("Run 'fxtr-git-store flush-to-git' to write changes to TSV files.")
        else:
            typer.echo("No dirty TSV files. All TSV files are up to date.")

    anyio.run(run_status)


def main() -> None:
    """Entry point for the fxtr-git-store CLI."""
    app()


if __name__ == "__main__":
    main()
