"""CLI for managing tsv namespaces.

This module provides the `fxtr-tsv-ns` command-line interface for initializing,
syncing, and managing tsv namespaces.

Commands:
    init          Initialize a new tsv namespace
    sync-from-tsv Sync TSV files into SQLite (git -> sqlite)
    flush-to-tsv  Flush all dirty TSVs to disk (sqlite -> git)
    export        Export the namespace to a standalone archive (tar or raw sqlite+mdb)
    import        Import entities from an archive (tar or raw sqlite)
    list          List all pins in the namespace
    status        Show namespace status (namespace IDs, dirty TSVs, etc.)
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path
from typing import Annotated, Optional

import anyio
import typer

from fxtr2.data.tsv_namespace import TsvNamespaceBackend
from fxtr2.data.tsv_namespace.config import (
    DEFAULT_RESERVED_PREFIX,
    FxtrConfigError,
    load_config,
    namespace_exists,
)
from fxtr2.data.tsv_namespace.dirty import dirty_dir_for_tsv
from fxtr2.data.tsv_namespace.sync import FxtrSyncError

app = typer.Typer(
    name="fxtr-tsv-ns",
    help="Manage tsv namespaces.",
    no_args_is_help=True,
)


def error(message: str) -> None:
    """Print an error message to stderr and exit with code 1."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def resolve_store_root(path: Path) -> Path:
    """Resolve and validate the store root path."""
    namespace_root = path.resolve()
    if not namespace_root.exists():
        error(f"Path does not exist: {namespace_root}")
    if not namespace_root.is_dir():
        error(f"Path is not a directory: {namespace_root}")
    return namespace_root


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Path to initialize as a tsv namespace")],
    reserved_prefix: Annotated[
        str,
        typer.Option(
            "--reserved-prefix",
            "-p",
            help="Reserved prefix for keys without colon (default: 'root')",
        ),
    ] = DEFAULT_RESERVED_PREFIX,
    name: Annotated[
        Optional[str],
        typer.Option("--name", "-n", help="Human-readable name for the namespace"),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option("--description", "-d", help="Description for the namespace"),
    ] = None,
) -> None:
    """Initialize a new tsv namespace.

    Creates fxtr_namespace.toml and related configuration files.
    The path must be within a git repository.
    """
    namespace_root = path.resolve()

    if not namespace_root.exists():
        error(f"Path does not exist: {namespace_root}")
    if not namespace_root.is_dir():
        error(f"Path is not a directory: {namespace_root}")

    if namespace_exists(namespace_root):
        error(f"A tsv namespace already exists at {namespace_root}")

    async def run_init() -> None:
        try:
            await TsvNamespaceBackend.init(
                namespace_root,
                reserved_prefix=reserved_prefix,
                name=name,
                description=description,
            )
        except FxtrConfigError as e:
            error(str(e))

    anyio.run(run_init)
    typer.echo(f"Initialized tsv namespace at {namespace_root}")


@app.command("sync-from-tsv")
def sync_from_git(
    path: Annotated[Path, typer.Argument(help="Path to the tsv namespace")],
) -> None:
    """Sync TSV files into SQLite (git -> sqlite).

    Use after `git checkout` or `git pull` to pre-warm the SQLite store.
    Skips dirty TSVs (SQLite is already authoritative for those).
    """
    namespace_root = resolve_store_root(path)

    if not namespace_exists(namespace_root):
        error(f"No tsv namespace found at {namespace_root}")

    async def run_sync() -> None:
        try:
            async with TsvNamespaceBackend.open(namespace_root) as _ns:
                # Opening the namespace performs a full sync automatically
                # We just need to open and close it
                pass
        except FxtrConfigError as e:
            error(str(e))
        except FxtrSyncError as e:
            error(str(e))

    anyio.run(run_sync)
    typer.echo("Sync complete.")


@app.command("flush-to-tsv")
def flush_to_git(
    path: Annotated[Path, typer.Argument(help="Path to the tsv namespace")],
) -> None:
    """Flush all dirty TSVs to disk (sqlite -> git).

    Use before `git commit` or `git checkout` to ensure TSV files are up to date.
    Also cleans up orphaned temp directories from crashed processes.
    """
    namespace_root = resolve_store_root(path)

    if not namespace_exists(namespace_root):
        error(f"No tsv namespace found at {namespace_root}")

    async def run_flush() -> None:
        try:
            async with TsvNamespaceBackend.open(namespace_root, auto_flush_seconds=None) as ns:
                await ns.full_sync_state_to_git()
        except FxtrConfigError as e:
            error(str(e))
        except FxtrSyncError as e:
            error(str(e))

    anyio.run(run_flush)
    typer.echo("Flush complete. TSV files are up to date.")


@app.command("export")
def export_archive(
    path: Annotated[Path, typer.Argument(help="Path to the tsv namespace")],
    output_file: Annotated[Path, typer.Argument(help="Output archive file path")],
    tar: Annotated[bool, typer.Option(help="Package as tarball")] = True,
    gz: Annotated[bool, typer.Option(help="Compress tarball with gzip")] = False,
) -> None:
    """Export the namespace to a standalone archive.

    Flushes all dirty TSVs first to ensure consistency.
    The archive can be imported into other tsv namespaces.

    By default produces a tarball. Use --no-tar for raw sqlite + mdb files.
    """
    namespace_root = resolve_store_root(path)

    if not namespace_exists(namespace_root):
        error(f"No tsv namespace found at {namespace_root}")

    output_path = output_file.resolve()
    if output_path.exists():
        error(f"Output file already exists: {output_path}")

    async def run_export() -> None:
        try:
            async with TsvNamespaceBackend.open(namespace_root, auto_flush_seconds=None) as ns:
                await ns.export_archive(output_path, tar=tar, gz=gz)
        except FxtrConfigError as e:
            error(str(e))
        except FxtrSyncError as e:
            error(str(e))

    anyio.run(run_export)
    typer.echo(f"Exported archive to {output_path}")


@app.command("import")
def import_archive(
    path: Annotated[Path, typer.Argument(help="Path to the tsv namespace")],
    archive_file: Annotated[
        Path, typer.Argument(help="Archive file to import (.tar, .tar.gz, or .sqlite)")
    ],
) -> None:
    """Import entities from an archive into the index namespace.

    Accepts tarball (.tar, .tar.gz, .tgz) or raw sqlite (.sqlite) archives.
    Use this to restore missing entities referenced by TSV files.
    """
    namespace_root = resolve_store_root(path)

    if not namespace_exists(namespace_root):
        error(f"No tsv namespace found at {namespace_root}")

    archive_path = archive_file.resolve()
    if not archive_path.exists():
        error(f"Archive file does not exist: {archive_path}")

    async def run_import() -> None:
        try:
            # Use skip_sync=True to allow importing when backing store is missing/empty.
            # The import will restore entities, then we sync afterward.
            async with TsvNamespaceBackend.open(
                namespace_root, auto_flush_seconds=None, skip_sync=True
            ) as ns:
                await ns.import_archive_into_backing_store(archive_path)
                # Now sync to update worktree namespace from TSVs
                await ns.sync_from_git()
        except FxtrConfigError as e:
            error(str(e))
        except FxtrSyncError as e:
            error(str(e))
        except ValueError as e:
            error(str(e))
        except (tarfile.TarError, sqlite3.Error, OSError) as e:
            error(f"Could not import archive: {e}")

    anyio.run(run_import)
    typer.echo(f"Imported entities from {archive_path}")


@app.command("list")
def list_pins(
    path: Annotated[Path, typer.Argument(help="Path to the tsv namespace")],
    prefix: Annotated[
        str,
        typer.Option("--prefix", "-p", help="Filter pins by key prefix"),
    ] = "",
) -> None:
    """List all pins in the namespace.

    Outputs one pin per line in the format: key -> fxtr_id
    """
    namespace_root = resolve_store_root(path)

    if not namespace_exists(namespace_root):
        error(f"No tsv namespace found at {namespace_root}")

    async def run_list() -> dict[str, str]:
        try:
            async with TsvNamespaceBackend.open(namespace_root, auto_flush_seconds=None) as ns:
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
    path: Annotated[Path, typer.Argument(help="Path to the tsv namespace")],
) -> None:
    """Show namespace status.

    Displays configuration info and lists any dirty TSV files.
    """
    namespace_root = resolve_store_root(path)

    if not namespace_exists(namespace_root):
        error(f"No tsv namespace found at {namespace_root}")

    async def run_status() -> None:
        try:
            config = await load_config(namespace_root)
        except FxtrConfigError as e:
            error(str(e))
            return  # unreachable

        typer.echo(f"Namespace root:       {config.namespace_root}")
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
        for tsv_file in namespace_root.rglob("*.fxtr.tsv"):
            dirty_dir = dirty_dir_for_tsv(str(tsv_file))
            sentinel_path = f"{dirty_dir}/DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
            import os

            if os.path.exists(sentinel_path):
                dirty_tsvs.append(tsv_file.relative_to(namespace_root))

        # Method 2: Scan dirty directories (for TSVs that don't exist on disk yet)
        for dirty_dir in namespace_root.rglob("*.fxtr.tsv.dirty"):
            sentinel = dirty_dir / "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
            if sentinel.exists():
                # Infer TSV path from dirty dir path (remove ".dirty" suffix)
                tsv_name = dirty_dir.name.removesuffix(".dirty")
                tsv_path = dirty_dir.parent / tsv_name
                relative_path = tsv_path.relative_to(namespace_root)
                if relative_path not in dirty_tsvs:
                    dirty_tsvs.append(relative_path)

        typer.echo("")
        if dirty_tsvs:
            typer.echo(f"Dirty TSV files ({len(dirty_tsvs)}):")
            for tsv in sorted(dirty_tsvs):
                typer.echo(f"  {tsv}")
            typer.echo("")
            typer.echo("Run 'fxtr-tsv-ns flush-to-tsv' to write changes to TSV files.")
        else:
            typer.echo("No dirty TSV files. All TSV files are up to date.")

    anyio.run(run_status)


def main() -> None:
    """Entry point for the fxtr-tsv-ns CLI."""
    app()


if __name__ == "__main__":
    main()
