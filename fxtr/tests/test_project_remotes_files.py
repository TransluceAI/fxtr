"""Tests for ``fxtr remote``: the edits it makes to ``pyproject.toml`` and
``fxtr.local.toml``, through the command line."""

from __future__ import annotations

import os
import pty
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from fxtr.project import cli
from fxtr.project.config import load_remotes
from fxtr.project.new import write_project
from fxtr.project.remotes import hub_login
from fxtr.project.remotes.hub import HttpHubClient, HubRequestError, hub_token

pytestmark = pytest.mark.anyio

PYPROJECT = """\
# The lab's experiments.
[project]
name = "lab"  # the project's name

[tool.fxtr]
modules = ["lab.steps"]

[tool.uv]
package = false
"""
PROJECT_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    return tmp_path


async def fxtr(root: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    capsys.readouterr()
    status = await cli.main(["remote", *args, "--project", str(root)])
    out, err = capsys.readouterr()
    return status, out, err


async def test_a_committed_remote_is_written_to_pyproject_keeping_the_rest(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first remote becomes the default; the file's comments and other tables stay."""
    status, out, _ = await fxtr(
        root, capsys, "add", "team", "https://hub.test", "--project-id", PROJECT_ID
    )
    assert status == cli.EXIT_OK
    assert out.splitlines() == [
        "Added the remote team to pyproject.toml, as the default remote.",
        "",
        "Done: this project is linked to the remote team.",
        "Next: commit pyproject.toml, so every checkout has the remote."
        " fxtr run needs it committed too.",
    ]
    assert (root / "pyproject.toml").read_text() == PYPROJECT.replace(
        '\nmodules = ["lab.steps"]\n',
        '\nmodules = ["lab.steps"]\ndefault_remote = "team"\n\n'
        '[tool.fxtr.remotes.team]\nkind = "hub"\nurl = "https://hub.test"\n'
        f'project = "{PROJECT_ID}"\n',
    )
    remotes = load_remotes(root)
    assert remotes.default == "team"
    assert dict(remotes.by_name["team"].settings) == {
        "url": "https://hub.test",
        "project": PROJECT_ID,
    }

    # A second remote is added beside it; the default stays.
    status, out, _ = await fxtr(
        root, capsys, "add", "registry", "ghcr.io/lab/jobs", "--kind", "oci", "--git-remote", "up"
    )
    assert status == cli.EXIT_OK and "default" not in out
    remotes = load_remotes(root)
    assert list(remotes.by_name) == ["team", "registry"]
    assert remotes.default == "team"
    assert dict(remotes.by_name["registry"].settings) == {
        "repository": "ghcr.io/lab/jobs",
        "git_remote": "up",
    }
    _, out, _ = await fxtr(root, capsys, "list")
    assert out.splitlines() == [
        f"* team  hub  url=https://hub.test project={PROJECT_ID}  (pyproject.toml)",
        "  registry  oci  repository=ghcr.io/lab/jobs git_remote=up  (pyproject.toml)",
    ]


async def test_a_local_remote_and_default_are_this_machines_own(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await fxtr(root, capsys, "add", "team", "https://hub.test", "--project-id", PROJECT_ID)
    (root / "fxtr.local.toml").write_text('database_url = "postgresql://localhost/db"\n')
    status, _, _ = await fxtr(
        root, capsys, "add", "mine", "http://localhost:8000", "--project-id", OTHER_ID, "--local"
    )
    assert status == cli.EXIT_OK
    local = (root / "fxtr.local.toml").read_text()
    assert local.startswith('database_url = "postgresql://localhost/db"\n')
    assert "[remotes.mine]" in local
    assert "mine" not in (root / "pyproject.toml").read_text()

    # A committed default must be a remote every checkout has.
    status, _, err = await fxtr(root, capsys, "set-default", "mine")
    assert status == cli.EXIT_REFUSED and "--local" in err
    status, out, _ = await fxtr(root, capsys, "set-default", "mine", "--local")
    assert (status, out) == (cli.EXIT_OK, "mine is the default remote on this machine\n")
    assert load_remotes(root).default == "mine"
    assert 'default_remote = "team"' in (root / "pyproject.toml").read_text()


async def test_a_local_remote_writes_the_local_file_when_there_is_none(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, _, _ = await fxtr(
        root, capsys, "add", "mine", "http://localhost:8000", "--project-id", OTHER_ID, "--local"
    )
    assert status == cli.EXIT_OK
    assert load_remotes(root).by_name["mine"].local
    assert load_remotes(root).default == "mine"


async def test_rename_and_remove_follow_the_remote_and_the_default(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await fxtr(root, capsys, "add", "team", "https://hub.test", "--project-id", PROJECT_ID)
    await fxtr(root, capsys, "add", "other", "https://hub.test", "--project-id", OTHER_ID)
    await fxtr(root, capsys, "add", "last", "ghcr.io/lab/jobs", "--kind", "oci")
    status, out, _ = await fxtr(root, capsys, "rename", "team", "lab")
    assert (status, out) == (cli.EXIT_OK, "renamed remote team to lab\n")
    remotes = load_remotes(root)
    assert list(remotes.by_name) == ["lab", "other", "last"]
    assert remotes.default == "lab"
    assert dict(remotes.by_name["lab"].settings)["project"] == PROJECT_ID

    status, out, _ = await fxtr(root, capsys, "remove", "lab")
    assert (status, out) == (cli.EXIT_OK, "removed remote lab; the jobs it holds stay there\n")
    remotes = load_remotes(root)
    assert list(remotes.by_name) == ["other", "last"]
    assert remotes.default is None


async def test_renaming_or_removing_a_local_remote_leaves_the_environment_alone(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    refreshed: list[Path] = []

    async def refresh(project_root: Path) -> None:
        refreshed.append(project_root)

    monkeypatch.setattr(cli, "_refresh_environment", refresh)
    await fxtr(root, capsys, "add", "mine", "ghcr.io/lab/jobs", "--kind", "oci", "--local")
    await fxtr(root, capsys, "rename", "mine", "ours")
    await fxtr(root, capsys, "remove", "ours")
    assert refreshed == []
    await fxtr(root, capsys, "add", "team", "ghcr.io/lab/jobs", "--kind", "oci")
    await fxtr(root, capsys, "rename", "team", "lab")
    await fxtr(root, capsys, "remove", "lab")
    assert refreshed == [root.resolve()] * 3 or refreshed == [root] * 3


@pytest.mark.parametrize(
    "args,message",
    [
        (("add", "team", "https://hub.test"), "--project-id"),
        (("add", "team", "https://hub.test", "--project-id", PROJECT_ID, "--another"), "--create"),
        (("add", "team", "https://hub.test", "--project-id", "nope"), "not a hub project id"),
        (("add", "a/b", "https://hub.test", "--create"), "cannot name a remote"),
        (("add", "a.b", "https://hub.test", "--create"), "cannot name a remote"),
        (("add", "team", "http://hub.test", "--create"), "only over HTTPS"),
        (("add", "team", "https://hub.test/path", "--create"), "must be an origin"),
        (("add", "team", "--kind", "oci"), "registry repository"),
        (("add", "team", "ghcr.io/x", "--kind", "oci", "--create"), "use --kind hub"),
        (("add", "team", "--project-name", "Lab", "--project-id", PROJECT_ID), "--create"),
        (("rename", "nothing", "else"), "no remote named 'nothing'"),
        (("remove", "nothing"), "no remote named 'nothing'"),
        (("set-default", "nothing"), "no remote named 'nothing'"),
    ],
)
async def test_what_a_remote_command_refuses_leaves_the_files_as_they_were(
    root: Path, capsys: pytest.CaptureFixture[str], args: tuple[str, ...], message: str
) -> None:
    status, _, err = await fxtr(root, capsys, *args)
    assert status == cli.EXIT_REFUSED and message in err, err
    assert (root / "pyproject.toml").read_text() == PYPROJECT
    assert not (root / "fxtr.local.toml").exists()


async def test_a_name_is_one_remote_across_both_files(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await fxtr(root, capsys, "add", "team", "https://hub.test", "--project-id", PROJECT_ID)
    await fxtr(root, capsys, "add", "mine", "ghcr.io/me/jobs", "--kind", "oci", "--local")
    for args in (
        ("add", "team", "ghcr.io/x", "--kind", "oci", "--local"),
        ("add", "mine", "ghcr.io/x", "--kind", "oci"),
        ("rename", "team", "mine"),
    ):
        status, _, err = await fxtr(root, capsys, *args)
        assert status == cli.EXIT_REFUSED and "exists already" in err, err


async def test_list_says_when_there_are_no_remotes(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, err = await fxtr(root, capsys, "list")
    assert (status, out) == (cli.EXIT_OK, "")
    assert "fxtr remote add" in err


async def test_a_remote_is_added_to_the_pyproject_fxtr_new_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``fxtr new`` puts ``[build-system]`` between ``[tool.uv]`` and ``[tool.fxtr]``, so the
    ``[tool]`` table is in pieces; the remote goes into ``[tool.fxtr]`` all the same, and every
    command edits it there."""
    root = tmp_path / "lab"
    write_project(root, checkout=None)
    status, _, err = await fxtr(
        root, capsys, "add", "team", "https://hub.test", "--project-id", PROJECT_ID
    )
    assert status == cli.EXIT_OK, err
    remotes = load_remotes(root)
    assert remotes.default == "team" and remotes.by_name["team"].settings["project"] == PROJECT_ID
    for args in (("rename", "team", "lab"), ("set-default", "lab"), ("remove", "lab")):
        status, _, err = await fxtr(root, capsys, *args)
        assert status == cli.EXIT_OK, err
    assert load_remotes(root).by_name == {}


INSTALL_URL = "https://github.com/apps/fxtr/installations/new"
INSTALL_LINK = f"{INSTALL_URL}?state=fxtr-cli"
NOT_INSTALLED = HubRequestError(
    403, "repository_forbidden", "not installed", {"install_url": INSTALL_URL}
)


class Terminal:
    """Standard input as a person's terminal, which ``remote add --create`` may wait for."""

    def isatty(self) -> bool:
        return True


class CreateAnswers:
    """What ``HttpHubClient.create_project`` answers on each call, and the tokens it was given."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *answers: Exception | str) -> None:
        self.answers = list(answers)
        self.tokens: list[str | None] = []
        self.another: list[bool] = []
        answering = self

        async def create_project(
            self: HttpHubClient,
            name: str,
            repository: str,
            project_path: str,
            *,
            another: bool = False,
        ) -> str:
            answering.tokens.append(self.token)
            answering.another.append(another)
            answer = answering.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(HttpHubClient, "create_project", create_project)


@pytest.fixture
def checkout(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project in a checkout of a GitHub repository, on a computer not connected to a hub."""
    for args in (("init", "-q"), ("remote", "add", "origin", "git@github.com:acme/lab.git")):
        subprocess.run(["git", *args], cwd=root, check=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("FXTR_HUB_TOKEN", raising=False)
    monkeypatch.setattr(hub_login, "INSTALL_POLL_SECONDS", 0)
    return root


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    links: list[str] = []
    monkeypatch.setattr(hub_login.webbrowser, "open", links.append)
    return links


async def test_create_in_a_terminal_connects_the_computer_and_waits_for_the_app(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    opened: list[str],
) -> None:
    """One command sets up a repository: the browser steps it needs happen on the way."""
    monkeypatch.setattr("sys.stdin", Terminal())

    async def browser_login(url: str, *, open_browser: bool = True) -> str:
        return "hub_cli_new"

    monkeypatch.setattr(hub_login, "browser_login", browser_login)
    created = CreateAnswers(monkeypatch, NOT_INSTALLED, NOT_INSTALLED, PROJECT_ID)

    status, out, err = await fxtr(checkout, capsys, "add", "hub", "https://hub.test", "--create")

    assert status == cli.EXIT_OK, err
    assert created.tokens == ["hub_cli_new"] * 3
    assert hub_token("https://hub.test") == "hub_cli_new"
    assert opened == [INSTALL_LINK]
    assert f"Open {INSTALL_LINK}" in out
    assert "The GitHub App is installed on acme/lab." in out
    assert "Done: this project is linked to the remote hub." in out
    assert load_remotes(checkout).by_name["hub"].settings["project"] == PROJECT_ID
    assert not (checkout / "fxtr.local.toml").exists()


EXISTS = HubRequestError(
    409,
    "project_exists",
    "This directory of the repository already has a hub project",
    {"projects": [{"id": OTHER_ID, "name": "Lab"}]},
)


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bytes], None]]:
    """Makes standard input a real terminal with these lines typed into it."""
    typist, screen = pty.openpty()
    monkeypatch.setattr("sys.stdin", os.fdopen(screen))
    yield lambda keys: (os.write(typist, keys), None)[1]
    sys.stdin.close()
    os.close(typist)


async def test_create_offers_the_project_this_directory_has_already(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    answers: Callable[[bytes], None],
) -> None:
    monkeypatch.setenv("FXTR_HUB_TOKEN", "t")
    created = CreateAnswers(monkeypatch, EXISTS)
    answers(b"\n")

    status, out, err = await fxtr(checkout, capsys, "add", "hub", "https://hub.test", "--create")

    assert status == cli.EXIT_OK, err
    assert (
        f"acme/lab already has a hub project for this directory:\n  1  Lab  https://hub.test/projects/{OTHER_ID}"
        in err
    )
    assert f"Using the existing hub project: https://hub.test/projects/{OTHER_ID}" in out
    assert created.another == [False]
    assert load_remotes(checkout).by_name["hub"].settings["project"] == OTHER_ID


async def test_create_makes_another_project_when_asked_to_in_the_terminal(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    answers: Callable[[bytes], None],
) -> None:
    monkeypatch.setenv("FXTR_HUB_TOKEN", "t")
    created = CreateAnswers(monkeypatch, EXISTS, PROJECT_ID)
    answers(b"9\nanother\n")

    status, _, err = await fxtr(checkout, capsys, "add", "hub", "https://hub.test", "--create")

    assert status == cli.EXIT_OK, err
    assert "Choose a number from 1 to 1, or `another`." in err
    assert created.another == [False, True]
    assert load_remotes(checkout).by_name["hub"].settings["project"] == PROJECT_ID


async def test_a_script_is_told_of_the_existing_project_and_makes_none(
    checkout: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FXTR_HUB_TOKEN", "t")
    created = CreateAnswers(monkeypatch, EXISTS)

    status, _, err = await fxtr(checkout, capsys, "add", "hub", "https://hub.test", "--create")

    assert status == cli.EXIT_REFUSED
    assert OTHER_ID in err and "--project-id ID" in err and "--another" in err
    assert created.another == [False]
    assert load_remotes(checkout).by_name == {}


async def test_another_makes_the_project_without_asking(
    checkout: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FXTR_HUB_TOKEN", "t")
    created = CreateAnswers(monkeypatch, PROJECT_ID)

    status, _, err = await fxtr(
        checkout, capsys, "add", "hub", "https://hub.test", "--create", "--another"
    )

    assert status == cli.EXIT_OK, err
    assert created.another == [True]


async def test_create_with_no_browser_prints_the_link_without_opening_it(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    opened: list[str],
) -> None:
    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setenv("FXTR_HUB_TOKEN", "tok")
    CreateAnswers(monkeypatch, NOT_INSTALLED, PROJECT_ID)

    status, out, err = await fxtr(
        checkout, capsys, "add", "hub", "https://hub.test", "--create", "--no-browser"
    )

    assert status == cli.EXIT_OK, err
    assert opened == []
    assert f"Open {INSTALL_LINK}" in out


async def test_create_outside_a_terminal_stops_at_the_hubs_refusal(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    opened: list[str],
) -> None:
    """A script has nobody to use a browser, and must not sit waiting for someone."""
    monkeypatch.setenv("FXTR_HUB_TOKEN", "tok")
    created = CreateAnswers(monkeypatch, NOT_INSTALLED)

    status, _, err = await fxtr(checkout, capsys, "add", "hub", "https://hub.test", "--create")

    assert status != cli.EXIT_OK and "not installed" in err
    assert (created.tokens, opened) == (["tok"], [])
    assert "hub" not in load_remotes(checkout).by_name


async def test_waiting_for_the_app_ends_on_another_refusal_and_at_its_deadline(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    opened: list[str],
) -> None:
    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setenv("FXTR_HUB_TOKEN", "tok")
    refused = HubRequestError(403, "repository_forbidden", "you cannot read this repository")
    created = CreateAnswers(monkeypatch, NOT_INSTALLED, refused)
    status, _, err = await fxtr(checkout, capsys, "add", "hub", "https://hub.test", "--create")
    assert status != cli.EXIT_OK and "you cannot read this repository" in err
    assert created.tokens == ["tok", "tok"]

    monkeypatch.setattr(hub_login, "INSTALL_WAIT_SECONDS", 0)
    CreateAnswers(monkeypatch, NOT_INSTALLED)
    status, _, err = await fxtr(checkout, capsys, "add", "hub", "https://hub.test", "--create")
    assert status != cli.EXIT_OK and "run this command again" in err
    assert INSTALL_URL in err
    assert "hub" not in load_remotes(checkout).by_name


async def test_waiting_for_the_app_outlasts_a_hub_that_does_not_answer(
    checkout: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    opened: list[str],
) -> None:
    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setenv("FXTR_HUB_TOKEN", "tok")
    down = HubRequestError(503, "unavailable", "the hub is restarting")
    CreateAnswers(monkeypatch, NOT_INSTALLED, down, down, PROJECT_ID)

    status, out, err = await fxtr(checkout, capsys, "add", "hub", "https://hub.test", "--create")

    assert status == cli.EXIT_OK, err
    assert out.count("Still waiting") == 1
