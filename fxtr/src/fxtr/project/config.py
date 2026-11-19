"""A project's location and configuration.

A project is a directory holding a ``pyproject.toml`` with a ``[tool.fxtr]`` table: the *project
root*, the directory of a Python project that depends on fxtr. It need not be the root of its
repository; a monorepo or a uv workspace member is a project inside a larger repo. Two files
beside each other configure it.

**``pyproject.toml``**, committed, shared by every checkout::

    [project]
    name = "my-experiments"          # required; names the project

    [tool.fxtr]                       # required; marks the pyproject as a fxtr project
    modules = ["my_experiments.steps", "my_experiments.flows"]
                                      # list of str, default []: the modules whose import
                                      # registers the project's steps and workflows (see
                                      # fxtr.experiment.registry); what a process running a job
                                      # it did not launch imports
    stale_after = 30                  # number > 0, default 30: seconds without a heartbeat after
                                      # which a job's lease is presumed abandoned. Every process
                                      # of a project must agree on it, since one process's
                                      # heartbeat is judged by another's threshold
    default_remote = "team"           # str, optional: the remote that a push or a pull uses when
                                      # it names no remote

    [tool.fxtr.views]                 # optional: the project's renderers, as a view bundle
    package = "views"                 # str, required in the table: the renderer package's
                                      # directory, relative to the project root. The package
                                      # itself says where its bundle is (fxtr.project.views)

    [tool.fxtr.remotes.team]          # optional, any number: a place to push jobs to and pull
    kind = "hub"                      # jobs from, for every checkout. kind (str) is required;
    url = "https://hub.transluce.org" # the kind declares the other settings and checks them
    project = "6f1c…"                 # when it builds the remote (see fxtr.project.remotes)

**``fxtr.local.toml``**, gitignored, how *this machine* reaches the project's state::

    database_url = "postgresql+asyncpg://fxtr:fxtr@localhost:55433/fxtr"
                                      # str, required unless FXTR_DATABASE_URL is set
    schema = "fxtr_my_experiments"    # str matching [a-z_][a-z0-9_]*, at most 63 characters,
                                      # default "fxtr_<name>" with the name lowercased and other
                                      # characters replaced by "_": the Postgres schema the
                                      # project's tables live in
    default_remote = "mine"           # str, optional: overrides the committed default_remote

    [remotes.mine]                    # optional, any number: remotes of this machine only. A
    kind = "oci"                      # name may not be configured in both files
    repository = "ghcr.io/me/jobs"

``fxtr init`` writes the local file. The :data:`DATABASE_URL_ENV_VAR` environment variable
(``FXTR_DATABASE_URL``) overrides its ``database_url`` when set, and stands in for the file when
there is none, so a test or a CI job can point a project elsewhere without editing it.

**``.env``**, gitignored, this machine's secrets, such as the API keys of the model providers an
experiment calls. :func:`load_env_file` reads it into the process environment when a project's
client is opened, before the project's modules are imported, so a launcher, ``fxtr run``, and
``fxtr resume`` all see it. Its values override variables already set in the environment, so the
project's keys are the ones used; an empty value is skipped, so a placeholder like
``OPENAI_API_KEY=`` sets nothing and leaves any value from the shell in place.

**Reading rules.** Each table is read strictly against the settings declared above
(:data:`FXTR_SETTINGS`, :data:`LOCAL_SETTINGS`): a key that is not declared, or a value of the
wrong type, is a :class:`ConfigError`, so a misspelled setting cannot be silently ignored.

**Remotes.** The project's remotes are those of both files: committed ones, which every
checkout has, and this machine's own. A name configured in both files is an error, since a push
that names it could mean either. The default remote is the local file's ``default_remote`` if
it sets one, else the committed one. ``fxtr remote`` edits both files
(:mod:`fxtr.project.remotes.files`).

**Finding a project.** :func:`find_project_root` walks up from an *anchor*, a file or directory,
to the nearest directory holding a ``pyproject.toml``, which must have the ``[tool.fxtr]`` table:
the nearest pyproject is the Python project the anchor belongs to, and if it is not a fxtr
project that is an error rather than a reason to keep walking. Which anchor to use is the
caller's business: a launch anchors on the root workflow's source file (the code that is
executing), the CLI on the working directory. :func:`load_project` reads a root into a
:class:`Project`; :func:`locate_project` does both.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from collections.abc import Callable, Mapping, MutableMapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

PYPROJECT_FILENAME = "pyproject.toml"
LOCAL_CONFIG_FILENAME = "fxtr.local.toml"
ENV_FILENAME = ".env"
"""The project's secrets, beside its configuration files (:func:`load_env_file`)."""
TOOL_TABLE = "fxtr"
"""The ``[tool.<name>]`` table that marks a pyproject as a fxtr project."""

DATABASE_URL_ENV_VAR = "FXTR_DATABASE_URL"
"""The environment variable whose value, when set, overrides the local config's database URL."""

DEFAULT_STALE_AFTER = timedelta(seconds=30)
"""The default of ``[tool.fxtr] stale_after``."""

_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*")
_SCHEMA_NAME_MAX = 63  # Postgres keeps this many characters of an identifier


class ProjectError(Exception):
    """A project could not be located or its configuration read."""


class ProjectNotFoundError(ProjectError):
    """No directory from the anchor up holds a ``pyproject.toml``, or the nearest one has no
    ``[tool.fxtr]`` table."""


class ConfigError(ProjectError):
    """A configuration file is missing, malformed, or holds a setting it should not."""


# ---- the settings, declared ----


@dataclasses.dataclass(frozen=True)
class Setting:
    """One setting a configuration table may hold.

    Attributes:
        key: The TOML key.
        type: The Python type(s) a valid value has (``bool`` never passes for a numeric type).
        default: The value when the key is absent, or :data:`REQUIRED`.
        describe: What the value must be, for error messages.
        check: A further validity check on a present value, returning what is wrong or ``None``.
    """

    key: str
    type: type | tuple[type, ...]
    default: Any
    describe: str
    check: Callable[[Any], str | None] = lambda _value: None


REQUIRED = object()
"""A :class:`Setting` default meaning the key must be present, and a string value not empty: a
required string names something (a URL, an id, a path), which an empty string cannot."""


def _positive_number(value: float) -> str | None:
    return None if value > 0 else "must be greater than zero"


def _module_names(value: list[Any]) -> str | None:
    if all(isinstance(m, str) and m for m in value):
        return None
    return "must contain module names (non-empty strings)"


def _schema_name(value: str) -> str | None:
    if _SCHEMA_NAME.fullmatch(value) and len(value) <= _SCHEMA_NAME_MAX:
        return None
    return f"must match {_SCHEMA_NAME.pattern!r} and be at most {_SCHEMA_NAME_MAX} characters"


def _relative_path(value: str) -> str | None:
    if value and not Path(value).is_absolute():
        return None
    return "must be a non-empty path relative to the project root"


FXTR_SETTINGS = (
    Setting("modules", list, [], "a list of module names", _module_names),
    Setting("views", dict, None, "a table ([tool.fxtr.views])"),
    Setting("default_remote", str, None, "the name of a configured remote"),
    Setting("remotes", dict, {}, "a table of remotes ([tool.fxtr.remotes.<name>])"),
    Setting(
        "stale_after",
        (int, float),
        DEFAULT_STALE_AFTER.total_seconds(),
        "a positive number of seconds",
        _positive_number,
    ),
)
"""The ``[tool.fxtr]`` table of ``pyproject.toml``."""

VIEWS_SETTINGS = (
    Setting("package", str, REQUIRED, "a path relative to the project root", _relative_path),
)
"""The ``[tool.fxtr.views]`` table."""

LOCAL_SETTINGS = (
    Setting("database_url", str, REQUIRED, "a database URL"),
    Setting("schema", str, None, "a Postgres schema name", _schema_name),
    Setting("default_remote", str, None, "the name of a configured remote"),
    Setting("remotes", dict, {}, "a table of remotes ([remotes.<name>])"),
)
"""The ``fxtr.local.toml`` table. ``database_url`` is required of the file only when
:data:`DATABASE_URL_ENV_VAR` does not supply it; ``schema`` defaults to
:func:`default_schema_name` of the project's name."""

COMMITTED_REMOTES_TABLE = f"tool.{TOOL_TABLE}.remotes"
"""The dotted name of the table of remotes in ``pyproject.toml``."""

LOCAL_REMOTES_TABLE = "remotes"
"""The dotted name of the table of remotes in ``fxtr.local.toml``."""


# ---- the project ----


@dataclasses.dataclass(frozen=True)
class ViewsConfig:
    """``[tool.fxtr.views]``: where the project's renderer package is (see
    :mod:`fxtr.project.views` for how the package says where its bundle is).

    Attributes:
        package: The renderer package's directory, absolute.
    """

    package: Path


@dataclasses.dataclass(frozen=True)
class RemoteConfig:
    """One remote's table: ``[tool.fxtr.remotes.<name>]`` of ``pyproject.toml`` or
    ``[remotes.<name>]`` of ``fxtr.local.toml``. A remote is a place that a project pushes jobs
    to and pulls jobs from (see :mod:`fxtr.project.remotes`).

    Attributes:
        name: The name of the remote. It is the key of the table.
        kind: The kind of the remote. The kind is the implementation of the remote interface
            that reaches the remote.
        settings: The other keys of the table, as the file writes them. The kind declares and
            checks them when it builds the remote. Thus, an error in them shows when you use the
            remote, not when the project loads.
        file: The file whose table configures the remote, or ``None`` for a remote that code
            builds without a file.
    """

    name: str
    kind: str
    settings: Mapping[str, Any]
    file: Path | None = None

    @property
    def local(self) -> bool:
        """True if ``fxtr.local.toml`` configures the remote, for this machine only."""
        return self.file is not None and self.file.name == LOCAL_CONFIG_FILENAME

    @property
    def where(self) -> str:
        """The remote's table and its file, for messages."""
        if self.file is None:
            return f"remote {self.name!r}"
        table = LOCAL_REMOTES_TABLE if self.local else COMMITTED_REMOTES_TABLE
        return f"[{table}.{self.name}] in {self.file}"


@dataclasses.dataclass(frozen=True)
class RemotesConfig:
    """The remotes of both files (see the module docstring).

    Attributes:
        by_name: The remotes that both files configure, by name.
        default: The name of the remote that a push or a pull uses when it names no remote, or
            ``None``.
    """

    by_name: Mapping[str, RemoteConfig] = dataclasses.field(default_factory=dict)
    default: str | None = None


@dataclasses.dataclass(frozen=True)
class Project:
    """A located project and its configuration (see the module docstring).

    Attributes:
        root: The project root: the directory of its ``pyproject.toml``.
        name: ``[project].name``.
        modules: ``[tool.fxtr] modules``, in the order given.
        stale_after: ``[tool.fxtr] stale_after``, as a duration.
        database_url: How this machine reaches the project's database.
        schema: The database schema (namespace) the project's tables live in.
        views: ``[tool.fxtr.views]``, or ``None`` for a project without renderers of its own.
        remotes: The remotes of both files.
    """

    root: Path
    name: str
    modules: tuple[str, ...]
    stale_after: timedelta
    database_url: str
    schema: str
    views: ViewsConfig | None = None
    remotes: RemotesConfig = dataclasses.field(default_factory=RemotesConfig)

    @property
    def pyproject_path(self) -> Path:
        return self.root / PYPROJECT_FILENAME

    @property
    def local_config_path(self) -> Path:
        return self.root / LOCAL_CONFIG_FILENAME


def find_project_root(anchor: Path) -> Path:
    """The project root of the nearest ``pyproject.toml`` at or above ``anchor`` (a file or a
    directory).

    Raises:
        ProjectNotFoundError: If no directory from the anchor up to the filesystem root holds a
            ``pyproject.toml``, or the nearest one has no ``[tool.fxtr]`` table.
    """
    anchor = anchor.resolve()
    start = anchor if anchor.is_dir() else anchor.parent
    for directory in (start, *start.parents):
        pyproject = directory / PYPROJECT_FILENAME
        if pyproject.is_file():
            if TOOL_TABLE in _table(_read_toml(pyproject), "tool"):
                return directory
            raise ProjectNotFoundError(
                f"{pyproject} is the nearest pyproject above {start}, and it has no"
                f" [tool.{TOOL_TABLE}] table: it is not a fxtr project"
            )
    raise ProjectNotFoundError(f"No {PYPROJECT_FILENAME} found from {start} upward")


def load_project(root: Path, *, env: Mapping[str, str] = os.environ) -> Project:
    """Read the project at ``root`` (see the module docstring for the two files).

    Args:
        root: The project root.
        env: Where :data:`DATABASE_URL_ENV_VAR` is looked up; the process environment by default.

    Raises:
        ConfigError: If a file is malformed, a setting is unknown or invalid, the project has no
            name, or nothing says where the database is (no local file and no environment
            override).
    """
    root = root.resolve()
    pyproject_path = root / PYPROJECT_FILENAME
    pyproject = _read_toml(pyproject_path)
    name = _project_name(pyproject, pyproject_path)
    fxtr = _read_fxtr_table(pyproject, pyproject_path)

    local_path = root / LOCAL_CONFIG_FILENAME
    local_table = _read_toml(local_path) if local_path.is_file() else {}
    url_override = env.get(DATABASE_URL_ENV_VAR)
    local = read_settings(
        local_table,
        LOCAL_SETTINGS,
        str(local_path),
        # The environment supplies the URL, so the file need not.
        optional=("database_url",) if url_override else (),
        missing_hint=(
            None
            if url_override or local_path.is_file()
            else f"{local_path} does not exist and {DATABASE_URL_ENV_VAR} is not set: run"
            " `fxtr init` in the project to say where its database is"
        ),
    )
    return Project(
        root=root,
        name=name,
        modules=tuple(fxtr["modules"]),
        stale_after=timedelta(seconds=fxtr["stale_after"]),
        database_url=url_override or local["database_url"],
        schema=local["schema"] if local["schema"] is not None else default_schema_name(name),
        views=_views(root, fxtr["views"], pyproject_path),
        remotes=_remotes_config(fxtr, pyproject_path, local, local_path),
    )


def _project_name(pyproject: Mapping[str, Any], pyproject_path: Path) -> str:
    name = _table(pyproject, "project").get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{pyproject_path} has no [project] name")
    return name


def load_project_name(root: Path) -> str:
    """Read only the name of the project at ``root``, with no database configured.

    Raises:
        ConfigError: If ``pyproject.toml`` is malformed, or the project has no name.
    """
    pyproject_path = root.resolve() / PYPROJECT_FILENAME
    return _project_name(_read_toml(pyproject_path), pyproject_path)


def load_remotes(root: Path) -> RemotesConfig:
    """Read only the remotes of the project at ``root``, from both files: what the remote
    commands need, with no database configured.

    Raises:
        ConfigError: If a file is malformed, or a setting is unknown or not valid (see
            :func:`load_project`).
    """
    root = root.resolve()
    pyproject_path = root / PYPROJECT_FILENAME
    fxtr = _read_fxtr_table(_read_toml(pyproject_path), pyproject_path)
    local_path = root / LOCAL_CONFIG_FILENAME
    local_table = _read_toml(local_path) if local_path.is_file() else {}
    local = read_settings(local_table, LOCAL_SETTINGS, str(local_path), optional=("database_url",))
    return _remotes_config(fxtr, pyproject_path, local, local_path)


def _remotes_config(
    committed: Mapping[str, Any],
    pyproject_path: Path,
    local: Mapping[str, Any],
    local_path: Path,
) -> RemotesConfig:
    """Read and merge the remotes of both files (see the module docstring).

    Args:
        committed: ``[tool.fxtr]`` of ``pyproject.toml``, read against :data:`FXTR_SETTINGS`.
        pyproject_path: That file, for error messages.
        local: ``fxtr.local.toml``, read against :data:`LOCAL_SETTINGS`.
        local_path: That file, for error messages.

    Raises:
        ConfigError: If a remote is not a table or has no ``kind``, or a remote is configured in
            both files.
    """
    tables = (
        (committed, pyproject_path, COMMITTED_REMOTES_TABLE),
        (local, local_path, LOCAL_REMOTES_TABLE),
    )
    remotes: dict[str, RemoteConfig] = {}
    default_remote: str | None = None
    for settings, path, name in tables:
        for remote in _remotes(settings["remotes"], path, f"[{name}] in {path}"):
            if remote.name in remotes:
                raise ConfigError(
                    f"The remote {remote.name!r} is configured in both {pyproject_path} and"
                    f" {local_path}: rename or remove one of them"
                )
            remotes[remote.name] = remote
        if settings["default_remote"] is not None:
            default_remote = settings["default_remote"]
    return RemotesConfig(by_name=remotes, default=default_remote)


def _remotes(table: Mapping[str, Any], path: Path, where: str) -> list[RemoteConfig]:
    """Read the remotes of one file's ``remotes`` table. Each remote must be a table with a
    ``kind`` (a string). This function does not read the settings of the kind: the kind reads
    them when it builds the remote.

    Args:
        table: The ``remotes`` table.
        path: The file.
        where: The table of remotes and its file, for error messages.

    Raises:
        ConfigError: If a remote is not a table, or has no ``kind``.
    """
    remotes: list[RemoteConfig] = []
    for name, remote in table.items():
        if not isinstance(remote, dict):
            raise ConfigError(f"{where}: {name} must be a table")
        kind = remote.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ConfigError(f"{where}: {name} needs a kind (a string)")
        settings = {key: value for key, value in remote.items() if key != "kind"}
        remotes.append(RemoteConfig(name=name, kind=kind, settings=settings, file=path))
    return remotes


def load_views(root: Path) -> ViewsConfig | None:
    """Read only ``[tool.fxtr.views]`` of the project at ``root``: what a command that builds the
    bundle needs, with no local configuration required.

    Raises:
        ConfigError: If ``pyproject.toml`` is malformed or a setting is unknown or invalid.
    """
    root = root.resolve()
    pyproject_path = root / PYPROJECT_FILENAME
    fxtr = _read_fxtr_table(_read_toml(pyproject_path), pyproject_path)
    return _views(root, fxtr["views"], pyproject_path)


def _read_fxtr_table(pyproject: Mapping[str, Any], pyproject_path: Path) -> dict[str, Any]:
    tool = _table(pyproject, "tool").get(TOOL_TABLE)
    if not isinstance(tool, dict):
        raise ConfigError(f"{pyproject_path} has no [tool.{TOOL_TABLE}] table")
    return read_settings(tool, FXTR_SETTINGS, f"[tool.{TOOL_TABLE}] in {pyproject_path}")


def _views(root: Path, table: Mapping[str, Any] | None, pyproject_path: Path) -> ViewsConfig | None:
    if table is None:
        return None
    views = read_settings(table, VIEWS_SETTINGS, f"[tool.{TOOL_TABLE}.views] in {pyproject_path}")
    return ViewsConfig(package=root / views["package"])


def locate_project(anchor: Path, *, env: Mapping[str, str] = os.environ) -> Project:
    """:func:`find_project_root` then :func:`load_project`."""
    return load_project(find_project_root(anchor), env=env)


def load_env_file(root: Path, *, env: MutableMapping[str, str] = os.environ) -> dict[str, str]:
    """Set the variables in the project's ``.env`` at ``root`` in ``env``, overriding what is
    there, and return them. An empty value is skipped; a missing file sets nothing. See the module
    docstring."""
    path = root / ENV_FILENAME
    if not path.is_file():
        return {}
    loaded = {name: value for name, value in dotenv_values(path).items() if value}
    env.update(loaded)
    return loaded


def default_schema_name(project_name: str) -> str:
    """The schema a project's tables live in unless the local config says otherwise:
    ``fxtr_<name>`` with the name lowercased and every character outside ``[a-z0-9_]`` replaced
    by an underscore, cut to the identifier length Postgres keeps."""
    normalized = re.sub(r"[^a-z0-9_]", "_", project_name.lower())
    return f"fxtr_{normalized}"[:_SCHEMA_NAME_MAX]


def write_local_config(root: Path, *, database_url: str, schema: str | None = None) -> Path:
    """Write the project's database settings to its ``fxtr.local.toml`` (what ``fxtr init``
    does), and return the file's path. If the file exists, this function replaces its
    ``database_url`` and ``schema`` and keeps the rest, such as this machine's remotes.
    ``schema`` is written only when given; otherwise the default applies when the file is read.
    """
    import tomlkit
    from tomlkit.exceptions import TOMLKitError

    path = root / LOCAL_CONFIG_FILENAME
    try:
        document = tomlkit.parse(path.read_text())
    except (OSError, TOMLKitError):
        # None yet, or one that cannot be read: nothing in it can be kept.
        document = tomlkit.document()
        document.add(
            tomlkit.comment(
                "How this machine reaches the project's fxtr state. Local; not committed."
            )
        )
    document["database_url"] = database_url
    if schema is not None:
        document["schema"] = schema
    elif "schema" in document:
        del document["schema"]
    path.write_text(tomlkit.dumps(document))
    return path


# ---- reading ----


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} does not exist") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    table = data.get(key, {})
    return table if isinstance(table, dict) else {}


def read_settings(
    table: Mapping[str, Any],
    settings: tuple[Setting, ...],
    where: str,
    *,
    optional: tuple[str, ...] = (),
    missing_hint: str | None = None,
) -> dict[str, Any]:
    """Read ``table`` strictly against ``settings``: every declared key with its type and check,
    defaults for absent ones, and an error for anything undeclared. This function reads the
    tables of the configuration files. It also reads the table of each remote,
    against the settings of its kind (:mod:`fxtr.project.remotes`).

    Args:
        table: The table to read.
        settings: The settings that the table can hold.
        where: Where the table is, for error messages.
        optional: Required settings that may be absent this time (something else supplies them).
        missing_hint: What to say instead of the generic message when a required setting is
            absent.
    """
    unknown = set(table) - {s.key for s in settings}
    if unknown:
        raise ConfigError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for setting in settings:
        if setting.key not in table:
            if setting.default is REQUIRED and setting.key not in optional:
                raise ConfigError(missing_hint or f"{where}: {setting.key} is required")
            values[setting.key] = None if setting.default is REQUIRED else setting.default
            continue
        value = table[setting.key]
        wrong_type = not isinstance(value, setting.type) or (
            isinstance(value, bool) and bool not in _types(setting.type)
        )
        problem = None if wrong_type else setting.check(value)
        if problem is None and setting.default is REQUIRED and value == "":
            problem = "must not be empty"
        if wrong_type or problem is not None:
            detail = "" if problem is None else f" ({problem})"
            raise ConfigError(
                f"{where}: {setting.key} must be {setting.describe}{detail}, got {value!r}"
            )
        values[setting.key] = value
    return values


def _types(type_: type | tuple[type, ...]) -> tuple[type, ...]:
    return type_ if isinstance(type_, tuple) else (type_,)


__all__ = [
    "DATABASE_URL_ENV_VAR",
    "COMMITTED_REMOTES_TABLE",
    "DEFAULT_STALE_AFTER",
    "ENV_FILENAME",
    "FXTR_SETTINGS",
    "LOCAL_CONFIG_FILENAME",
    "LOCAL_REMOTES_TABLE",
    "LOCAL_SETTINGS",
    "PYPROJECT_FILENAME",
    "REQUIRED",
    "ConfigError",
    "Project",
    "ProjectError",
    "ProjectNotFoundError",
    "RemoteConfig",
    "RemotesConfig",
    "Setting",
    "VIEWS_SETTINGS",
    "ViewsConfig",
    "default_schema_name",
    "find_project_root",
    "load_env_file",
    "load_project",
    "load_remotes",
    "load_views",
    "locate_project",
    "read_settings",
    "write_local_config",
]
