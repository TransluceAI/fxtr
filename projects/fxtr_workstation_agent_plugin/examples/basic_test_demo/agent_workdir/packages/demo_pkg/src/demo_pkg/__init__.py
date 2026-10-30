"""Shared package for the ``basic_test_demo`` example.

Lives at ``agent_workdir/packages/demo_pkg/src/demo_pkg/`` (src
layout) so it's both:

- importable from the agent's sandboxed scripts — the
  ``run_script`` tool walks the agent's ``packages/`` dir,
  finds each ``<pkg>/pyproject.toml`` + ``<pkg>/src/`` pair,
  and adds ``<pkg>/src`` to the sandbox's ``sys.path``; and
- importable by the standalone ``run_demo_workstation.py``
  process (which adds the same ``src/`` to its own
  ``sys.path`` near the top of its main).

This single source of truth keeps the entity ``fxtr_type``
registration and the workstation external_names in lockstep
across both sides.
"""

from __future__ import annotations

#: external_name the demo workstation registers the delayed-echo
#: service under. Agents resolve it via
#: :func:`_fxtr_mcp_builtin.lookup_service_by_name`.
DELAYED_ECHO_NAME = "delayed-echo"

#: external_name the demo workstation registers a stable Note
#: entity under. Agents resolve it via
#: :func:`_fxtr_mcp_builtin.lookup_entity_by_name`.
DEMO_NOTE_NAME = "demo-note"

#: external_name the demo workstation registers the
#: callback-tester service under. The service exposes
#: ``register_callback(cb)`` (stashes a callback ref for later
#: notification) and ``fire_notify(payload, delay)``
#: (fire-and-forget: schedules a background notify on the stashed
#: cb after ``delay`` seconds).
CALLBACK_TESTER_NAME = "callback-tester"

#: external_name the demo workstation registers the sandbox
#: spawner service under. Registered via the workstation's
#: ``external_services`` kwarg so the workstation treats it like
#: a fundamental builtin. Agents resolve it via
#: :func:`_fxtr_mcp_builtin.lookup_service_by_name` and pass a
#: ``NixWasmServiceDefn(git_commit=current_commit(), flake_attr=...)``
#: to ``spawn`` to build + launch a sandboxed service from the
#: workdir flake's ``fxtr.services.<name>``.
SANDBOX_SPAWNER_NAME = "sandbox-spawner"

#: external_name the demo workstation registers the
#: open-internet passthrough network authorizer under. Agents
#: resolve it, then bridge it into a sandboxed service's
#: ``connect`` config, where the sandboxed code can hand it to
#: ``fxtr_sandbox_rpc_adapter.get_client(authorizer)`` for
#: HTTP/HTTPS access to any host on the public internet. The
#: demo registers a passthrough (no whitelist) — convenient but
#: also gives the agent's sandboxed code unrestricted outbound
#: HTTP.
INTERNET_AUTHORIZER_NAME = "internet-authorizer"

#: external_name for the host-side Anthropic-API authorizer.
#: Only registered when the demo workstation is started with
#: ``--allow-llm-apis`` *and* ``ANTHROPIC_API_KEY`` is set on the
#: host. The authorizer fixes the upstream to
#: ``https://api.anthropic.com`` and injects the key as a
#: header, so sandboxed code can call the API without ever
#: seeing the key.
ANTHROPIC_AUTHORIZER_NAME = "anthropic-api-authorizer"

#: external_name for the host-side OpenAI-API authorizer. Same
#: shape as :data:`ANTHROPIC_AUTHORIZER_NAME` — only registered
#: when the demo workstation is started with ``--allow-llm-apis``
#: *and* ``OPENAI_API_KEY`` is set on the host.
OPENAI_AUTHORIZER_NAME = "openai-api-authorizer"

# Imported after the constants above so ``scenarios`` (which
# imports them via ``from . import ...``) doesn't hit a partial-
# init circular-import error.
from . import scenarios, typed_protos  # noqa: E402
from .note import Note  # noqa: E402
from .typed_protos import (  # noqa: E402
    CallbackTesterProto,
    DelayedEchoProto,
    WorkstationSessionEventInbox,
)

__all__ = [
    "ANTHROPIC_AUTHORIZER_NAME",
    "CALLBACK_TESTER_NAME",
    "CallbackTesterProto",
    "DELAYED_ECHO_NAME",
    "DEMO_NOTE_NAME",
    "DelayedEchoProto",
    "WorkstationSessionEventInbox",
    "INTERNET_AUTHORIZER_NAME",
    "Note",
    "OPENAI_AUTHORIZER_NAME",
    "SANDBOX_SPAWNER_NAME",
    "scenarios",
    "typed_protos",
]
