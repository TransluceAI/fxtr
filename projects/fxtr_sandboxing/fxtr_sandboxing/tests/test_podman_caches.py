"""Unit tests for the per-process podman micro-caches in ``_podman`` (no
podman needed): ``podman_is_remote`` and the sidecar host key are each fetched
once and memoized, since both are properties that can't change during a run."""

from __future__ import annotations

import pytest
from fxtr_sandboxing import _podman


async def test_podman_is_remote_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def fake_run_checked(cmd, *, what, input=None):
        calls.append(list(cmd))
        return "true\n"

    monkeypatch.setattr(_podman, "_run_checked", fake_run_checked)
    monkeypatch.setattr(_podman, "_is_remote_cache", {})  # isolate from other tests

    assert await _podman.podman_is_remote("podman-xyz") is True
    assert await _podman.podman_is_remote("podman-xyz") is True
    assert len(calls) == 1  # second call served from cache, no `podman info`


async def test_sidecar_host_key_is_cached_per_image(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def fake_run_checked(cmd, *, what, input=None):
        calls.append(list(cmd))
        return "ssh-ed25519 AAAAKEY\n"

    monkeypatch.setattr(_podman, "_run_checked", fake_run_checked)
    monkeypatch.setattr(_podman, "_host_key_cache", {})

    k1 = await _podman._sidecar_host_key("podman", "localhost/img:abc", "sc-1")
    k2 = await _podman._sidecar_host_key("podman", "localhost/img:abc", "sc-2")
    assert k1 == k2 == "ssh-ed25519 AAAAKEY"
    assert len(calls) == 1  # same image ref → one exec, reused for the 2nd sidecar
    # A different image ref is a separate key → a separate fetch.
    await _podman._sidecar_host_key("podman", "localhost/img:def", "sc-3")
    assert len(calls) == 2
