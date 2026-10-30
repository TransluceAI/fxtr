"""Unit tests for the pure relock-target decision (`plan_relock`, no nix)."""

from __future__ import annotations

import pytest
from fxtr_nix_runner.update_hashes import Mode, plan_relock


def test_empty_lock_relocks_all_preloads_as_new() -> None:
    plan = plan_relock(Mode.DEFAULT, set(), {"a", "b"}, set())
    assert plan.relock == ["a", "b"]
    assert plan.reasons == {"a": "new", "b": "new"}
    assert plan.drop == []


def test_default_relocks_only_new_leaving_existing() -> None:
    # 'a' is already locked -> untouched; 'b' is new.
    plan = plan_relock(Mode.DEFAULT, set(), {"a", "b"}, {"a"})
    assert plan.relock == ["b"]
    assert plan.reasons == {"b": "new"}
    assert plan.unchanged == ["a"]


def test_default_does_not_relock_existing_slots() -> None:
    # An already-locked slot is never auto-relocked, even if its inputs changed
    # in place — that's `--all` / `-k` / `nix build --check` territory.
    plan = plan_relock(Mode.DEFAULT, set(), {"a"}, {"a"})
    assert plan.relock == []
    assert plan.unchanged == ["a"]


def test_default_drops_stale_lock_keys() -> None:
    plan = plan_relock(Mode.DEFAULT, set(), {"a"}, {"a", "gone"})
    assert plan.drop == ["gone"]
    assert plan.relock == []


def test_all_mode_relocks_every_preload() -> None:
    plan = plan_relock(Mode.ALL, set(), {"a", "b"}, {"a"})
    assert plan.relock == ["a", "b"]
    assert set(plan.reasons.values()) == {"--all"}


def test_keys_mode_relocks_requested_subset() -> None:
    plan = plan_relock(Mode.KEYS, {"a"}, {"a", "b"}, {"a", "b"})
    assert plan.relock == ["a"]
    assert plan.reasons == {"a": "requested"}
    assert "b" in plan.unchanged


def test_keys_mode_rejects_undeclared_key() -> None:
    with pytest.raises(ValueError, match="not declared"):
        plan_relock(Mode.KEYS, {"nope"}, {"a"}, {"a"})
