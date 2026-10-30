/**
 * Builtin renderer module for the assembly-stage demo.
 *
 * Re-exports the existing static-registry components from
 * `@fxtr-ui/agent-plugin-renderer` (the agent panel + log-event
 * stream entries + the workstation-service substitution link),
 * `@fxtr-ui/workstation` (the basic-type badge link components),
 * and `@fxtr-ui/reports` (the RootReport / SubReport renderers
 * for the four shell keys + the Figure inline key) under one
 * subtype key per type-id-string.
 *
 * Don't edit this folder — it ships with the demo, and the type
 * routing in this dir's `fxtr_renderer.nix` slice rides on
 * `lib.fxtr.entities.*` (library type entities whose id-strings are
 * content-identical re-declarations of the Python
 * `system_entities.define_type(...)` constants across
 * `fxtr_workstation_mcp.{agent_log_types,agent_mcp_proto}`,
 * `fxtr_report_builder.entities`, and the workstation's
 * `!basic_type` constant — see `nix/fxtr_lib_type_ids`).
 *
 * The `default` export is a 2-level map
 * `{subtype_key: {dispatch_key_uuid: Component}}`; the frontend's
 * dynamic dispatcher looks up the per-call resolution by
 * `module[subtype_key][dispatch_key.uuid]`. Each subtype below
 * corresponds to one `subtypes.<key>` entry in `fxtr_renderer.nix`.
 */

import {
	AgentMcpFullPanel,
	CallbackNotificationStreamEntry,
	InvokeErrorStreamEntry,
	InvokeResultStreamEntry,
	InlineLinkWithServiceSubstitution,
	ScriptExecutionStreamEntry,
	WorkstationServicePlaceholderInlineLink,
} from "@fxtr-ui/agent-plugin-renderer";
import { channelReaderRenderers } from "@fxtr-ui/rd-types-channel";
import { StreamEntry } from "@fxtr-ui/rd-types-stream";
import {
	Figure,
	RootReportBlockLink,
	RootReportFullPanel,
	RootReportHoverPreview,
	RootReportInlineLink,
	SubReportBlockLink,
	SubReportFigure,
	SubReportFullPanel,
	SubReportHoverPreview,
	SubReportInlineLink,
} from "@fxtr-ui/reports";
import { BasicTypeBlockLink, BasicTypeInlineLink } from "@fxtr-ui/workstation";
import {
	BlockLink,
	FullPanel,
	HoverPreview,
	InlineLink,
} from "@fxtr-ui/workstation-dispatch";

export default {
	agent_mcp_running_session: {
		[FullPanel.uuid]: AgentMcpFullPanel,
	},
	log_script_execution: {
		[StreamEntry.uuid]: ScriptExecutionStreamEntry,
	},
	log_invoke_result: {
		[StreamEntry.uuid]: InvokeResultStreamEntry,
	},
	log_invoke_error: {
		[StreamEntry.uuid]: InvokeErrorStreamEntry,
	},
	log_callback_notification: {
		[StreamEntry.uuid]: CallbackNotificationStreamEntry,
	},
	workstation_service_placeholder: {
		[InlineLinkWithServiceSubstitution.uuid]: WorkstationServicePlaceholderInlineLink,
	},
	// Data entities with `$fxtr.type = !basic_type` flow through
	// the normal dispatch path (the `!`-prefix short-circuit only
	// fires for the type-entity's own ref, not for entities tagged
	// *as* that type). Registering "!basic_type" as a `for_type_id`
	// routes those entities through the BasicType link components.
	basic_type: {
		[BlockLink.uuid]: BasicTypeBlockLink,
		[InlineLink.uuid]: BasicTypeInlineLink,
	},
	root_report: {
		[FullPanel.uuid]: RootReportFullPanel,
		[BlockLink.uuid]: RootReportBlockLink,
		[InlineLink.uuid]: RootReportInlineLink,
		[HoverPreview.uuid]: RootReportHoverPreview,
	},
	sub_report: {
		[FullPanel.uuid]: SubReportFullPanel,
		[BlockLink.uuid]: SubReportBlockLink,
		[InlineLink.uuid]: SubReportInlineLink,
		[HoverPreview.uuid]: SubReportHoverPreview,
		[Figure.uuid]: SubReportFigure,
	},
	// The fixed channel-reader service type (fxtr2 output channels);
	// covers the four shell keys. The reader renderers fetch get_tag()
	// to learn the channel's tag and re-dispatch on the tag's entity
	// type (ChannelContentCard / ChannelStatusPreview).
	channel_reader: channelReaderRenderers,
};
