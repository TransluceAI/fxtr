"""Demo scenarios for ``basic_test_demo``.

Each scenario function is an ``async`` callable intended to be
called from inside a sandboxed ``run_script`` body. They cover
the end-to-end paths the agent (or
``test_mcp_subprocess.py``) wants to exercise:

- :func:`schedule_basic_echo` / :func:`retrieve_basic_echo` —
  round-trip a scalar through the workstation's ``delayed-echo``
  service (untyped path; ``service_type=UnknownService`` default).
- :func:`schedule_typed_echo` / :func:`retrieve_typed_echo` —
  same round-trip via the typed ``DelayedEchoProto`` dispatch
  path, with ``retrieve_result`` using ``result_type_of(...)`` to
  get the method's typed return back.
- :func:`schedule_entity_arg_echo` /
  :func:`retrieve_entity_arg_echo` — pass a freshly-stored
  ``Note`` entity through the service and load it back.
- :func:`fetch_demo_note` — resolve the stable ``demo-note``
  entity by external_name and load it (no invoke needed).
- :func:`same_script_register_and_fire_callback` /
  :func:`cross_script_register_callback` /
  :func:`cross_script_fire_callback` /
  :func:`retrieve_one_callback_notification` — exercise the
  workstation-managed callback flow (stage 6). The
  ``callback-tester`` service stashes a callback ref and fires
  notifications back asynchronously; the agent observes them via
  ``notifications/claude/channel`` frames and retrieves
  payloads via ``retrieve_callback_notification``.

The pair-shape (schedule in one ``run_script``, retrieve in a
later one) mirrors how invoke results arrive — the
``notifications/claude/channel`` frame fires between the two
scripts and tells the agent when to issue the retrieve.
"""

from __future__ import annotations

from typing import Any

from fxtr2.data.entity_ref_scope import EntityRef

from . import CALLBACK_TESTER_NAME, DELAYED_ECHO_NAME, DEMO_NOTE_NAME
from .note import Note
from .typed_protos import CallbackTesterProto, DelayedEchoProto

#: Slot_key the basic-echo scenario writes its result into.
SLOT_BASIC = "basic_echo"

#: Slot_key the typed-echo scenario writes its result into.
SLOT_TYPED = "typed_echo"

#: Slot_key the entity-arg-echo scenario writes its result into.
SLOT_ENTITY_ARG = "entity_arg_echo"

#: Slot_key the same-script callback scenario uses for the
#: callback itself; the matching register / fire invokes use
#: ``{SLOT_CALLBACK}__register`` / ``{SLOT_CALLBACK}__fire`` to
#: avoid the same-script slot_key uniqueness check.
SLOT_CALLBACK = "demo_callback"

#: Slot_key the typed callback scenario uses; same shape as
#: :data:`SLOT_CALLBACK` but with a distinct key so both can
#: appear in the same test run without colliding.
SLOT_TYPED_CALLBACK = "typed_demo_callback"


async def schedule_basic_echo(value: str = "hello", delay: float = 0.05) -> None:
    """Schedule a ``delayed-echo`` invoke of ``value``. Result
    lands in slot :data:`SLOT_BASIC` once the workstation's invoke
    worker completes after ``delay`` seconds.
    """
    from _fxtr_mcp_builtin import lookup_service_by_name, schedule_invoke

    svc = await lookup_service_by_name(DELAYED_ECHO_NAME)
    await schedule_invoke(SLOT_BASIC, svc, lambda s: s.echo(value, delay))


async def retrieve_basic_echo() -> Any:
    """Retrieve the slot from :func:`schedule_basic_echo`. Raises
    ``LookupError`` if the result hasn't arrived yet (the agent
    should only call this after seeing the channel notification
    or polling)."""
    from _fxtr_mcp_builtin import retrieve_result

    return await retrieve_result(SLOT_BASIC)


async def schedule_typed_echo(value: str = "typed-hello", delay: float = 0.05) -> None:
    """Typed-dispatch variant of :func:`schedule_basic_echo`.

    Looks the service up with the explicit
    :class:`DelayedEchoProto` annotation so the sandbox's typed
    encoding path runs (args are bound to the Protocol method's
    signature, encoded through the dialect, and walked into the
    wire). Result lands in slot :data:`SLOT_TYPED`.
    """
    from _fxtr_mcp_builtin import lookup_service_by_name, schedule_invoke

    svc = await lookup_service_by_name(DELAYED_ECHO_NAME, DelayedEchoProto)
    await schedule_invoke(SLOT_TYPED, svc, lambda s: s.echo(value, delay))


async def retrieve_typed_echo() -> Any:
    """Retrieve :data:`SLOT_TYPED` with typed return decoding.

    Uses :func:`result_type_of(DelayedEchoProto, DelayedEchoProto.echo)`
    to drive the typed decoder through the layer-2 return wrapper.
    For ``echo``'s ``Any`` return annotation this is effectively a
    no-op (matches the untyped path), but it exercises the typed
    decode pipeline.
    """
    from _fxtr_mcp_builtin import result_type_of, retrieve_result

    return await retrieve_result(
        SLOT_TYPED, result_type_of(DelayedEchoProto, DelayedEchoProto.echo)
    )


async def schedule_entity_arg_echo(
    title: str = "passed-in",
    body: str = "round-trip",
) -> None:
    """Create a fresh :class:`Note` and pass it as an arg to the
    ``delayed-echo`` service. The service returns the same Note,
    which lands in slot :data:`SLOT_ENTITY_ARG`."""
    from _fxtr_mcp_builtin import lookup_service_by_name, schedule_invoke

    svc = await lookup_service_by_name(DELAYED_ECHO_NAME)
    note_ref = Note(title=title, body=body).store()
    await schedule_invoke(SLOT_ENTITY_ARG, svc, lambda s: s.echo(note_ref, 0.05))


async def retrieve_entity_arg_echo() -> Note:
    """Retrieve the echoed entity. ``retrieve_result`` returns a
    :class:`FxtrID` (the unescape step already resolved the wire
    ``["entity", short_id]`` tag); load it as a :class:`Note` to
    verify the round-trip preserved the fields."""
    from _fxtr_mcp_builtin import retrieve_result

    fxtr_id = await retrieve_result(SLOT_ENTITY_ARG)
    return await EntityRef(fxtr_id, Note).load()


async def fetch_demo_note() -> Note:
    """Resolve the stable ``demo-note`` entity registered by the
    demo workstation script and load it. No invoke needed —
    pure read-side lookup via ``lookup_entity_by_name``."""
    from _fxtr_mcp_builtin import lookup_entity_by_name

    fxtr_id = await lookup_entity_by_name(DEMO_NOTE_NAME)
    return await EntityRef(fxtr_id, Note).load()


# --- Callback scenarios (stage 6) -----------------------------------


async def same_script_register_and_fire_callback(
    payload: Any = "hello-callback",
    delay: float = 0.05,
) -> None:
    """Same-script callback flow.

    Creates a callback at :data:`SLOT_CALLBACK`, then in the same
    script schedules two ``callback-tester`` invokes:

    1. ``register_callback(cb)`` — stashes the callback ref on
       the demo service.
    2. ``fire_notify(payload, delay)`` — fire-and-forget;
       returns immediately and the demo service notifies the
       stashed callback after ``delay`` seconds.

    The notification lands as a
    ``<channel kind="callback" slot_key="demo_callback"
    notif_index="...">`` frame; the payload is retrievable via
    :func:`retrieve_one_callback_notification` in a later script.

    Three slot-claiming ops in one mutation (one
    ``create_callback`` + two ``schedule_invoke``s) — exercises
    monotonic slot allocation across kinds.
    """
    from _fxtr_mcp_builtin import (
        create_callback,
        lookup_service_by_name,
        schedule_invoke,
    )

    svc = await lookup_service_by_name(CALLBACK_TESTER_NAME)
    cb = create_callback(SLOT_CALLBACK)
    await schedule_invoke(f"{SLOT_CALLBACK}__register", svc, lambda s: s.register_callback(cb))
    await schedule_invoke(f"{SLOT_CALLBACK}__fire", svc, lambda s: s.fire_notify(payload, delay))


async def cross_script_register_callback() -> None:
    """First half of the cross-script callback flow.

    Just opens a callback at :data:`SLOT_CALLBACK`. The follow-up
    in a second ``run_script`` retrieves the stub via
    ``retrieve_result`` and uses it.
    """
    from _fxtr_mcp_builtin import create_callback

    _ = create_callback(SLOT_CALLBACK)


async def cross_script_fire_callback(
    payload: Any = "cross-script-hello",
    delay: float = 0.05,
) -> None:
    """Second half of the cross-script callback flow.

    Reads the committed callback stub from :data:`SLOT_CALLBACK`
    via ``retrieve_result``, then schedules register + fire on
    the ``callback-tester``.
    """
    from _fxtr_mcp_builtin import (
        lookup_service_by_name,
        retrieve_result,
        schedule_invoke,
    )

    svc = await lookup_service_by_name(CALLBACK_TESTER_NAME)
    cb = await retrieve_result(SLOT_CALLBACK)
    await schedule_invoke(f"{SLOT_CALLBACK}__register", svc, lambda s: s.register_callback(cb))
    await schedule_invoke(f"{SLOT_CALLBACK}__fire", svc, lambda s: s.fire_notify(payload, delay))


async def retrieve_one_callback_notification(
    slot_key: str = SLOT_CALLBACK,
) -> Any:
    """Read the most-recent stored callback notification for
    ``slot_key`` and return the decoded payload.

    Raises :class:`AssertionError` if the slot has no stored
    notifications yet (a sign the channel frame hasn't been
    processed before the test polled).
    """
    from _fxtr_mcp_builtin import (
        callback_notification_keys,
        retrieve_callback_notification,
    )

    keys = await callback_notification_keys(slot_key)
    assert keys, f"no notifications stored for {slot_key!r}"
    return await retrieve_callback_notification(slot_key, keys[-1])


async def typed_same_script_register_and_fire_callback(
    payload: Any = "typed-hello-callback",
    delay: float = 0.05,
) -> None:
    """Typed variant of :func:`same_script_register_and_fire_callback`.

    Uses the explicit :class:`CallbackTesterProto` annotation on
    ``lookup_service_by_name`` so the typed-dispatch path runs.
    The owned-callback :class:`ServiceRef` returned by
    ``create_callback`` is passed straight into
    ``register_callback`` — the wire walker emits
    ``["callback_handle", slot_key]`` for the unbound
    same-script form and the host's mutation builder rewrites it
    to ``["callback", N]`` once the slot is allocated.
    """
    from _fxtr_mcp_builtin import (
        create_callback,
        lookup_service_by_name,
        schedule_invoke,
    )

    svc = await lookup_service_by_name(CALLBACK_TESTER_NAME, CallbackTesterProto)
    cb = create_callback(SLOT_TYPED_CALLBACK)
    await schedule_invoke(
        f"{SLOT_TYPED_CALLBACK}__register",
        svc,
        lambda s: s.register_callback(cb),
    )
    await schedule_invoke(
        f"{SLOT_TYPED_CALLBACK}__fire",
        svc,
        lambda s: s.fire_notify(payload, delay),
    )
