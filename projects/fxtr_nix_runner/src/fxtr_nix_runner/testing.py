"""Test-support helpers for downstream suites that depend on a configured
nix backend / container runtime.

Since the config has **no implicit default** (see :mod:`fxtr_nix_runner._config`),
production code hard-fails at point of use when ``nix_backend`` /
``container_runtime_cmd`` are unset. For *tests*, that would turn a cold
``pytest`` on an unconfigured machine into a wall of errors, so downstream E2E
suites instead **skip** when nothing is configured — the limit case of the
backend/runtime sweeps' existing skip-when-*unavailable*.

The one hazard — a CI lane that *means* to run these but forgot to set the env
would skip → go green silently — is closed by the opt-in
``FXTR_TESTS_REQUIRE_RUNTIME`` env: when set, an unconfigured machine **fails**
loudly instead of skipping. CI sets it; local cold runs leave it unset.
"""

from __future__ import annotations

import os

from fxtr_nix_runner._config import ENV_NIX_BACKEND, ENV_RUNTIME_CMD, WIZARD_HINT, load_config

#: Set this (to any non-empty value) in CI so an unconfigured runtime **fails**
#: the suite loudly instead of skipping it.
ENV_REQUIRE_RUNTIME = "FXTR_TESTS_REQUIRE_RUNTIME"


def skip_if_runtime_unconfigured() -> None:
    """Skip the current test when no nix backend is configured.

    A no-op when ``nix_backend`` is set (via the default-location file, the
    full-file override, or ``FXTR_NIX_BACKEND``) — including every backend/
    runtime-sweep variant, which sets the env explicitly. When unset:

    - ``FXTR_TESTS_REQUIRE_RUNTIME`` set → :func:`pytest.fail` (the CI guard:
      a misconfigured CI fails loudly rather than skipping silently).
    - otherwise → :func:`pytest.skip` with an actionable reason.

    Checks ``nix_backend`` only — the universal requirement (every consumer
    constructs a backend). A container runtime, needed only by image delivery,
    is supplied by the sweep / env for the tests that need it and enforced at
    point of use by ``require_container_runtime``.
    """
    import pytest

    cfg = load_config()
    if cfg.nix_backend is not None:
        return

    message = (
        "no nix backend configured; set "
        f"{ENV_NIX_BACKEND}=host|podman (and {ENV_RUNTIME_CMD} for container "
        f"delivery), or {WIZARD_HINT}."
    )
    if os.environ.get(ENV_REQUIRE_RUNTIME):
        pytest.fail(f"{message} ({ENV_REQUIRE_RUNTIME} is set, so this is a failure, not a skip.)")
    pytest.skip(message)
