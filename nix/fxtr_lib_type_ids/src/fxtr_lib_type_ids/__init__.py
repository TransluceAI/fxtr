"""Stub re-declarations of library type-ids (workstation / report /
fxtr_experiment) that ``lib.fxtr.entities`` materializes as typeEntity
layers.

Each constant re-declares a type id via ``system_entities.define_type(NS,
label)`` using the **same namespace UUID + label** as the canonical
definition. FxtrIDs are content-addressed, so the result is byte-identical to
the real type — yet this package depends only on ``fxtr2``, letting
``mkEntityFromPython`` build the typeEntity layers from a light venv instead of
the heavy ``fxtr_workstation_mcp`` / ``fxtr_report_builder`` /
``fxtr_experiment`` dependency closures.

**Keep in sync with the canonical definitions.** If a canonical type's
namespace UUID or label ever changes, update the mirror here to match —
otherwise the built typeEntity id will no longer equal the preset key, and the
nix/git dispatcher's eager validation will reject the preset (loudly). Each
constant names its canonical source below.
"""

from __future__ import annotations

import uuid

# CHANNEL_READER_TYPE_ID is a direct re-export, not a mirrored
# re-declaration: its canonical definition lives in fxtr2 itself (this
# package's only dependency), so there is nothing to keep in sync.
from fxtr2.common.channels import CHANNEL_READER_TYPE_ID as CHANNEL_READER_TYPE_ID
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID

# Mirror of ``fxtr_workstation_mcp.agent_log_types.WS_AGENT_NS``.
_WS_AGENT_NS = uuid.UUID("8b3a7e1d-c4f0-46d2-9a51-7e8c1f3b2a09")
# Mirror of ``fxtr_report_builder.entities._FXTR_REPORTS_NS``.
_FXTR_REPORTS_NS = uuid.UUID("8e1a2c6d-4a9b-4d70-8f53-2c7b9e1d4a05")

# fxtr_workstation_mcp.agent_mcp_proto:AGENT_MCP_RUNNING_SESSION_TYPE_ID
AGENT_MCP_RUNNING_SESSION_TYPE_ID: FxtrID = system_entities.define_type(
    _WS_AGENT_NS, "agent_mcp_running_session"
)
# fxtr_workstation_mcp.agent_log_types:ScriptExecutionLogEvent.fxtr_type
LOG_SCRIPT_EXECUTION_TYPE_ID: FxtrID = system_entities.define_type(
    _WS_AGENT_NS, "log_script_execution"
)
# fxtr_workstation_mcp.agent_log_types:InvokeResultLogEvent.fxtr_type
LOG_INVOKE_RESULT_TYPE_ID: FxtrID = system_entities.define_type(_WS_AGENT_NS, "log_invoke_result")
# fxtr_workstation_mcp.agent_log_types:InvokeErrorLogEvent.fxtr_type
LOG_INVOKE_ERROR_TYPE_ID: FxtrID = system_entities.define_type(_WS_AGENT_NS, "log_invoke_error")
# fxtr_workstation_mcp.agent_log_types:CallbackNotificationLogEvent.fxtr_type
LOG_CALLBACK_NOTIFICATION_TYPE_ID: FxtrID = system_entities.define_type(
    _WS_AGENT_NS, "log_callback_notification"
)
# fxtr_workstation_mcp.agent_log_types:WorkstationServicePlaceholder.fxtr_type
WORKSTATION_SERVICE_PLACEHOLDER_TYPE_ID: FxtrID = system_entities.define_type(
    _WS_AGENT_NS, "workstation_service_placeholder"
)

# fxtr_report_builder.entities:ROOT_REPORT_TYPE_ID
ROOT_REPORT_TYPE_ID: FxtrID = system_entities.define_type(_FXTR_REPORTS_NS, "RootReport")
# fxtr_report_builder.entities:SUB_REPORT_TYPE_ID
SUB_REPORT_TYPE_ID: FxtrID = system_entities.define_type(_FXTR_REPORTS_NS, "SubReport")

# Mirror of ``fxtr_experiment_task._ns.WORKFLOWS3_NS``.
_WORKFLOWS3_NS = uuid.UUID("6843e93c-d2d8-4b1b-bfbe-592e4ca6defc")

# fxtr_experiment.user_service:USER_CAPABILITY_TYPE_ID (a SERVICE type id)
EXPERIMENT_USER_CAPABILITY_TYPE_ID: FxtrID = system_entities.define_type(
    _WORKFLOWS3_NS, "user_capability"
)
# fxtr_experiment.frame_encoding:FRAME_TYPE_ID
EXPERIMENT_FRAME_TYPE_ID: FxtrID = system_entities.define_type(_WORKFLOWS3_NS, "frame_v0")
# fxtr_experiment.encoding:GRID_CONFIG_TYPE_ID
EXPERIMENT_GRID_CONFIG_TYPE_ID: FxtrID = system_entities.define_type(
    _WORKFLOWS3_NS, "grid_config_v0"
)
# fxtr_experiment.encoding:SLICE_TEMPLATE_TYPE_ID
EXPERIMENT_SLICE_TEMPLATE_TYPE_ID: FxtrID = system_entities.define_type(
    _WORKFLOWS3_NS, "slice_template_v0"
)
