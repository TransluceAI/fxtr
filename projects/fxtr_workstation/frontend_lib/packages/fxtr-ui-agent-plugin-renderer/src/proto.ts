/**
 * TS-side type declaration mirroring
 * :class:`fxtr_workstation_mcp.agent_mcp_proto.AgentMcpRunningSessionProto`.
 *
 * The Python protocol exposes three methods; the frontend invokes
 * two of them directly:
 *
 * - ``get_state() -> EntityRef[WsAgentLogEventChain] | None`` — one-shot
 *   snapshot of the current chain head.
 * - ``send(payload) -> None`` — push ``payload`` into the agent's
 *   ``workstation_inbox`` callback. Lands in the chain as a
 *   ``CallbackNotificationLogEvent(slot_key="workstation_inbox", ...)``.
 *
 * The third — ``subscribe(subscriber)``, the wire-compatible fxtr2
 * ``EventSource.subscribe`` re-declaration — is **not** declared
 * here: the frontend never invokes it itself. Instead it passes the
 * agent service ref to its own inbox's ``subscribe_to`` (a
 * callback-target invoke), and the *workstation* subscribes the
 * inbox to the agent service, owning the lifecycle. The current
 * chain head arrives as the subscription's first inbox event.
 *
 * Wire-form notes:
 *
 * - ``EntityRef[WsAgentLogEventChain]`` wire-strips to a bare
 *   ``FxtrID``, which the workstation connector lifts to an
 *   :class:`EntityRefStub`. The TS return type is therefore
 *   ``EntityRefStub | null``.
 * - ``send``'s payload is typed :data:`InvokeArgValue` — the TS
 *   analog of Python's ``FxtrServiceData`` on the arg side. The
 *   demo passes strings; protocol stays open for structured
 *   payloads (dicts, lists, refs, callback handles).
 *
 * Wire-stable type-id strings for the service + the four event
 * payload entity types live in :mod:`./lookup_maps`; mirrored
 * by hand from the Python ``system_entities.define_type(...)``
 * declarations.
 *
 * Hand-mirroring the Python protocol is the current state of the
 * art; a future revision may generate this from a shared schema
 * or add a contract test.
 */

import type { EntityRefStub, InvokeArgValue, Method, ServiceProto } from "@fxtr-ui/core";

/**
 * TS-side mirror of
 * :class:`fxtr_workstation_mcp.agent_mcp_proto.AgentMcpRunningSessionProto`.
 *
 * A frontend obtains a ``ServiceRefStub<AgentMcpRunningSessionProto>``
 * via the workstation connector (typically by resolving the
 * ``agent_mcp:{session_id}`` external name) and dispatches the
 * methods via the session's ``allocateInvoke`` (chain-head
 * subscriptions go through the inbox's ``subscribe_to`` instead —
 * see the module docstring).
 */
export interface AgentMcpRunningSessionProto extends ServiceProto {
	readonly get_state: Method<readonly [], Record<string, never>, EntityRefStub | null>;
	readonly send: Method<readonly [InvokeArgValue], Record<string, never>, null>;
}
