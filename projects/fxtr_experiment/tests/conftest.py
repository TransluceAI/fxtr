"""Pytest configuration: the ``nix_runtime`` gate for the sandbox e2e suite.

The core suite is sandbox-free (the fake launcher —
``test_sandbox_serving.py``); only ``nix_runtime``-marked tests launch
real sandboxes, and they skip on machines without a configured
`fxtr_nix_runner` backend (the `fxtr_sandboxing` test-gating
convention; ``FXTR_TESTS_REQUIRE_RUNTIME=1`` flips skip → fail for CI
machines that must have one).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _require_runtime_for_nix_tests(request: pytest.FixtureRequest) -> None:
    """Skip a ``nix_runtime``-marked test when no nix backend is configured."""
    if request.node.get_closest_marker("nix_runtime"):
        from fxtr_nix_runner.testing import skip_if_runtime_unconfigured

        skip_if_runtime_unconfigured()
