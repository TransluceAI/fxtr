/**
 * TS-side wire-decoded shapes for the agent log's typed entities.
 *
 * Each interface mirrors the field layout of the matching Python
 * :class:`DataclassEntity` in
 * :mod:`fxtr_workstation_mcp.agent_log_types`, with the
 * connector's :func:`unescapeWire` lifting wire-encoded fields to
 * their TS counterparts:
 *
 * - ``EntityRef[T]`` → :class:`EntityRefStub` (target type erased
 *   at the wire boundary; consumers re-typed via casts where
 *   needed).
 * - ``FrozenFxtrCbor`` → :data:`ResultValue` (a nested structure
 *   of primitives + refs; the walker's substitutions surface as
 *   :class:`EntityRefStub` leaves the renderer can hand to
 *   ``<RefLink>`` / ``<JsonRefTree>``).
 *
 * :class:`ScriptOpRecord` is the discriminated subpart inside
 * :attr:`ScriptExecutionValue.ops`. The Python side stores it as a
 * flat dataclass with all-nullable per-kind fields + a ``kind``
 * discriminator; this TS module exposes it as a discriminated
 * union so consumer code narrows cleanly on ``kind`` and only sees
 * the fields meaningful to each op kind. (The decoded wire dict
 * still carries the other fields as ``null``; the union just
 * surfaces what's relevant.)
 */

import type { EntityRefStub, ResultValue } from "@fxtr-ui/core";

/**
 * Loaded shape of :class:`WsAgentLogEventChain`. The ``event`` ref
 * is loaded separately via the StreamEntry dispatch; ``prev`` is
 * walked back to fill gaps in the panel's entry list.
 */
export interface ChainNodeValue {
	readonly prev: EntityRefStub | null;
	readonly event: EntityRefStub;
	readonly timestamp: number;
}

/**
 * Loaded shape of :class:`ScriptExecutionLogEvent`. Three terminal
 * shapes — see the Python class docstring; ``error`` /
 * ``rejected`` distinguish, ``ops`` is empty on either error
 * path.
 */
export interface ScriptExecutionValue {
	readonly script_src: string;
	readonly preamble: EntityRefStub;
	readonly stdout: string;
	readonly stderr: string;
	readonly error: string | null;
	readonly rejected: string | null;
	readonly ops: ReadonlyArray<ScriptOpRecord>;
	readonly committed_seq: number | null;
}

/** Loaded shape of :class:`InvokeResultLogEvent`. */
export interface InvokeResultValue {
	readonly slot_key: string;
	readonly slot_n: number;
	readonly seq: number;
	readonly payload: ResultValue;
}

/** Loaded shape of :class:`InvokeErrorLogEvent`. */
export interface InvokeErrorValue {
	readonly slot_key: string;
	readonly slot_n: number;
	readonly seq: number;
	readonly message: string;
}

/** Loaded shape of :class:`CallbackNotificationLogEvent`. */
export interface CallbackNotificationValue {
	readonly slot_key: string;
	readonly slot_n: number;
	readonly seq: number;
	readonly payload: ResultValue;
}

/**
 * Loaded shape of :class:`WorkstationServicePlaceholder`.
 *
 * Stored by the host-side log walker as the substitute for a
 * ``["service", short_id]`` wire-form leaf. The renderer
 * registered against this type loads the entity, reads
 * :attr:`short_id`, and tries to resolve it via
 * :class:`RefStubResolver` so a connector-live service short_id
 * is re-rendered as a regular ``<RefLink>`` rather than the bare
 * placeholder entity link.
 *
 * :attr:`service_type_id` / :attr:`service_ctx` are the durable
 * identity captured at substitution time (workstation short_ids
 * are not stable across workstation restarts). The substitution
 * renderer doesn't consume them today — it relies on the
 * connector's live ``classifyShortId`` lookup — but they're part
 * of the wire shape and surface here for completeness.
 */
export interface WorkstationServicePlaceholderValue {
	readonly short_id: string;
	readonly service_type_id: EntityRefStub;
	readonly service_ctx: EntityRefStub;
}

/**
 * ``ScriptOpRecord.kind`` values — mirrors the ``OP_KIND_*``
 * constants in :mod:`fxtr_workstation_mcp.agent_log_types`. The
 * canonical place renderer code should reference instead of typing
 * string literals.
 */
export const OP_KIND_EXPORT_ENTITY = "export_entity";
export const OP_KIND_EXPORT_SERVICE = "export_service";
export const OP_KIND_FORGET = "forget";
export const OP_KIND_SCHEDULE_INVOKE = "schedule_invoke";
export const OP_KIND_CREATE_CALLBACK = "create_callback";
export const OP_KIND_FORGET_CALLBACK_NOTIFICATION = "forget_callback_notification";
export const OP_KIND_PRESENT_TO_USER = "present_to_user";

/**
 * Loaded shape of one :class:`ScriptOpRecord` inside
 * :attr:`ScriptExecutionValue.ops`. Discriminated by ``kind``;
 * each variant carries only the fields meaningful for its kind.
 * The wire-decoded dict still contains the other fields as
 * ``null`` (Python's dataclass-default-None layout), but consumer
 * code narrows on ``kind`` and reads only the per-kind fields.
 */
export type ScriptOpRecord =
	| {
			readonly kind: typeof OP_KIND_EXPORT_ENTITY;
			readonly slot_key: string;
			readonly entity: EntityRefStub;
	  }
	| {
			readonly kind: typeof OP_KIND_EXPORT_SERVICE;
			readonly slot_key: string;
			readonly service: EntityRefStub;
	  }
	| {
			readonly kind: typeof OP_KIND_FORGET;
			readonly slot_key: string;
	  }
	| {
			readonly kind: typeof OP_KIND_SCHEDULE_INVOKE;
			readonly slot_key: string;
			readonly service_short_id: string;
			readonly method_name: string;
			readonly args: ResultValue;
			readonly kwargs: ResultValue;
	  }
	| {
			readonly kind: typeof OP_KIND_CREATE_CALLBACK;
			readonly slot_key: string;
	  }
	| {
			readonly kind: typeof OP_KIND_FORGET_CALLBACK_NOTIFICATION;
			readonly slot_key: string;
			readonly notif_index: number;
	  }
	| {
			readonly kind: typeof OP_KIND_PRESENT_TO_USER;
			readonly payload: ResultValue;
	  };
