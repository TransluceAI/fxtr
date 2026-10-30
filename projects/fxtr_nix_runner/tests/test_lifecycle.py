"""``RefcountedResourceRegistry`` — sharing, refcounted teardown, failures."""

from __future__ import annotations

import anyio
import pytest
from fxtr_nix_runner import RefcountedResourceRegistry


def _counting_registry() -> tuple[RefcountedResourceRegistry[str, str], dict[str, list[str]]]:
    log: dict[str, list[str]] = {"created": [], "torn_down": []}

    async def factory(key: str) -> str:
        log["created"].append(key)
        return f"resource:{key}"

    async def teardown(resource: str) -> None:
        log["torn_down"].append(resource)

    return RefcountedResourceRegistry(factory, teardown), log


@pytest.mark.anyio
async def test_single_borrow_creates_and_tears_down():
    reg, log = _counting_registry()
    async with reg.borrow("a") as r:
        assert r == "resource:a"
        assert log["created"] == ["a"]
        assert log["torn_down"] == []
    assert log["torn_down"] == ["resource:a"]
    assert reg.active_keys() == []


@pytest.mark.anyio
async def test_nested_borrows_share_one_resource():
    reg, log = _counting_registry()
    async with reg.borrow("a") as r1:
        async with reg.borrow("a") as r2:
            assert r1 is r2 or r1 == r2
            assert log["created"] == ["a"]  # created once
        # Inner exit must NOT tear down — still one borrower.
        assert log["torn_down"] == []
    assert log["torn_down"] == ["resource:a"]  # last-out tears down


@pytest.mark.anyio
async def test_concurrent_borrows_share_and_create_once():
    reg, log = _counting_registry()

    async def use(hold: float) -> None:
        async with reg.borrow("k"):
            await anyio.sleep(hold)

    async with anyio.create_task_group() as tg:
        tg.start_soon(use, 0.05)
        tg.start_soon(use, 0.05)
        tg.start_soon(use, 0.05)

    assert log["created"] == ["k"]  # exactly one creation despite 3 borrowers
    assert log["torn_down"] == ["resource:k"]  # exactly one teardown
    assert reg.active_keys() == []


@pytest.mark.anyio
async def test_distinct_keys_are_independent():
    reg, log = _counting_registry()
    async with reg.borrow("a"):
        async with reg.borrow("b"):
            assert set(reg.active_keys()) == {"a", "b"}
            assert sorted(log["created"]) == ["a", "b"]
    assert sorted(log["torn_down"]) == ["resource:a", "resource:b"]


@pytest.mark.anyio
async def test_reborrow_after_teardown_recreates():
    reg, log = _counting_registry()
    async with reg.borrow("a"):
        pass
    async with reg.borrow("a"):
        pass
    assert log["created"] == ["a", "a"]  # created fresh each time
    assert log["torn_down"] == ["resource:a", "resource:a"]


@pytest.mark.anyio
async def test_teardown_runs_to_completion_when_borrow_cancelled():
    """A cancelled borrow must still tear its resource down (shielded), not
    orphan it — this is the `podman rm`-on-cancellation guarantee. The teardown
    here awaits a checkpoint that would be cancelled without the shield."""
    torn: list[str] = []

    async def factory(key: str) -> str:
        return f"resource:{key}"

    async def teardown(resource: str) -> None:
        await anyio.sleep(0.01)  # a real teardown awaits (e.g. `podman rm`)
        torn.append(resource)

    reg: RefcountedResourceRegistry[str, str] = RefcountedResourceRegistry(factory, teardown)

    async def hold() -> None:
        async with reg.borrow("a"):
            await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await anyio.sleep(0.05)  # let `hold` acquire the borrow
        tg.cancel_scope.cancel()

    assert torn == ["resource:a"]  # torn down despite the cancellation
    assert reg.active_keys() == []


@pytest.mark.anyio
async def test_factory_failure_propagates_and_recovers():
    attempts = {"n": 0}

    async def factory(key: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("cold start failed")
        return f"resource:{key}"

    async def teardown(resource: str) -> None:
        pass

    reg: RefcountedResourceRegistry[str, str] = RefcountedResourceRegistry(factory, teardown)

    with pytest.raises(RuntimeError, match="cold start failed"):
        async with reg.borrow("a"):
            pass
    # Failed entry was removed; a retry succeeds with a fresh factory call.
    assert reg.active_keys() == []
    async with reg.borrow("a") as r:
        assert r == "resource:a"
    assert attempts["n"] == 2


@pytest.mark.anyio
async def test_pin_alone_never_instantiates():
    reg, log = _counting_registry()
    async with reg.pin("a"):
        # The pin reserves the key but must not run the factory.
        assert log["created"] == []
        assert reg.active_keys() == ["a"]  # entry exists (reserved), no resource
    # Releasing an unborrowed pin drops the entry with no teardown.
    assert log["created"] == []
    assert log["torn_down"] == []
    assert reg.active_keys() == []


@pytest.mark.anyio
async def test_pin_keeps_resource_warm_across_borrow_cycles():
    reg, log = _counting_registry()
    async with reg.pin("a"):
        async with reg.borrow("a") as r:
            assert r == "resource:a"
            assert log["created"] == ["a"]
        # Borrow count is back to 0, but the pin keeps it alive — no teardown.
        assert log["torn_down"] == []
        # A second borrow reuses the same resource — factory not re-run.
        async with reg.borrow("a"):
            pass
        assert log["created"] == ["a"]  # still one build
        assert log["torn_down"] == []
    # Pin released with no borrows → teardown now (exactly once).
    assert log["torn_down"] == ["resource:a"]
    assert reg.active_keys() == []


@pytest.mark.anyio
async def test_borrow_outliving_pin_still_tears_down():
    reg, log = _counting_registry()
    async with reg.borrow("a"):
        async with reg.pin("a"):
            pass  # pin released while a borrow is still active
        assert log["torn_down"] == []  # borrow still holds it
    assert log["torn_down"] == ["resource:a"]  # last borrow out → teardown


@pytest.mark.anyio
async def test_factory_failure_under_pin_retries_on_next_borrow():
    attempts = {"n": 0}
    torn: list[str] = []

    async def factory(key: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("cold start failed")
        return f"resource:{key}"

    async def teardown(resource: str) -> None:
        torn.append(resource)

    reg: RefcountedResourceRegistry[str, str] = RefcountedResourceRegistry(factory, teardown)

    async with reg.pin("a"):
        with pytest.raises(RuntimeError, match="cold start failed"):
            async with reg.borrow("a"):
                pass
        # The pin survives the failed build; the next borrow retries the factory.
        async with reg.borrow("a") as r:
            assert r == "resource:a"
        assert attempts["n"] == 2
    assert torn == ["resource:a"]
    assert reg.active_keys() == []


def _versioning_registry() -> tuple[RefcountedResourceRegistry[str, str], dict[str, list[str]]]:
    """A registry whose factory returns a fresh `<key>#<n>` each build, so a
    rebuild is observable."""
    log: dict[str, list[str]] = {"created": [], "torn_down": []}
    n = {"i": 0}

    async def factory(key: str) -> str:
        n["i"] += 1
        r = f"{key}#{n['i']}"
        log["created"].append(r)
        return r

    async def teardown(resource: str) -> None:
        log["torn_down"].append(resource)

    return RefcountedResourceRegistry(factory, teardown), log


@pytest.mark.anyio
async def test_invalidate_under_pin_tears_down_and_rebuilds():
    reg, log = _versioning_registry()
    async with reg.pin("a"):
        async with reg.borrow("a") as r1:
            assert r1 == "a#1"
        assert log["torn_down"] == []  # kept warm by the pin
        # Simulate death: invalidate the cached resource (by identity — pass the
        # exact instance the borrow yielded, as real callers do with `_Builder`).
        await reg.invalidate("a", r1)
        assert log["torn_down"] == ["a#1"]  # dead resource torn down
        # Next borrow rebuilds (pin survived), yielding a fresh resource.
        async with reg.borrow("a") as r2:
            assert r2 == "a#2"
        assert log["created"] == ["a#1", "a#2"]
    assert log["torn_down"] == ["a#1", "a#2"]  # pin release tears down the rebuild


@pytest.mark.anyio
async def test_invalidate_is_noop_when_resource_already_replaced():
    reg, log = _versioning_registry()
    async with reg.pin("a"):
        async with reg.borrow("a"):
            pass
        # Invalidating a resource that isn't the cached one (e.g. a concurrent
        # caller already recycled) is a no-op — the warm resource stays.
        await reg.invalidate("a", "a#999")
        assert log["torn_down"] == []
        async with reg.borrow("a") as r:
            assert r == "a#1"  # reused, not rebuilt
        assert log["created"] == ["a#1"]
