"""``fxtr new``: empty and example projects outside and inside a checkout and published ones
(depending on fxtr from the package index), the vendored TypeScript packages, generated Python
checked by Pyright, the example launcher run on Postgres, the refusals, the renderer package's
independent pnpm environment and versions, and the command's output. Opt-in
(``FXTR_TEST_EXAMPLE_PROJECT=1`` with ``FXTR_TEST_DATABASE_URL``, since
it syncs a venv): a written project synced (pinned to this repository's locked versions),
committed, and run as a job through ``uv run``."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from anyio import to_thread

from fxtr.project import cli
from fxtr.project import new as new_module
from fxtr.project.config import find_project_root, load_env_file, load_views
from fxtr.project.new import (
    FXTR_EXTRAS,
    PACKAGE_MANAGER,
    VENDORED_DEV_DEPENDENCIES,
    VIEWS_DEV_DEPENDENCIES,
    WORKFLOW_NAME,
    NewProjectError,
    find_checkout,
    in_repository,
    project_names,
    render,
    write_project,
)
from fxtr.project.ts_packages import BASE_FILES, PROJECT_PACKAGES
from fxtr.project.views import VENDOR_DIR

if TYPE_CHECKING:
    from fxtr.runner.postgres.database import Database

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parents[2]
OPT_IN_ENV_VAR = "FXTR_TEST_EXAMPLE_PROJECT"


def a_checkout(root: Path) -> Path:
    """A stand-in for a fxtr checkout at ``root``: what a new project refers to in one (fxtr's
    pyproject, the TypeScript packages, the shared configs), with this repository's
    ``pnpm-workspace.yaml``."""
    (root / "fxtr").mkdir(parents=True)
    (root / "fxtr" / "pyproject.toml").write_text('[project]\nname = "fxtr"\n')
    for package in PROJECT_PACKAGES:
        (root / "ts" / "packages" / package / "src").mkdir(parents=True)
        manifest = {"name": package, "exports": {".": "./src/index.ts"}}
        (root / "ts" / "packages" / package / "package.json").write_text(json.dumps(manifest))
        (root / "ts" / "packages" / package / "src" / "index.ts").write_text("export {};\n")
    for name in BASE_FILES:
        (root / "ts" / name).write_text("{}\n")
    (root / "pyright-base.json").write_text("{}\n")
    shutil.copy(REPO / "pnpm-workspace.yaml", root / "pnpm-workspace.yaml")
    return root


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


# ---- names and templates ----


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("demo", ("demo", "demo")),
        ("My_Project", ("my-project", "my_project")),
        ("a.b--c", ("a-b-c", "a_b_c")),
    ],
)
def test_a_name_is_normalized_into_a_project_and_a_package(
    name: str, expected: tuple[str, str]
) -> None:
    assert project_names(name) == expected


@pytest.mark.parametrize("name", ["", "-demo", "my project", "1st", "class", "fxtr"])
def test_a_name_that_makes_no_project_or_package_is_refused(name: str) -> None:
    with pytest.raises(NewProjectError, match="--name"):
        project_names(name)


def test_render_fills_values_and_keeps_sections_by_flag() -> None:
    template = "a {{x}}\n{{#on}}\nkept {{x}}\n{{/on}}\n{{^on}}\ndropped\n{{/on}}\nend\n\n"
    assert render(template, {"x": "1"}, {"on": True}) == "a 1\nkept 1\nend\n"
    assert render(template, {"x": "1"}, {"on": False}) == "a 1\ndropped\nend\n"
    with pytest.raises(KeyError):
        render("{{y}}", {"x": "1"}, {})
    with pytest.raises(KeyError):
        render("{{#off}}x{{/off}}", {}, {"on": True})


def test_render_keeps_a_section_inside_another_by_both_flags() -> None:
    template = "{{#outer}}\na {{#inner}}b{{/inner}}{{^inner}}c{{/inner}} d\n{{/outer}}\n"
    assert render(template, {}, {"outer": True, "inner": True}) == "a b d\n"
    assert render(template, {}, {"outer": True, "inner": False}) == "a c d\n"
    assert render(template, {}, {"outer": False, "inner": True}) == "\n"


# ---- the project written ----


def test_a_project_outside_the_checkout_refers_to_it_by_absolute_paths(tmp_path: Path) -> None:
    project = write_project(tmp_path / "work" / "My_Project", checkout=REPO)

    root = tmp_path / "work" / "My_Project"
    assert (project.root, project.name, project.package) == (root, "my-project", "my_project")
    assert not project.in_checkout and not project.example
    assert sorted(p.relative_to(root).as_posix() for p in project.files) == [
        ".env",
        ".gitignore",
        "README.md",
        "pyproject.toml",
        "src/my_project/__init__.py",
        "views/package.json",
        "views/pnpm-workspace.yaml",
        "views/src/bundle.ts",
        "views/tsconfig.json",
        "views/vite.config.ts",
    ]
    pyproject = read_toml(root / "pyproject.toml")
    assert pyproject["project"]["name"] == "my-project"
    assert pyproject["tool"]["uv"]["sources"]["fxtr"]["path"] == str(REPO / "fxtr")
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/my_project"]
    assert pyproject["tool"]["fxtr"] == {"modules": [], "views": {"package": "views"}}
    assert "pyright" not in pyproject["tool"]
    assert find_project_root(root / "src" / "my_project") == root
    views = load_views(root)
    assert views is not None and views.package == root / "views"
    assert "views" in (root / "README.md").read_text()
    assert "launch.py" not in (root / "README.md").read_text()
    assert not (root / "launch.py").exists()
    assert not (root / "src/my_project/experiment.py").exists()
    for path in project.files:
        text = path.read_text()
        assert "{{" not in text and text.endswith("\n") and not text.endswith("\n\n"), path


@pytest.mark.parametrize("published", [False, True])
def test_project_creation_needs_no_skills_docs_or_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: bool
) -> None:
    checkout = None if published else a_checkout(tmp_path / "fxtr3")
    monkeypatch.setenv("PATH", "")
    project = write_project(tmp_path / "demo", checkout=checkout)

    assert not (project.root / ".agents").exists()
    assert not (project.root / ".claude").exists()
    readme = (project.root / "README.md").read_text()
    assert "/plugin install fxtr@transluce-plugins" in readme
    assert "codex plugin add fxtr@transluce-plugins" in readme
    assert "fxtr skills install" not in readme
    assert "https://docs.transluce.ai/fxtr/reference/project-configuration" in readme


@pytest.mark.parametrize("example", [False, True])
def test_a_project_outside_the_checkout_links_its_copy_of_the_fxtr_packages(
    tmp_path: Path, example: bool
) -> None:
    project = write_project(tmp_path / "demo", checkout=REPO, example=example)

    root = project.root
    views = load_views(root)
    assert views is not None and views.package == root / "views"
    package = json.loads((root / "views" / "package.json").read_text())
    assert package["scripts"]["build"] == "vite build --configLoader runner"
    assert package["name"] == "demo-views"
    specs = package["dependencies"] | package["devDependencies"]
    vendored = root / "views" / VENDOR_DIR
    for name in ("fxtr-view", "fxtr-view-kit", "fxtr-view-bundle"):
        assert specs[name] == f"link:./vendor/fxtr/packages/{name}"
        assert (vendored / "packages" / name / "package.json").is_file()
    assert sorted(p.name for p in (vendored / "packages").iterdir()) == sorted(PROJECT_PACKAGES)
    for name, version in VENDORED_DEV_DEPENDENCIES.items():
        assert specs[name] == version
    assert (root / "views/pnpm-workspace.yaml").is_file()
    tsconfig = json.loads((root / "views" / "tsconfig.json").read_text())
    assert tsconfig["extends"] == "./vendor/fxtr/tsconfig.base.json"
    assert (vendored / "tsconfig.base.json").is_file()
    bundle = (root / "views" / "src" / "bundle.ts").read_text()
    assert ('"demo-views.GreetingPanel"' in bundle) == example
    overview = 'renderer(InvocationPanel, "greet_everyone", "demo-views.Overview"'
    assert (overview in bundle) == example
    greeting = root / "views" / "src" / "greeting.tsx"
    assert greeting.exists() == example
    assert (root / "views" / "src" / "overview.tsx").exists() == example
    assert (root / "views" / "src" / "overview.module.css").exists() == example
    if example:
        assert 'GREETING = "demo.Greeting"' in greeting.read_text()
    else:
        assert "greeting" not in bundle and "Greeting" not in bundle
    for path in project.files:
        assert "{{" not in path.read_text(), path
    ignored = (root / ".gitignore").read_text()
    assert "node_modules/" in ignored and ".fxtr/" in ignored
    assert ".env" in ignored.splitlines()
    assert load_env_file(root, env={}) == {}  # the placeholders set nothing until filled in
    assert "fxtr views build" in (root / "README.md").read_text()


@pytest.mark.parametrize("example", [False, True])
def test_a_project_inside_the_checkout_refers_to_it_by_relative_paths(
    tmp_path: Path, example: bool
) -> None:
    checkout = a_checkout(tmp_path / "fxtr3")
    project = write_project(checkout / "personal" / "alice", checkout=checkout, example=example)

    root = project.root
    assert project.in_checkout
    pyproject = read_toml(root / "pyproject.toml")
    assert pyproject["tool"]["uv"]["sources"]["fxtr"]["path"] == "../../fxtr"
    assert pyproject["tool"]["pyright"]["extends"] == "../../pyright-base.json"
    includes = pyproject["tool"]["pyright"]["include"]
    assert includes == (["src", "launch.py"] if example else ["src"])
    assert all((root / path).exists() for path in includes)
    assert (root / "../../pyright-base.json").resolve().is_file()
    package = json.loads((root / "views" / "package.json").read_text())
    assert package["scripts"]["build"] == "vite build --configLoader runner"
    specs = package["dependencies"] | package["devDependencies"]
    for name in ("fxtr-view", "fxtr-view-kit", "fxtr-view-bundle"):
        assert specs[name] == f"link:../../../ts/packages/{name}"
        assert (root / "views" / specs[name].removeprefix("link:")).resolve() == (
            checkout / "ts/packages" / name
        )
    assert (root / "views/pnpm-workspace.yaml").is_file()
    tsconfig = json.loads((root / "views" / "tsconfig.json").read_text())
    assert (root / "views" / tsconfig["extends"]).resolve() == checkout / "ts/tsconfig.base.json"
    assert "`pnpm install` in `views/`" in (root / "README.md").read_text()
    assert not (root / ".agents").exists() and not (root / ".claude").exists()
    readme = (root / "README.md").read_text()
    assert "The checkout's `docs/fxtr/reference/project-configuration.mdx`" in readme


@pytest.mark.parametrize("workspace_exists", [False, True])
def test_creating_a_project_writes_nothing_outside_it(
    tmp_path: Path, workspace_exists: bool
) -> None:
    checkout = a_checkout(tmp_path / "fxtr3")
    if not workspace_exists:
        (checkout / "pnpm-workspace.yaml").unlink()
    (checkout / "pnpm-lock.yaml").write_text("enclosing lockfile\n")
    root = checkout / "personal" / "bob"

    def enclosing_files() -> dict[Path, bytes]:
        return {
            p.relative_to(checkout): p.read_bytes()
            for p in checkout.rglob("*")
            if p.is_file() and not p.is_relative_to(root)
        }

    before = enclosing_files()
    write_project(root, checkout=checkout)
    assert enclosing_files() == before
    assert (root / "views/pnpm-workspace.yaml").exists()


@pytest.mark.skipif(shutil.which("pnpm") is None, reason="pnpm is not installed")
@pytest.mark.parametrize("in_checkout", [False, True])
async def test_pnpm_uses_the_renderers_own_workspace(tmp_path: Path, in_checkout: bool) -> None:
    checkout = a_checkout(tmp_path / "fxtr3")
    # Even a project outside fxtr can be nested in somebody else's workspace.
    (tmp_path / "pnpm-workspace.yaml").write_text('packages: ["**"]\n')
    root = checkout / "personal/demo" if in_checkout else tmp_path / "demo"
    write_project(root, checkout=checkout)
    views = root / "views"
    result = await anyio.run_process(
        [
            "pnpm",
            "--config.manage-package-manager-versions=false",
            "--dir",
            str(views),
            "root",
            "--workspace-root",
        ]
    )
    assert Path(result.stdout.decode().strip()).resolve() == views / "node_modules"


async def test_an_empty_project_can_initialize_its_database(
    tmp_path: Path, database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "empty_project", checkout=REPO)
    schema = database.pg_schema
    assert schema is not None
    status, out, err = await fxtr(
        capsys,
        "init",
        "--project",
        str(project.root),
        "--database-url",
        database.url,
        "--schema",
        schema,
    )

    assert status == cli.EXIT_OK, err
    assert "the project's database is ready" in out
    assert read_toml(project.root / "fxtr.local.toml") == {
        "database_url": database.url,
        "schema": schema,
    }


@pytest.mark.timeout_seconds(30)
@pytest.mark.parametrize("example", [False, True])
async def test_generated_python_passes_pyright(tmp_path: Path, example: bool) -> None:
    project = write_project(tmp_path / "greeter", checkout=REPO, example=example)
    config = project.root / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "extends": str(REPO / "pyright-base.json"),
                "include": ["src", "launch.py"] if example else ["src"],
            }
        )
    )
    completed = await anyio.run_process(
        [
            sys.executable,
            "-m",
            "pyright",
            "--project",
            str(config),
            "--pythonpath",
            sys.executable,
        ],
        cwd=project.root,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout + completed.stderr).decode()


async def test_the_generated_launcher_runs_on_postgres(tmp_path: Path, database: Database) -> None:
    project = write_project(tmp_path / "greeter", checkout=REPO, example=True)
    env = os.environ | {
        "PYTHONPATH": os.pathsep.join(
            [
                str(project.root / "src"),
                str(REPO / "fxtr" / "tests"),
                os.environ.get("PYTHONPATH", ""),
            ]
        )
    }
    script = """
import runpy
import sys
from postgres_testing import a_client
from fxtr.project import running
from fxtr.runner.postgres.database import Database
from greeter.experiment import greet, greet_everyone

# Pin only a fixture code version; the generated project needs no repository or venv in this test.
database = Database(sys.argv[1], pg_schema=sys.argv[2])
running.open_local_client = lambda *args, **kwargs: a_client(database, greet, greet_everyone)
runpy.run_path('launch.py', run_name='__main__')
"""
    schema = database.pg_schema
    assert schema is not None
    completed = await to_thread.run_sync(
        lambda: subprocess.run(
            [sys.executable, "-c", script, database.url, schema],
            cwd=project.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    )
    assert completed.returncode == 0, completed.stderr
    assert ": succeeded" in completed.stdout
    assert completed.stdout.splitlines()[-3:] == ["Hello, Ada!", "Hello, Alan!", "Hello, Grace!"]


# ---- refusals ----


def test_a_directory_with_anything_in_it_is_refused(tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "notes.txt").write_text("")
    (tmp_path / "a_file").write_text("")
    for target in (tmp_path / "taken", tmp_path / "a_file"):
        with pytest.raises(NewProjectError, match="not an empty directory"):
            write_project(target, checkout=REPO)
    (tmp_path / "empty").mkdir()
    assert write_project(tmp_path / "empty", checkout=REPO).name == "empty"


@pytest.mark.parametrize("example", [False, True])
def test_a_published_project_depends_on_fxtr_from_the_package_index(
    tmp_path: Path, example: bool
) -> None:
    project = write_project(tmp_path / "demo", checkout=None, example=example)

    root = project.root
    assert project.checkout is None and not project.in_checkout
    assert not (root / ".agents").exists() and not (root / ".claude").exists()
    version = importlib.metadata.version("fxtr")
    pyproject = read_toml(root / "pyproject.toml")
    assert pyproject["project"]["dependencies"] == [f"{FXTR_EXTRAS}>={version}"]
    assert "sources" not in pyproject["tool"]["uv"] and "pyright" not in pyproject["tool"]
    package = json.loads((root / "views" / "package.json").read_text())
    assert package["dependencies"]["fxtr-view"] == "link:./vendor/fxtr/packages/fxtr-view"
    assert (root / "views" / VENDOR_DIR / "packages" / "fxtr-view" / "package.json").is_file()
    readme = (root / "README.md").read_text()
    assert f"fxtr {version} or later" in readme
    assert "/plugin install fxtr@transluce-plugins" in readme
    assert "codex plugin add fxtr@transluce-plugins" in readme
    assert "docker run" in readme and "postgres_env" not in readme
    assert "checkout" not in readme
    assert "uv run fxtr views vendor" in readme
    for path in project.files:
        text = path.read_text()
        assert "{{" not in text and text.endswith("\n") and not text.endswith("\n\n"), path


def test_without_a_checkout_a_new_project_is_published(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(new_module, "_is_checkout", lambda path: False)
    assert find_checkout() is None


def test_views_need_the_fxtr_packages_in_the_checkout(tmp_path: Path) -> None:
    checkout = a_checkout(tmp_path / "fxtr3")
    shutil.rmtree(checkout / "ts" / "packages" / "fxtr-view-kit")
    with pytest.raises(NewProjectError, match="no ts/packages/fxtr-view-kit"):
        write_project(tmp_path / "demo", checkout=checkout)
    assert not (tmp_path / "demo").exists()


def test_the_checkout_is_where_fxtr_was_imported_from() -> None:
    assert find_checkout() == REPO


def test_the_renderer_package_uses_the_example_projects_versions() -> None:
    example = json.loads((REPO / "example_projects/basic_project/views/package.json").read_text())
    assert example["packageManager"] == PACKAGE_MANAGER
    for name, version in VIEWS_DEV_DEPENDENCIES.items():
        assert example["devDependencies"][name] == version, name


# ---- fxtr new ----


async def fxtr(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = await cli.main(argv)
    out, err = capsys.readouterr()
    return status, out, err


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
@pytest.mark.parametrize("example_args", [(), ("--example",)], ids=["empty", "example"])
async def test_fxtr_new_writes_the_project_and_prints_the_next_steps(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    example_args: tuple[str, ...],
) -> None:
    fresh = not in_repository(tmp_path)
    status, out, _ = await fxtr(capsys, "new", str(tmp_path / "demo"), *example_args)

    assert status == cli.EXIT_OK
    root = tmp_path / "demo"
    assert f"wrote the fxtr project demo in {root.resolve()}" in out
    assert "installed the agent skills" not in out
    assert ("initialized a git repository there" in out) == fresh
    assert (root / ".git").exists() == fresh
    assert not (root / ".venv").exists()
    assert not (root / "uv.lock").exists()
    assert load_views(root) is not None
    assert (root / "views" / "package.json").is_file()
    assert (root / "views").exists()
    example = bool(example_args)
    assert (root / "launch.py").exists() == example
    assert (root / "src/demo/experiment.py").exists() == example
    assert (root / "views/src/greeting.tsx").exists() == example
    modules = read_toml(root / "pyproject.toml")["tool"]["fxtr"]["modules"]
    assert modules == (["demo.experiment"] if example else [])
    for path in (root / "src").rglob("*.py"):
        compile(path.read_text(), str(path), "exec")
    readme = (root / "README.md").read_text()
    assert ("uv run python launch.py" in readme) == example
    assert ("This project has no experiments yet" in out) == (not example)
    assert "{{" not in readme
    next_steps = out.split("next, in", 1)[1].split("\n\n", 1)[0]
    steps = [line.strip() for line in next_steps.splitlines()[1:]]
    commands = [line for line in steps if not line.startswith("#")]
    expected_commands = [
        "uv sync",
        "uv run fxtr init --database-url postgresql+asyncpg://fxtr:fxtr@localhost:55433/fxtr",
        "(cd views && pnpm install) && uv run fxtr views build",
        "git add . && git commit -m 'A new fxtr project'",
    ]
    if example:
        expected_commands += [
            "uv run python launch.py",
            f"uv run fxtr run {WORKFLOW_NAME} --input salutation=Hello",
        ]
    assert commands == expected_commands


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
async def test_fxtr_new_inside_the_checkout_keeps_setup_local(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = a_checkout(tmp_path / "fxtr3")
    monkeypatch.setattr(new_module.fxtr, "__file__", str(checkout / "fxtr/src/fxtr/__init__.py"))
    workspace = checkout / "pnpm-workspace.yaml"
    before = workspace.read_text()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    root = checkout / "personal" / "alice"
    status, out, _ = await fxtr(capsys, "new", str(root))

    assert status == cli.EXIT_OK
    assert load_views(root) is not None
    assert "added its renderer package" not in out
    assert "initialized a git repository" not in out and not (root / ".git").exists()
    assert "installed the agent skills" not in out
    assert "git add . && git commit" in out
    assert "(cd views && pnpm install) && uv run fxtr views build" in out
    assert "../../pnpm-workspace.yaml" not in out
    assert workspace.read_text() == before


async def test_fxtr_new_refuses_an_occupied_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "pyproject.toml").write_text("")
    status, out, err = await fxtr(capsys, "new", str(tmp_path / "taken"))

    assert status == cli.EXIT_REFUSED
    assert out == ""
    assert err.startswith("fxtr: ") and "is not an empty directory" in err


# ---- end to end (opt-in) ----


def _run(*command: str, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, (
        f"{' '.join(command)} failed ({completed.returncode}):"
        f"\n{completed.stdout}\n{completed.stderr}"
    )
    return completed.stdout


def _pin_to_repository_lock(root: Path) -> None:
    """Constrain the new project at ``root`` to the third-party versions in this repository's
    ``uv.lock``, so that its sync installs what the rest of the suite tests fxtr with rather than
    whatever was released last."""
    exported = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--all-packages",
            "--all-extras",
            "--no-hashes",
            "--no-header",
            "--no-annotate",
            "--no-emit-workspace",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pins = [line for line in exported.splitlines() if line and not line.startswith("#")]
    pyproject = root / "pyproject.toml"
    text = pyproject.read_text()
    assert text.count("\n[tool.uv]\n") == 1, text
    pinned = f"constraint-dependencies = {json.dumps(pins)}\n"
    pyproject.write_text(text.replace("\n[tool.uv]\n", f"\n[tool.uv]\n{pinned}"))


# The sync (up to 300 seconds) and the launch (up to 120) take far longer than the default timeout.
@pytest.mark.timeout_seconds(480)
async def test_a_new_project_runs_as_a_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not os.environ.get(OPT_IN_ENV_VAR):
        pytest.skip(f"{OPT_IN_ENV_VAR} is not set")
    url = os.environ.get("FXTR_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("FXTR_TEST_DATABASE_URL is not set")
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("uv and git are required")
    pytest.importorskip("sqlalchemy")
    from fxtr.runner.postgres.database import Database

    if in_repository(tmp_path):
        pytest.skip("tmp_path is inside a repository, whose commit the launch would pin")
    root = tmp_path / "greeter"
    status, _, err = await fxtr(capsys, "new", str(root), "--example")
    assert status == cli.EXIT_OK, err
    _pin_to_repository_lock(root)
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "FXTR_DATABASE_URL")}
    # The suite's Python too: the pinned versions may have no wheels for a newer one.
    env["UV_PYTHON"] = f"{sys.version_info.major}.{sys.version_info.minor}"
    with anyio.fail_after(300):
        await to_thread.run_sync(lambda: _run("uv", "sync", cwd=root, env=env))

    schema = f"test_{uuid.uuid4().hex}"
    (root / "fxtr.local.toml").write_text(f'database_url = "{url}"\nschema = "{schema}"\n')
    try:
        _run("git", "add", ".", cwd=root, env=env)
        _run(
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "new",
            cwd=root,
            env=env,
        )
        command = ("uv", "run", "python", "launch.py")
        with anyio.fail_after(120):
            out = await to_thread.run_sync(lambda: _run(*command, cwd=root, env=env))
        assert ": succeeded" in out, out
        assert "Hello, Ada!" in out
        assert _run("git", "status", "--porcelain", cwd=root, env=env) == ""
    finally:
        database = Database(url, pg_schema=schema)
        try:
            await database.drop_schema()
        finally:
            await database.dispose()
