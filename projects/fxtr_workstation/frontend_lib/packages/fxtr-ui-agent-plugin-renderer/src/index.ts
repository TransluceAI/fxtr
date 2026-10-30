/**
 * Public surface of ``@fxtr-ui/agent-plugin-renderer``.
 *
 * Exports the ``FullPanel`` renderer for the
 * ``agent_mcp_running_session`` service type + four
 * ``StreamEntry`` renderers (one per event-payload entity type),
 * plus pre-built lookup maps the consumer's dispatch registry
 * can compose directly.
 *
 * Typical consumer flow (in a demo's ``dispatch_registry.tsx``):
 *
 * ```ts
 * import {
 *   AGENT_PLUGIN_TYPE_IDS,
 *   agentPluginFullPanelServiceMap,
 *   agentPluginStreamEntryEntityMap,
 * } from "@fxtr-ui/agent-plugin-renderer";
 *
 * createDispatchRegistry({
 *   entityEntries: [
 *     entityEntry(StreamEntry, staticEntityTypeDispatch({
 *       lookupMap: agentPluginStreamEntryEntityMap,
 *     })),
 *   ],
 *   serviceEntries: [
 *     serviceEntry(FullPanel, staticServiceTypeDispatch({
 *       lookupMap: agentPluginFullPanelServiceMap,
 *     })),
 *   ],
 * });
 * ```
 */

// Protocol + data shapes (for advanced consumers who want to
// type their own service refs / read loaded payloads directly).
export type { AgentMcpRunningSessionProto } from "./proto.js";
export {
	OP_KIND_CREATE_CALLBACK,
	OP_KIND_EXPORT_ENTITY,
	OP_KIND_EXPORT_SERVICE,
	OP_KIND_FORGET,
	OP_KIND_FORGET_CALLBACK_NOTIFICATION,
	OP_KIND_PRESENT_TO_USER,
	OP_KIND_SCHEDULE_INVOKE,
} from "./data_shapes.js";
export type {
	CallbackNotificationValue,
	ChainNodeValue,
	InvokeErrorValue,
	InvokeResultValue,
	ScriptExecutionValue,
	ScriptOpRecord,
	WorkstationServicePlaceholderValue,
} from "./data_shapes.js";

// State-machine surface (exported so future tests / variants
// can reuse the driver against a different panel chrome).
export {
	INITIAL_PANEL_STATE,
	makeAgentPanelDriver,
} from "./state_machine.js";
export type {
	ChainEntry,
	PanelPhase,
	PanelState,
} from "./state_machine.js";

// Renderers.
export { AgentMcpFullPanel } from "./panel.js";
export { CallbackNotificationStreamEntry } from "./entries/callback_notification.js";
export { InvokeErrorStreamEntry } from "./entries/invoke_error.js";
export { InvokeResultStreamEntry } from "./entries/invoke_result.js";
export { ScriptExecutionStreamEntry } from "./entries/script_execution.js";

// Inline-link substitution surface — lets consumers re-render
// :class:`WorkstationServicePlaceholder` log-payload entries as
// live service links when the connector still recognises the
// short_id (see ``inline_link_substitution.ts``'s module docstring).
export {
	InlineLinkWithServiceSubstitution,
	JsonRefTreeWithServiceSubstitution,
	WorkstationServicePlaceholderInlineLink,
} from "./inline_link_substitution.js";

// Lookup maps for registry composition.
export {
	AGENT_PLUGIN_TYPE_IDS,
	agentPluginFullPanelServiceMap,
	agentPluginInlineLinkSubstitutionEntityMap,
	agentPluginStreamEntryEntityMap,
} from "./lookup_maps.js";
