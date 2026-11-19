"""Tests for locating a project and reading its two configuration files."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from fxtr.project.config import (
    DATABASE_URL_ENV_VAR,
    DEFAULT_STALE_AFTER,
    LOCAL_CONFIG_FILENAME,
    ConfigError,
    ProjectNotFoundError,
    RemoteConfig,
    RemotesConfig,
    ViewsConfig,
    default_schema_name,
    find_project_root,
    load_env_file,
    load_project,
    load_views,
    locate_project,
    write_local_config,
)

PYPROJECT = """
[project]
name = "My-Project"

[tool.fxtr]
modules = ["my_project.steps", "my_project.flows"]
"""
LOCAL = """
database_url = "postgresql+asyncpg://u:p@localhost/db"
"""


def a_project(root: Path, pyproject: str = PYPROJECT, local: str | None = LOCAL) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(pyproject)
    if local is not None:
        (root / "fxtr.local.toml").write_text(local)
    return root


# ---- finding ----


def test_the_root_is_found_from_a_nested_file_or_directory(tmp_path: Path) -> None:
    root = a_project(tmp_path / "repo" / "proj")
    nested = root / "src" / "my_project"
    nested.mkdir(parents=True)
    (nested / "flows.py").write_text("")
    assert find_project_root(nested / "flows.py") == root
    assert find_project_root(nested) == root
    assert find_project_root(root) == root


def test_the_nearest_pyproject_must_be_an_fxtr_project(tmp_path: Path) -> None:
    a_project(tmp_path / "repo")
    member = tmp_path / "repo" / "lib"
    member.mkdir()
    (member / "pyproject.toml").write_text('[project]\nname = "lib"\n')
    with pytest.raises(ProjectNotFoundError, match="nearest pyproject"):
        find_project_root(member)


def test_no_project_is_an_error_naming_the_start(tmp_path: Path) -> None:
    start = tmp_path / "nowhere"
    start.mkdir()
    with pytest.raises(ProjectNotFoundError, match=str(start)):
        find_project_root(start)


# ---- reading ----


def test_both_files_are_read(tmp_path: Path) -> None:
    root = a_project(tmp_path / "proj")
    project = load_project(root, env={})
    assert project.root == root.resolve()
    assert project.name == "My-Project"
    assert project.modules == ("my_project.steps", "my_project.flows")
    assert project.stale_after == DEFAULT_STALE_AFTER
    assert project.database_url == "postgresql+asyncpg://u:p@localhost/db"
    assert project.schema == "fxtr_my_project"
    assert project.pyproject_path == root.resolve() / "pyproject.toml"
    assert project.local_config_path == root.resolve() / "fxtr.local.toml"


def test_locate_is_find_then_load(tmp_path: Path) -> None:
    root = a_project(tmp_path / "proj")
    (root / "deep").mkdir()
    assert locate_project(root / "deep", env={}).root == root.resolve()


def test_settings_are_optional_and_typed(tmp_path: Path) -> None:
    root = a_project(
        tmp_path / "proj",
        pyproject='[project]\nname = "p"\n[tool.fxtr]\nstale_after = 12.5\n',
        local='database_url = "sqlite:///x"\nschema = "custom_schema"\n',
    )
    project = load_project(root, env={})
    assert project.modules == ()
    assert project.stale_after == timedelta(seconds=12.5)
    assert project.schema == "custom_schema"


def test_the_environment_overrides_the_local_url(tmp_path: Path) -> None:
    root = a_project(tmp_path / "proj")
    project = load_project(root, env={DATABASE_URL_ENV_VAR: "postgresql+asyncpg://elsewhere/db"})
    assert project.database_url == "postgresql+asyncpg://elsewhere/db"
    assert project.schema == "fxtr_my_project"  # the file's other settings still apply


def test_the_environment_stands_in_for_a_missing_local_file(tmp_path: Path) -> None:
    root = a_project(tmp_path / "proj", local=None)
    project = load_project(root, env={DATABASE_URL_ENV_VAR: "postgresql+asyncpg://x/db"})
    assert project.database_url == "postgresql+asyncpg://x/db"


def test_no_database_is_an_error_naming_fxtr_init(tmp_path: Path) -> None:
    root = a_project(tmp_path / "proj", local=None)
    with pytest.raises(ConfigError, match="fxtr init"):
        load_project(root, env={})


@pytest.mark.parametrize(
    ("pyproject", "local", "message"),
    [
        ('[project]\nname = "p"\n[tool.fxtr]\nmodule = ["x"]\n', LOCAL, "unknown setting"),
        ('[project]\nname = "p"\n[tool.fxtr]\nmodules = "x"\n', LOCAL, "modules must be"),
        ('[project]\nname = "p"\n[tool.fxtr]\nmodules = [1]\n', LOCAL, "module names"),
        ('[project]\nname = "p"\n[tool.fxtr]\nstale_after = "30"\n', LOCAL, "stale_after must be"),
        ('[project]\nname = "p"\n[tool.fxtr]\nstale_after = 0\n', LOCAL, "greater than zero"),
        ('[project]\nname = "p"\n[tool.fxtr]\nstale_after = true\n', LOCAL, "stale_after must be"),
        ("[tool.fxtr]\n", LOCAL, "no \\[project\\] name"),
        ('[project]\nname = "p"\n', LOCAL, "no \\[tool.fxtr\\] table"),
        (PYPROJECT, 'database_url = "x"\nschema = "Has-Caps"\n', "schema must be"),
        (PYPROJECT, 'database_url = "x"\nurl = "y"\n', "unknown setting"),
        (
            PYPROJECT,
            'database_url = ""\n',
            "database_url must be a database URL \\(must not be empty",
        ),
        (PYPROJECT, "not toml = = =\n", "not valid TOML"),
        (PYPROJECT + "[tool.fxtr.views]\n", LOCAL, "package is required"),
        (
            PYPROJECT + '[tool.fxtr.views]\npackage = "/abs"\n',
            LOCAL,
            "relative to the project root",
        ),
        (PYPROJECT + '[tool.fxtr.views]\npackage = "v"\ndist = "d"\n', LOCAL, "unknown setting"),
        (PYPROJECT, LOCAL + "remotes = 1\n", "remotes must be"),
        (PYPROJECT, LOCAL + "default_remote = 1\n", "default_remote must be"),
        (PYPROJECT, LOCAL + "[remotes]\nteam = 1\n", r"\[remotes\] in .*: team must be a table"),
        (
            PYPROJECT,
            LOCAL + '[remotes.team]\nrepository = "r"\n',
            "team needs a kind",
        ),
        (
            PYPROJECT + "[tool.fxtr.remotes]\nteam = 1\n",
            LOCAL,
            r"\[tool.fxtr.remotes\] in .*: team must be a table",
        ),
        (
            PYPROJECT + '[tool.fxtr.remotes.team]\nkind = "oci"\n',
            LOCAL + '[remotes.team]\nkind = "oci"\n',
            "'team' is configured in both",
        ),
    ],
)
def test_malformed_configuration_is_refused(
    tmp_path: Path, pyproject: str, local: str, message: str
) -> None:
    root = a_project(tmp_path / "proj", pyproject=pyproject, local=local)
    with pytest.raises(ConfigError, match=message):
        load_project(root, env={})


# ---- remotes ----


def test_remotes_are_read_with_their_kind_and_the_default(tmp_path: Path) -> None:
    local = (
        LOCAL
        + 'default_remote = "team"\n'
        + '[remotes.team]\nkind = "oci"\nrepository = "ghcr.io/org/jobs"\n'
        + '[remotes.mine]\nkind = "oci"\nrepository = "localhost:5000/jobs"\n'
        + "insecure = true\n"
    )
    root = a_project(tmp_path / "proj", local=local).resolve()
    project = load_project(root, env={})
    assert project.remotes == RemotesConfig(
        by_name={
            "team": RemoteConfig(
                "team", "oci", {"repository": "ghcr.io/org/jobs"}, root / "fxtr.local.toml"
            ),
            "mine": RemoteConfig(
                "mine",
                "oci",
                {"repository": "localhost:5000/jobs", "insecure": True},
                root / "fxtr.local.toml",
            ),
        },
        default="team",
    )
    # Without the tables, a project has no remotes and no default.
    plain = load_project(a_project(tmp_path / "plain"), env={})
    assert plain.remotes == RemotesConfig()


def test_committed_and_local_remotes_are_the_projects_remotes(tmp_path: Path) -> None:
    """Every checkout has the remotes of pyproject.toml; fxtr.local.toml adds this machine's
    own and can choose another default for this machine."""
    pyproject = (
        PYPROJECT
        + 'default_remote = "team"\n'
        + '[tool.fxtr.remotes.team]\nkind = "hub"\nurl = "https://hub.test"\n'
    )
    root = a_project(tmp_path / "proj", pyproject=pyproject).resolve()
    shared = load_project(root, env={}).remotes
    assert shared.default == "team"
    (team,) = shared.by_name.values()
    assert (team.file, team.local) == (root / "pyproject.toml", False)
    assert team.where == f"[tool.fxtr.remotes.team] in {root / 'pyproject.toml'}"

    local = LOCAL + 'default_remote = "mine"\n[remotes.mine]\nkind = "oci"\n'
    (root / "fxtr.local.toml").write_text(local)
    mine = load_project(root, env={}).remotes
    assert list(mine.by_name) == ["team", "mine"]
    assert mine.default == "mine"
    assert mine.by_name["mine"].local
    assert mine.by_name["mine"].where == f"[remotes.mine] in {root / 'fxtr.local.toml'}"


# ---- the views table ----


def test_the_views_table_names_the_renderer_package(tmp_path: Path) -> None:
    root = a_project(
        tmp_path / "proj", pyproject=PYPROJECT + '[tool.fxtr.views]\npackage = "ui/views"\n'
    )
    project = load_project(root, env={})
    assert project.views == ViewsConfig(package=root.resolve() / "ui" / "views")
    assert load_project(a_project(tmp_path / "plain"), env={}).views is None


def test_load_views_needs_no_local_configuration(tmp_path: Path) -> None:
    pyproject = PYPROJECT + '[tool.fxtr.views]\npackage = "views"\n'
    root = a_project(tmp_path / "proj", pyproject=pyproject, local=None)
    assert load_views(root) == ViewsConfig(package=root.resolve() / "views")
    assert load_views(a_project(tmp_path / "plain", local=None)) is None


# ---- the schema default and the writer ----


def test_default_schema_name_is_a_valid_identifier() -> None:
    assert default_schema_name("my-project") == "fxtr_my_project"
    assert default_schema_name("Weird.Name 2") == "fxtr_weird_name_2"
    assert len(default_schema_name("x" * 100)) == 63


def test_write_local_config_round_trips(tmp_path: Path) -> None:
    root = a_project(tmp_path / "proj", local=None)
    path = write_local_config(root, database_url='postgresql+asyncpg://u:"p"@h/db')
    assert path == root / "fxtr.local.toml"
    project = load_project(root, env={})
    assert project.database_url == 'postgresql+asyncpg://u:"p"@h/db'
    assert project.schema == "fxtr_my_project"
    write_local_config(root, database_url="sqlite:///x", schema="other")
    assert load_project(root, env={}).schema == "other"


def test_write_local_config_keeps_the_rest_of_the_file(tmp_path: Path) -> None:
    """``fxtr init --force`` replaces the database settings, not this machine's remotes."""
    local = (
        LOCAL
        + 'schema = "old"\n'
        + "# this machine's own registry\n"
        + '[remotes.mine]\nkind = "oci"\nrepository = "localhost:5000/jobs"\n'
    )
    root = a_project(tmp_path / "proj", local=local)
    write_local_config(root, database_url="postgresql+asyncpg://u:p@elsewhere/db")
    project = load_project(root, env={})
    assert project.database_url == "postgresql+asyncpg://u:p@elsewhere/db"
    assert project.schema == "fxtr_my_project"
    assert list(project.remotes.by_name) == ["mine"]
    assert "# this machine's own registry" in (root / "fxtr.local.toml").read_text()


def test_load_env_file_sets_nonempty_variables_over_the_environment(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "ANTHROPIC_API_KEY=sk-ant\n"
        "OPENAI_API_KEY=\n"
        'QUOTED="a value"\n'
        "export EXPORTED=yes\n"
        "ALREADY=from-file\n"
    )
    env = {"ALREADY": "from-shell", "OPENAI_API_KEY": "sk-shell"}

    loaded = load_env_file(tmp_path, env=env)

    assert loaded == {
        "ANTHROPIC_API_KEY": "sk-ant",
        "QUOTED": "a value",
        "EXPORTED": "yes",
        "ALREADY": "from-file",
    }
    assert env == {"OPENAI_API_KEY": "sk-shell", **loaded}  # the empty placeholder kept the shell's


def test_load_env_file_without_a_file_sets_nothing(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert load_env_file(tmp_path, env=env) == {} and env == {}


def test_write_local_config_replaces_a_file_it_cannot_read(tmp_path: Path) -> None:
    (tmp_path / LOCAL_CONFIG_FILENAME).write_text("not [ toml")
    path = write_local_config(tmp_path, database_url="sqlite:///x")
    assert 'database_url = "sqlite:///x"' in path.read_text()
