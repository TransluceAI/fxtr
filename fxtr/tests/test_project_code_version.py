"""Tests for the code version: where each registered function is implemented, inside the pinned
repository or outside it, and the entity's round trip through a store."""

from __future__ import annotations

import dataclasses
import importlib.metadata
import inspect
import linecache
from pathlib import Path
from typing import Any, cast

import cattrs.gen
import pytest

from fxtr.experiment.registry import Function, FunctionRegistry
from fxtr.experiment.steps import StepContext, step
from fxtr.project.code_version import CodeVersion, ModuleSource, RepoSource, function_sources
from fxtr.storage.memory_storage import MemoryStorage

REPO_ROOT = Path(__file__).resolve().parents[2]


@step(inputs={"x": int}, output=int)
async def double(context: StepContext, x: int) -> int:
    return 2 * x


@dataclasses.dataclass(frozen=True)
class _Registered:
    """A stand-in for a step whose body is defined somewhere a test cannot define one."""

    name: str
    body: Any


def registered(name: str, body: Any) -> Function:
    return cast("Function", _Registered(name, body))


def without_source() -> Any:
    """A function whose source cannot be read: defined by ``exec``, as ``python -c`` or generated
    code would, so its file is ``<string>``."""
    namespace: dict[str, Any] = {"__name__": "generated"}
    exec("async def made(context, x):\n    return x\n", namespace)
    return namespace["made"]


def lines_of(body: Any) -> tuple[int, int]:
    lines, start = inspect.getsourcelines(body)
    return start, start + len(lines) - 1


def test_a_function_in_the_repository_is_located_by_path() -> None:
    sources = function_sources(FunctionRegistry([double]), REPO_ROOT)
    start, end = lines_of(double.body)
    assert sources == {
        double.name: RepoSource(
            file="fxtr/tests/test_project_code_version.py",
            symbol="double",
            start_line=start,
            end_line=end,
        )
    }


def test_a_function_outside_the_repository_is_located_by_module(tmp_path: Path) -> None:
    # Pinned elsewhere, this file is outside the repository: its module names it.
    (source,) = function_sources(FunctionRegistry([double]), tmp_path).values()
    start, end = lines_of(double.body)
    assert source == ModuleSource(
        module=__name__,
        symbol="double",
        start_line=start,
        end_line=end,
        distribution=None,
        version=None,
    )


def test_an_installed_package_names_its_distribution() -> None:
    # The venv may live inside the checkout (this repository's does); it is still a package.
    body = cattrs.gen.make_dict_structure_fn
    sources = function_sources(FunctionRegistry([registered("structure", body)]), REPO_ROOT)
    start, end = lines_of(body)
    assert sources["structure"] == ModuleSource(
        module="cattrs.gen",
        symbol="make_dict_structure_fn",
        start_line=start,
        end_line=end,
        distribution="cattrs",
        version=importlib.metadata.version("cattrs"),
    )


def test_a_function_without_source_has_no_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    # Python's line cache holds lines for "<string>" whenever something else exec'd code: they
    # are not this function's, and "<string>" is no file in the repository.
    monkeypatch.setitem(linecache.cache, "<string>", (1, None, ["x = 1\n"], "<string>"))
    sources = function_sources(FunctionRegistry([registered("made", without_source())]), REPO_ROOT)
    assert sources["made"] == ModuleSource(
        module="generated",
        symbol="made",
        start_line=None,
        end_line=None,
        distribution=None,
        version=None,
    )


def test_an_ambiguous_name_is_left_out() -> None:
    registry = FunctionRegistry([double, registered(double.name, without_source())])
    assert function_sources(registry, REPO_ROOT) == {}


@pytest.mark.anyio
async def test_a_code_version_round_trips_through_a_store() -> None:
    sources = function_sources(
        FunctionRegistry([double, registered("made", without_source())]), REPO_ROOT
    )
    code_version = CodeVersion(
        commit="a" * 40,
        tree="b" * 40,
        ephemeral=False,
        project_path="example_projects/basic_project",
        environment_verified=True,
        functions=sources,
    )
    store = MemoryStorage()
    assert await store.load(await store.store(code_version)) == code_version
