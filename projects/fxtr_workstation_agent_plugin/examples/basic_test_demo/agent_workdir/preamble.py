"""Preamble for the basic_test_demo agent_workdir.

Re-executed in the sandbox before every ``run_script`` call.
Globals defined here are in scope for the per-call script,
which lets the agent's one-liner script bodies just call into
:mod:`demo_pkg.scenarios` without repeating import boilerplate.

``_fxtr_mcp_builtin`` is installed before the preamble runs, so
the common builtins can be imported here once and used directly
from each script body. (Note: ``exec()`` for the preamble itself
doesn't permit top-level await, so async builtins can be
imported here but only *called* from the per-script body.)
"""

from _fxtr_mcp_builtin import (  # noqa: F401
    NixWasmServiceDefn,
    ServiceRef,
    UnknownService,
    WorkstationSessionEventInbox,
    callback_notification_keys,
    create_callback,
    current_commit,
    export_entity,
    export_service,
    forget,
    forget_callback_notification,
    get_id_for_service,
    get_service,
    lookup_entity,
    lookup_entity_by_name,
    lookup_service_by_name,
    result_type_of,
    retrieve_callback_notification,
    retrieve_nix_entity,
    retrieve_result,
    schedule_invoke,
)

from demo_pkg import (  # noqa: F401
    CALLBACK_TESTER_NAME,
    CallbackTesterProto,
    DELAYED_ECHO_NAME,
    DEMO_NOTE_NAME,
    DelayedEchoProto,
    Note,
    scenarios,
)
