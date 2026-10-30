"""Unit tests for ``resolve_report_cli_command`` — the arg → env → dev order.

Pure (no TS bundle / nix needed): cases 1/2 return the executable as-is (the
nix wrapper bakes in node); the dev fallback prepends ``node`` to the in-tree
bundle.
"""

from __future__ import annotations

import pathlib

import pytest

from fxtr_report_builder._ts_dist_path import (
    CLI_ENV_VAR,
    MdxSubprocessError,
    resolve_report_cli_command,
)


def test_explicit_arg_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLI_ENV_VAR, "/env/fxtr-report-builder")
    assert resolve_report_cli_command("/explicit/fxtr-report-builder") == [
        "/explicit/fxtr-report-builder"
    ]
    # A Path arg is stringified (still a single self-contained executable).
    assert resolve_report_cli_command(pathlib.Path("/explicit/cli")) == ["/explicit/cli"]


def test_env_var_used_when_no_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLI_ENV_VAR, "/env/fxtr-report-builder")
    assert resolve_report_cli_command() == ["/env/fxtr-report-builder"]


def test_dev_fallback_prepends_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)
    monkeypatch.setattr(
        "fxtr_report_builder._ts_dist_path.resolve_ts_dist_path",
        lambda: pathlib.Path("/dev/dist/main.cjs"),
    )
    assert resolve_report_cli_command() == ["node", "/dev/dist/main.cjs"]


def test_empty_env_falls_through_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLI_ENV_VAR, "")
    monkeypatch.setattr(
        "fxtr_report_builder._ts_dist_path.resolve_ts_dist_path",
        lambda: pathlib.Path("/dev/dist/main.cjs"),
    )
    assert resolve_report_cli_command() == ["node", "/dev/dist/main.cjs"]


def test_dev_fallback_raises_when_bundle_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)

    def _missing() -> pathlib.Path:
        raise MdxSubprocessError("bundle missing")

    monkeypatch.setattr("fxtr_report_builder._ts_dist_path.resolve_ts_dist_path", _missing)
    with pytest.raises(MdxSubprocessError):
        resolve_report_cli_command()
