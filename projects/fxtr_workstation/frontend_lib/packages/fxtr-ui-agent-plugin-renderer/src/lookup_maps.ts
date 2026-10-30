/**
 * Pre-built lookup maps + type-id constants for consumers to
 * compose a dispatch registry concisely.
 *
 * The four event-payload-entity type-id strings + the
 * agent-mcp-running-session type-id string are hand-mirrored from
 * the Python ``system_entities.define_type(WS_AGENT_NS, ...)``
 * declarations in :mod:`fxtr_workstation_mcp.agent_log_types` and
 * :mod:`fxtr_workstation_mcp.agent_mcp_proto`. They're stable
 * given the namespace UUID + label, so once the Python side fixes
 * its labels these strings don't change.
 *
 * Consumers (the demo) should still assert these match the
 * runtime-injected ``TYPE_IDS`` blob from the Python backend's
 * stdout — that's the defense-in-depth that catches accidental
 * drift if the Python side renames a label.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import type { StreamEntryProps } from "@fxtr-ui/rd-types-stream";
import type { DispatchedComponent } from "@fxtr-ui/react-dispatch";
import type { FullPanelProps, LinkProps } from "@fxtr-ui/workstation-dispatch";

import { CallbackNotificationStreamEntry } from "./entries/callback_notification.js";
import { InvokeErrorStreamEntry } from "./entries/invoke_error.js";
import { InvokeResultStreamEntry } from "./entries/invoke_result.js";
import { ScriptExecutionStreamEntry } from "./entries/script_execution.js";
import { WorkstationServicePlaceholderInlineLink } from "./inline_link_substitution.js";
import { AgentMcpFullPanel } from "./panel.js";

/**
 * Wire-stable type-id strings for the agent plugin's entities +
 * service.
 *
 * - ``agent_mcp_running_session`` — service exposed under
 *   ``agent_mcp:{session_id}`` on the workstation.
 * - ``ws_agent_log_chain`` — the head-only linked-list spine.
 *   Not registered with any dispatch key here (the panel loads
 *   chain heads itself; renderers consume the event payloads).
 * - The four ``log_*`` types — event payload entities, each
 *   registered with the ``StreamEntry`` key below.
 */
export const AGENT_PLUGIN_TYPE_IDS = {
	agent_mcp_running_session: "db7cfuzhcj6744exem23apyoyg3ayx5rs7uvjtkjnovs37nnvm4eq",
	ws_agent_log_chain: "dfca6carpku3l77uqaz6usdmlmj2f6aacoozf5pqaokgewvuk4m4a",
	log_script_execution: "dzrgduvojmh2le7frxjs6s66bza4rgrrte2weeprqtys4vaorjq6q",
	log_invoke_result: "djhhw6diwxpj7xypu5rlomohathz7ksdn2tiocteebtcstmu4yaxq",
	log_invoke_error: "d5pv2hlapgb6bkegmdwun7rpprqyfhwwi2siitz6lta6w6pp7ohra",
	log_callback_notification: "dvguvnwsj4327k4fqv3jz46lan36bfgdygt2qsfffrvkd7g3fre7q",
	// Substitution-target entity stored by the host-side log walker
	// in place of a ``["service", short_id]`` wire-form leaf.
	// Registered against
	// :data:`agentPluginInlineLinkSubstitutionEntityMap` so refs in
	// log payloads render through the connector's live service
	// short_id rather than as bare placeholder-entity links.
	workstation_service_placeholder: "duzfdis2ybod5jav4d3ktwvnuwr2qvkc7o7lddqhe5c72qcwniqca",
} as const;

/**
 * Pre-built lookup map for the workstation ``FullPanel`` dispatch
 * key's service-side entries: maps the agent-mcp-running-session
 * type-id to :data:`AgentMcpFullPanel`. Consumers add this to
 * their ``staticServiceTypeDispatch({lookupMap: ...})`` call.
 */
export const agentPluginFullPanelServiceMap: ReadonlyMap<
	string,
	DispatchedComponent<ServiceRefStub, FullPanelProps>
> = new Map([[AGENT_PLUGIN_TYPE_IDS.agent_mcp_running_session, AgentMcpFullPanel]]);

/**
 * Pre-built lookup map for the ``StreamEntry`` dispatch key's
 * entity-side entries: maps each of the four event-payload type
 * ids to its renderer. Consumers add this to their
 * ``staticEntityTypeDispatch({lookupMap: ...})`` call when
 * registering the ``StreamEntry`` key.
 */
export const agentPluginStreamEntryEntityMap: ReadonlyMap<
	string,
	DispatchedComponent<EntityRefStub, StreamEntryProps>
> = new Map([
	[AGENT_PLUGIN_TYPE_IDS.log_script_execution, ScriptExecutionStreamEntry],
	[AGENT_PLUGIN_TYPE_IDS.log_invoke_result, InvokeResultStreamEntry],
	[AGENT_PLUGIN_TYPE_IDS.log_invoke_error, InvokeErrorStreamEntry],
	[AGENT_PLUGIN_TYPE_IDS.log_callback_notification, CallbackNotificationStreamEntry],
]);

/**
 * Pre-built lookup map for the :data:`InlineLinkWithServiceSubstitution`
 * dispatch key. Consumers register this on the entity-side of that
 * key so the substitution-target entity
 * (:class:`WorkstationServicePlaceholder`) renders through the
 * connector's live service short_id rather than as a bare
 * placeholder-entity link.
 *
 * Refs of any *other* entity type fall through to the key's
 * NoMatch fallback, which dispatches back to the standard
 * :data:`InlineLink` key — preserving the normal link rendering
 * for everything except the substitution target.
 */
export const agentPluginInlineLinkSubstitutionEntityMap: ReadonlyMap<
	string,
	DispatchedComponent<EntityRefStub, LinkProps>
> = new Map([
	[AGENT_PLUGIN_TYPE_IDS.workstation_service_placeholder, WorkstationServicePlaceholderInlineLink],
]);
