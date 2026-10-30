"""Pure, backend-neutral helpers in ``_backend.py``."""

from __future__ import annotations

import pytest
from fxtr_nix_runner._backend import impure_env_nix_flags


def test_impure_env_flags_empty_is_noop():
    assert impure_env_nix_flags(None) == []
    assert impure_env_nix_flags({}) == []


def test_impure_env_flags_builds_option():
    flags = impure_env_nix_flags(
        {
            "FXTR_REMOTE_BASE_URL": "http://127.0.0.1:8080",
            "FXTR_REMOTE_BEARER_TOKEN": "deadbeef",
        }
    )
    assert flags == [
        "--extra-experimental-features",
        "configurable-impure-env",
        "--option",
        "impure-env",
        "FXTR_REMOTE_BASE_URL=http://127.0.0.1:8080 FXTR_REMOTE_BEARER_TOKEN=deadbeef",
    ]


def test_impure_env_flags_rejects_spaces():
    # The impure-env setting is a space-separated K=V list; a value with a space
    # would corrupt the parse, so it's rejected rather than silently mangled.
    with pytest.raises(ValueError):
        impure_env_nix_flags({"K": "has space"})
    with pytest.raises(ValueError):
        impure_env_nix_flags({"has space": "v"})
