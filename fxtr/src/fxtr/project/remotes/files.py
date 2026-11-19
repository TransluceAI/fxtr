"""The edits that ``fxtr remote`` makes to the files that configure remotes.

A remote is a table in one of two files (see :mod:`fxtr.project.config`): committed in
``pyproject.toml`` under ``[tool.fxtr.remotes.<name>]``, so every checkout has it, or this
machine's own in ``fxtr.local.toml`` under ``[remotes.<name>]``. Each file may set
``default_remote`` (in ``[tool.fxtr]``, or at the top of the local file); the local file's wins.

The functions here edit a file's text with ``tomlkit``, which keeps the comments and layout of
everything else in it, and then read both files again (:func:`~fxtr.project.config.
load_remotes`), so an edit that would leave the configuration unreadable is refused and
nothing is written. They need no database and open no project client, as ``git remote`` needs
no network. Creating a hub project for a remote is the caller's: these functions write only what
they are given.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import tomlkit
from tomlkit.container import OutOfOrderTableProxy
from tomlkit.exceptions import ParseError
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from fxtr.project.config import (
    LOCAL_CONFIG_FILENAME,
    PYPROJECT_FILENAME,
    TOOL_TABLE,
    ConfigError,
    RemoteConfig,
    load_remotes,
)

_REMOTE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")


def check_remote_name(project_root: Path, name: str) -> None:
    """Refuse a name that a new remote cannot take: one that is not a plain name, or that a
    remote of either file has already.

    Args:
        project_root: The project root.
        name: The new remote's name.

    Raises:
        ConfigError: If ``name`` is not letters, digits, ``_``, and ``-`` (not first), or a
            remote has the name.
    """
    if not _REMOTE_NAME.fullmatch(name):
        raise ConfigError(
            f"{name!r} cannot name a remote: use letters, digits, '_', and '-', and do not start"
            " with '-'"
        )
    existing = load_remotes(project_root).by_name.get(name)
    if existing is not None:
        raise ConfigError(f"A remote named {name!r} exists already: {existing.where}")


def add_remote(
    project_root: Path, name: str, kind: str, settings: Mapping[str, str], *, local: bool
) -> bool:
    """Add a remote. If neither file sets a default remote, the new remote becomes the default,
    in the same file, so a push that names no remote keeps going to it when more are added.

    Args:
        project_root: The project root.
        name: The remote's name (:func:`check_remote_name`).
        kind: The remote's kind, such as ``hub``.
        settings: The other keys of the remote's table, which its kind declares.
        local: If true, write ``fxtr.local.toml``, for this machine only; else
            ``pyproject.toml``, for every checkout.

    Returns:
        True if the remote became the default remote.

    Raises:
        ConfigError: If the name cannot be used, or a file cannot be read.
    """
    check_remote_name(project_root, name)
    becomes_default = load_remotes(project_root).default is None
    path = _path(project_root, local)
    document = _read(path)
    table = _settings_table(document, local)
    remotes = _table(table, "remotes")
    if remotes is None:
        remotes = tomlkit.table(is_super_table=True)
        table["remotes"] = remotes
    remote = tomlkit.table()
    remote["kind"] = kind
    for key, value in settings.items():
        remote[key] = value
    # A blank line after the table, as before every other table, so the next one stands apart.
    remote.add(tomlkit.nl())
    remotes[name] = remote
    if becomes_default:
        table["default_remote"] = name
    _write(project_root, {path: document})
    return becomes_default


def remove_remote(project_root: Path, name: str) -> None:
    """Remove a remote, and any ``default_remote`` that names it, in either file.

    Raises:
        ConfigError: If no remote has the name, or a file cannot be read.
    """
    remote = _configured(project_root, name)
    documents = _documents(project_root)
    for path, document in documents.items():
        table = _settings_table(document, path.name == LOCAL_CONFIG_FILENAME)
        remotes = _table(table, "remotes")
        if remote.file == path and remotes is not None:
            del remotes[name]
        if table.get("default_remote") == name:
            del table["default_remote"]
    _write(project_root, documents)


def rename_remote(project_root: Path, old: str, new: str) -> None:
    """Rename a remote, in the file that configures it, and any ``default_remote`` that names
    it, in either file. The remote's settings and position in its file are kept.

    Raises:
        ConfigError: If no remote is named ``old``, ``new`` cannot name a new remote, or a file
            cannot be read.
    """
    remote = _configured(project_root, old)
    check_remote_name(project_root, new)
    documents = _documents(project_root)
    for path, document in documents.items():
        table = _settings_table(document, path.name == LOCAL_CONFIG_FILENAME)
        remotes = _table(table, "remotes")
        if remote.file == path and remotes is not None:
            # Rebuilt in order, so the renamed remote keeps its place among the others.
            entries = [(new if key == old else key, value) for key, value in remotes.items()]
            for key, _ in list(remotes.items()):
                del remotes[key]
            for key, value in entries:
                remotes[key] = value
        if table.get("default_remote") == old:
            table["default_remote"] = new
    _write(project_root, documents)


def set_default_remote(project_root: Path, name: str, *, local: bool) -> None:
    """Make a remote the one a push or a pull uses when it names none.

    Args:
        project_root: The project root.
        name: A remote of either file.
        local: If true, set the default for this machine only, in ``fxtr.local.toml``, where it
            overrides the committed default; else set the committed default in
            ``pyproject.toml``.

    Raises:
        ConfigError: If no remote has the name, a committed default would name a remote of this
            machine only (other checkouts do not have it), or a file cannot be read.
    """
    remote = _configured(project_root, name)
    if not local and remote.local:
        raise ConfigError(
            f"{name!r} is a remote of this machine only ({remote.where}); other checkouts do not"
            " have it. Set it as this machine's default with --local."
        )
    path = _path(project_root, local)
    document = _read(path)
    _settings_table(document, local)["default_remote"] = name
    _write(project_root, {path: document})


def _configured(project_root: Path, name: str) -> RemoteConfig:
    remote = load_remotes(project_root).by_name.get(name)
    if remote is None:
        raise ConfigError(f"This project has no remote named {name!r}")
    return remote


def _path(project_root: Path, local: bool) -> Path:
    return project_root / (LOCAL_CONFIG_FILENAME if local else PYPROJECT_FILENAME)


def _read(path: Path) -> TOMLDocument:
    """The file as a ``tomlkit`` document; a new one for a local file that does not exist."""
    if not path.is_file():
        document = tomlkit.document()
        document.add(tomlkit.comment("This machine's fxtr settings. Local; not committed."))
        return document
    try:
        return tomlkit.parse(path.read_text())
    except ParseError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None


def _documents(project_root: Path) -> dict[Path, TOMLDocument]:
    """Both files that exist, as documents."""
    paths = (_path(project_root, False), _path(project_root, True))
    return {path: _read(path) for path in paths if path.is_file()}


def _settings_table(
    document: TOMLDocument, local: bool
) -> TOMLDocument | Table | OutOfOrderTableProxy:
    """The table that holds the file's ``remotes`` and ``default_remote``: the local file
    itself, or ``[tool.fxtr]`` of ``pyproject.toml``."""
    if local:
        return document
    tool = _table(document, "tool")
    table = None if tool is None else _table(tool, TOOL_TABLE)
    if table is None:
        raise ConfigError(f"{PYPROJECT_FILENAME} has no [tool.{TOOL_TABLE}] table")
    return table


def _table(
    parent: TOMLDocument | Table | OutOfOrderTableProxy, key: str
) -> Table | OutOfOrderTableProxy | None:
    """The table at ``key`` of ``parent``, or ``None`` if there is none. A table whose
    subtables are not all together in the file, as ``[tool]`` is when ``[build-system]`` sits
    between ``[tool.uv]`` and ``[tool.fxtr]``, comes as an ``OutOfOrderTableProxy``, which edits
    the tables it spans. Reading the file checked its shape
    (:func:`~fxtr.project.config.load_remotes`), so a value there that is not a table is a file
    edited since."""
    value = parent.get(key)
    if value is None:
        return None
    if not isinstance(value, Table | OutOfOrderTableProxy):
        raise ConfigError(f"{key} must be a table")
    return value


def _write(project_root: Path, documents: Mapping[Path, TOMLDocument]) -> None:
    """Write the edited documents, if both files then read as a valid configuration; else
    write nothing and put the files back as they were.

    Raises:
        ConfigError: What reading the edited files raises.
    """
    before = {path: path.read_text() if path.is_file() else None for path in documents}
    try:
        for path, document in documents.items():
            path.write_text(tomlkit.dumps(document))
        load_remotes(project_root)
    except BaseException:
        for path, text in before.items():
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(text)
        raise


__all__ = [
    "add_remote",
    "check_remote_name",
    "remove_remote",
    "rename_remote",
    "set_default_remote",
]
