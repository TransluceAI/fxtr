"""End-to-end acceptance test for cross-task derived-scope drain.

Three concurrent tasks exchange arenas, leases, and a converter callable
across in-process streams to exercise the scope / lease / derived-lifetime
machinery as a whole. The predictions about *what closes when* pin the
cross-task drain ordering.

Under the stage-12 uniform derived token this is built with
``FxtrScope.open_with_explicit_leases(extra_derived_leases=…)`` — task 2 derives
``scope2``'s lifetime from task 1's *external* token (it holds only a
token+lease, no FxtrScope object), the idiomatic stand-in for the dispatch
arg-token pattern. (Originally written against the removed
``FxtrScope._with_derived_lifetime``; scope1's only non-key input and scope2's
extra input are GLOBAL, which never ends, so the drain predictions are
unchanged.)

This test is also the canonical worked example for the three lifetime-
conversion patterns: scope2's attach of t1's arena is **case 3** (X
derived from Y, exchange_reverse + STATIC key), scope1's attach of
scope2's arena is **case 2** (Y derived from X, exchange_forward + key
on a separate gating brand), and scope3's attach of scope2's arena is
**case 1** (caller already holds a Lease[Y], default converter). For the
full explainer of why these three cases exist and how to pick between
them, read
``plans/phase3/stage8c_user_caps/lifetime_conversion_mechanics.md`` —
section 4 walks through these three attaches in detail.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import anyio
import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, EntityRef, FxtrScope
from fxtr2.leases.lease_bridges import DependentLocalBridge, IndependentLocalBridge
from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.local_lease import STATIC_LIFETIME

# ---------------------------------------------------------------------------
# Test entity type
# ---------------------------------------------------------------------------

_ACCEPTANCE_TYDEF_UUID = uuid.UUID("d7f0a7e5-1f3c-4f11-9e44-2c9c84a3a7c1")


@dataclasses.dataclass(frozen=True)
class TaskNode(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_ACCEPTANCE_TYDEF_UUID, "task_acceptance_node"),
):
    children: tuple[EntityRef[TaskNode], ...]
    value: str


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Yield to the scheduler until ``predicate()`` is True or timeout fires."""
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0)


class TestThreeTaskAcceptance:
    """The full task-1 / task-2 / task-3 acceptance test from the sketch.

    Verifies the five "what closes when" predictions spelled out in
    ``part3_plan.md`` substage-3 acceptance criteria.
    """

    @pytest.mark.anyio
    async def test_three_tasks_drain_in_lockstep(self):
        # Shared so the test harness (running as task 3) can poke at internals.
        scopes: dict[str, FxtrScope] = {}

        # Stream pairs.
        t1_to_t2_send, t1_to_t2_recv = anyio.create_memory_object_stream[Any](max_buffer_size=1)
        t2_to_t1_send, t2_to_t1_recv = anyio.create_memory_object_stream[Any](max_buffer_size=1)
        t2_to_t3_send, t2_to_t3_recv = anyio.create_memory_object_stream[Any](max_buffer_size=1)
        t1_to_t3_send, t1_to_t3_recv = anyio.create_memory_object_stream[Any](max_buffer_size=1)

        # Task 3 drives the timing: it triggers task 1's drain at the right
        # moment via this event.
        t1_may_drain = anyio.Event()

        async def task1():
            async with FxtrScope(name="t1", isolated=True, include=[GLOBAL_SCOPE]) as scope1:
                scopes["scope1"] = scope1

                ent1 = TaskNode(children=(), value="from t1").store(scope=scope1)
                t1_ent_id = ent1.entity_id

                # Fork an arena slice containing just our entity, and a
                # lease for task 2 to consume as its derived-lifetime input.
                arena_for_t2 = scope1._main_arena.copy_to_new_arena(
                    scope1.clone_authorization(), [t1_ent_id]
                )
                lease_for_t2_derived = scope1.clone_authorization()

                await t1_to_t2_send.send((arena_for_t2, lease_for_t2_derived, t1_ent_id))
                await t1_to_t2_send.aclose()

                # Receive task 2's derived-output bridge.
                key_lease_t1, converter_t1, new_arena_t1, t2_ids = await t2_to_t1_recv.receive()

                # Lazily attach each of task 2's new entities into scope1's
                # main arena, bridging via the converter task 2 supplied.
                # Wrap the (key, base) → Y converter as a DependentLocalBridge
                # whose stored input is the key.
                t1_key_token = key_lease_t1.subject

                def t1_dep_converter(
                    inputs: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
                ) -> Lease[Any]:
                    key = inputs.pop(t1_key_token)
                    for v in inputs.values():
                        v.discard()
                    return converter_t1(key, base)

                for new_id in t2_ids:
                    scope1._main_arena.lazily_attach_from_arena(
                        scope1.clone_authorization(),
                        new_id,
                        new_arena_t1,
                        DependentLocalBridge(
                            leases={t1_key_token: key_lease_t1.clone()},
                            converter=t1_dep_converter,
                        ),
                    )

                # Loading each of these reaches all the way back to t1's
                # original entity transitively (it's a child reference).
                for new_id in t2_ids:
                    loaded = await scope1.load(EntityRef(new_id, TaskNode))
                    assert loaded.value.startswith("from t2")
                    assert len(loaded.children) == 1
                    assert loaded.children[0].entity_id == t1_ent_id

                # Hand task 3 a clone of our main lease.
                await t1_to_t3_send.send(scope1.clone_authorization())
                await t1_to_t3_send.aclose()

                # Local cleanup of consumed bridges.
                key_lease_t1.discard()
                new_arena_t1.close()

                # Wait for task 3 to release t1's lease before we drain — the
                # CM exit then triggers scope1.aclose, which (in concert with
                # task 3's release) drains scope1_ULL and cascades into
                # scope2's derived output.
                await t1_may_drain.wait()

        async def task2():
            (
                arena_from_t1,
                lease_for_derived,
                t1_ent_id,
            ) = await t1_to_t2_recv.receive()
            t1_token: LifetimeToken[Any] = lease_for_derived.subject

            # scope2's lifetime derives from t1_token — an *external* token (no
            # FxtrScope object here; it arrived over a stream). The entry
            # primitive expresses this via ``extra_derived_leases``. GLOBAL_SCOPE
            # is an ordinary parent (case-3 derived input); it never ends, so it
            # does not change scope2's drain behaviour.
            scope2 = FxtrScope(name="t2", isolated=True, include=[GLOBAL_SCOPE])
            try:
                scopes["scope2"] = scope2
                async with scope2.open_with_explicit_leases(
                    parent_leases={GLOBAL_SCOPE: GLOBAL_SCOPE.clone_authorization()},
                    extra_derived_leases={t1_token: lease_for_derived},
                ):
                    derived = scope2._derived
                    assert derived is not None
                    assert scope2._key_ull is not None
                    key_token = scope2._key_ull.token

                    def _key_clone() -> Lease[Any]:
                        # The key root is discarded at entry; split a fresh key
                        # lease out of scope2's current authorization.
                        auth = scope2.clone_authorization()
                        try:
                            return derived.subclone(auth, key_token)
                        finally:
                            auth.discard()

                    # Case-3 attach (see ``LifetimeConverter`` docstring):
                    # scope2's token is derived from t1_token, so a live
                    # Lease[scope2_token] already structurally witnesses
                    # t1 being alive. Use ``exchange_reverse`` to pull a
                    # Lease[t1_token] out of the consumer's base on each
                    # load (discarding the key + GLOBAL vias), and
                    # ``STATIC_LIFETIME`` as the stored key so the foreign-lease
                    # row holds nothing that would cyclically pin t1_token.
                    def t2_attach_converter(key: Lease[Any], base: Lease[Any]) -> Lease[Any]:
                        key.discard()
                        inputs = derived.exchange_reverse(base)
                        inputs[key_token].discard()
                        inputs[GLOBAL_SCOPE._token].discard()
                        return inputs[t1_token]

                    def t2_dep_converter(
                        inputs: dict[LifetimeToken[Any], Lease[Any]], base: Lease[Any]
                    ) -> Lease[Any]:
                        for v in inputs.values():
                            v.discard()
                        return t2_attach_converter(STATIC_LIFETIME.lease(), base)

                    scope2._main_arena.lazily_attach_from_arena(
                        scope2.clone_authorization(),
                        t1_ent_id,
                        arena_from_t1,
                        DependentLocalBridge(leases={}, converter=t2_dep_converter),
                    )

                    # Store new entities, each referencing t1's original.
                    new_refs: list[EntityRef[TaskNode]] = []
                    for i in range(2):
                        ref = TaskNode(
                            children=(EntityRef(t1_ent_id, TaskNode),),
                            value=f"from t2 #{i}",
                        ).store(scope=scope2)
                        new_refs.append(ref)
                    new_ids = [r.entity_id for r in new_refs]

                    # Two arena copies, one per recipient (each consumes its
                    # own lease clone internally).
                    new_arena_a = scope2._main_arena.copy_to_new_arena(
                        scope2.clone_authorization(), new_ids
                    )
                    new_arena_b = scope2._main_arena.copy_to_new_arena(
                        scope2.clone_authorization(), new_ids
                    )

                    # Case-2 converter for tasks 1 and 3 (see
                    # ``LifetimeConverter`` docstring): scope2's token is
                    # derived from t1_token, so the consumer (whose own
                    # token is t1_token) provides a fresh
                    # Lease[t1_token] as base each load, plus a key clone
                    # of scope2's key_ull. exchange_forward mints a
                    # Lease[scope2_token] on demand.
                    # GLOBAL_SCOPE is also a derived input now, so exchange_forward
                    # must supply a GLOBAL lease too (minted fresh — GLOBAL is
                    # authorized everywhere and never ends).
                    def t2_forward_converter(key: Lease[Any], base: Lease[Any]) -> Lease[Any]:
                        return derived.exchange_forward(
                            {
                                key_token: key,
                                GLOBAL_SCOPE._token: GLOBAL_SCOPE.clone_authorization(),
                                t1_token: base,
                            }
                        )

                    await t2_to_t1_send.send(
                        (
                            _key_clone(),
                            t2_forward_converter,
                            new_arena_a,
                            new_ids,
                        )
                    )
                    await t2_to_t1_send.aclose()
                    await t2_to_t3_send.send(
                        (
                            _key_clone(),
                            t2_forward_converter,
                            new_arena_b,
                            new_ids,
                        )
                    )
                    await t2_to_t3_send.aclose()
                    # Falls through: scope2's CM exit waits on the derived
                    # output token, which ends via the input-end cascade
                    # once t1_token ends.
            except BaseException:
                scope2.force_close()
                raise

        async def task3():
            key_lease_t3, converter_t3, new_arena_t3, t2_ids = await t2_to_t3_recv.receive()
            lease_from_t1 = await t1_to_t3_recv.receive()

            # ─── Prediction 1: mid-run invariants ────────────────────────────
            scope1 = scopes["scope1"]
            scope2 = scopes["scope2"]
            assert scope2._key_ull is not None
            assert not scope1._token.ended()
            assert not scope2._token.ended()
            assert not scope2._key_ull.token.ended()

            # Reconstruct a Lease[scope2_token] from (key, base) and use it as
            # the key for the lazy attach (default converter, Y == Z).
            t2_output_lease_for_attach = converter_t3(key_lease_t3.clone(), lease_from_t1.clone())
            # And a separate one we hold ourselves so we have something
            # concrete to drop for prediction 2.
            t2_output_lease_extra = converter_t3(key_lease_t3.clone(), lease_from_t1.clone())

            async with FxtrScope(name="t3", isolated=True, include=[GLOBAL_SCOPE]) as scope3:
                scope3._main_arena.lazily_attach_from_arena(
                    scope3.clone_authorization(),
                    t2_ids[0],
                    new_arena_t3,
                    IndependentLocalBridge(lease=t2_output_lease_for_attach),
                )
                loaded = await scope3.load(EntityRef(t2_ids[0], TaskNode))
                assert loaded.value.startswith("from t2")

            # scope3 has drained (its foreign-bridge cleanup ran). Close
            # new_arena_t3 (no lease held — passive view) so it doesn't sit
            # registered in the store.
            new_arena_t3.close()

            # ─── Prediction 2: drop our extra t2 output-lease clone ─────────
            # Nothing should close: scope1 still has its main lease, scope2
            # still has output leases inside the live derived (e.g. via
            # vias that other holders carry), and scope2's key still has
            # both stored copies in t1's foreign-leases and our key_lease_t3.
            t2_output_lease_extra.discard()
            assert not scope1._token.ended()
            assert not scope2._token.ended()
            assert not scope2._key_ull.token.ended()

            # ─── Prediction 3: drop lease_from_t1 → both output tokens close ─
            # Combined with task 1's own aclose discarding scope1._scope_auth_lease,
            # this releases the last clone of t1_token. scope1_ULL drains
            # synchronously; that ends scope1's token, which cascades through
            # scope2's derived (t1_token is an input) and ends scope2's
            # output token too. Both tasks exit their CMs.
            lease_from_t1.discard()
            t1_may_drain.set()

            # Task 1's `async with` exit and task 2's derived-aclose both
            # complete via that cascade. Wait for the predicted state.
            await _wait_for(lambda: scope1._token.ended() and scope2._token.ended())
            assert scope1._token.ended()
            assert scope2._token.ended()
            # Key ULL still dangles — task 3 still holds key_lease_t3.
            assert not scope2._key_ull.token.ended()

            # ─── Prediction 4: drop key_lease_t3 → key ULL ends ─────────────
            key_ull = scope2._key_ull
            assert key_ull is not None
            key_lease_t3.discard()
            await _wait_for(lambda: key_ull.token.ended())
            assert key_ull.token.ended()

        async with anyio.create_task_group() as tg:
            tg.start_soon(task1)
            tg.start_soon(task2)
            tg.start_soon(task3)
