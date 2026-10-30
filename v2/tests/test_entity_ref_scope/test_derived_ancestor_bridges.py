"""Tests for the ancestor-bridge plumbing under the uniform derived token.

Every scope's token is a ``DerivedLocalLifetime`` over a private key plus each
direct parent's main token, so every direct parent gets a **case-3
``DependentLocalBridge``** ancestor entry (converter
``child._derived.subtake(base, parent.main_token)``, no stored lease), and
transitive ancestors get a *composed* Dependent entry. Two scenarios:

1. **Single-level.** A child with ``include=[outer]`` gets a
   ``DependentLocalBridge`` ancestor entry on ``outer._token``;
   ``_mint_ancestor_lease`` produces a usable fresh lease and the drain ordering
   is cycle-free.
2. **Transitive Dependent-ancestor walk.** Three-deep chain ``S1 → S2 → S3``;
   ``S3`` ends up with ancestor entries for both ``S2._token`` (direct
   Dependent) and ``S1._token`` (transitive composed Dependent).

(The old "unconditional grandchild of a derived parent" eager-eval scenario is
gone: there are no unconditional scopes anymore — every scope is derived, so the
transitive walk always installs a composed Dependent entry, covered by
scenario 2.)

The lease-leak autouse fixture (``tests/conftest.py``) catches any slipped lease
through the bridges or composed converters, so a test that finishes without a
lease-leak warning is the strongest passive assertion this plan can ask for.
"""

from __future__ import annotations

import pytest

from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.leases.lease_bridges import DependentLocalBridge

# ---------------------------------------------------------------------------
# Single-level: parent ancestor entry is case-3 Dependent
# ---------------------------------------------------------------------------


class TestParentAncestorBridge:
    @pytest.mark.anyio
    async def test_parent_installs_dependent_ancestor(self) -> None:
        async with FxtrScope(name="outer", isolated=True) as outer:
            async with FxtrScope(name="child", isolated=True, include=[outer]) as child:
                entry = child._cap_bridges.get(outer._token)
                assert entry is not None, "ancestor entry missing for parent token"
                assert entry.is_ancestor_or_arena_owner is True
                assert isinstance(
                    entry.bridge, DependentLocalBridge
                ), f"expected DependentLocalBridge, got {type(entry.bridge).__name__}"
                # Empty stored leases — case-3 reads everything from base.
                assert entry.bridge.leases == {}

    @pytest.mark.anyio
    async def test_mint_ancestor_lease_returns_fresh_parent_lease(self) -> None:
        async with FxtrScope(name="outer", isolated=True) as outer:
            async with FxtrScope(name="child", isolated=True, include=[outer]) as child:
                minted = child._mint_ancestor_lease(outer._token)
                assert minted.subject is outer._token
                assert minted.is_active()
                # The minted lease's grant matches the parent's base grant
                # (case-3 just clones the via, which was minted from the
                # parent's authorization at the child's exchange_forward).
                assert minted.grant == outer._scope_auth_lease.grant
                minted.discard()

    @pytest.mark.anyio
    async def test_drain_does_not_deadlock(self) -> None:
        # A child can drain even when external holders kept clones of its
        # output past its own aclose, because the parent's drain isn't pinned
        # by an Independent ancestor lease on us (case-3 holds nothing at rest).
        async with FxtrScope(name="outer", isolated=True) as outer:
            child = FxtrScope(name="child", isolated=True, include=[outer])
            async with child.open_with_explicit_leases(
                parent_leases={outer: outer.clone_authorization()}
            ):
                # External holder: a stray clone of child's main lease.
                stray = child.clone_authorization()
                # Release the stray before block exit so the child can drain.
                stray.discard()
            # outer's exit (below) drains cleanly; the leak fixture would catch
            # any pinned lease.


# ---------------------------------------------------------------------------
# Transitive Dependent-ancestor walk
# ---------------------------------------------------------------------------


class TestTransitiveDependentAncestor:
    @pytest.mark.anyio
    async def test_three_deep_derived_chain(self) -> None:
        # S1 → S2 → S3, each deriving from its parent.
        async with FxtrScope(name="s1", include=[GLOBAL_SCOPE], isolated=True) as s1:
            async with FxtrScope(name="s2", include=[s1], isolated=True) as s2:
                async with FxtrScope(name="s3", include=[s2], isolated=True) as s3:
                    # Direct ancestor on S3 for S2's token: Dependent (case-3
                    # against S3's own derived).
                    s2_entry = s3._cap_bridges.get(s2._token)
                    assert s2_entry is not None
                    assert s2_entry.is_ancestor_or_arena_owner is True
                    assert isinstance(s2_entry.bridge, DependentLocalBridge)

                    # Transitive ancestor on S3 for S1's token: also Dependent
                    # (composed: S3.subtake → Lease[S2.main], then S2's
                    # converter → Lease[S1.main]).
                    s1_entry = s3._cap_bridges.get(s1._token)
                    assert s1_entry is not None
                    assert s1_entry.is_ancestor_or_arena_owner is True
                    assert isinstance(s1_entry.bridge, DependentLocalBridge)

                    # Mint ancestor leases through both entries; check they're
                    # active and on the right tokens.
                    minted_s2 = s3._mint_ancestor_lease(s2._token)
                    assert minted_s2.subject is s2._token
                    assert minted_s2.is_active()
                    minted_s2.discard()

                    minted_s1 = s3._mint_ancestor_lease(s1._token)
                    assert minted_s1.subject is s1._token
                    assert minted_s1.is_active()
                    minted_s1.discard()
