// Public API of @fxtr-ui/core.
//
// Cross-connector foundation: abstract stub classes, the structured
// value-type families (`StateValue` / `InvokeArgValue` /
// `ResultValue`), the abstract `Session` + `Connector` surfaces,
// `memoryStorage`, and the shared list-escape wire codec (`escapeWire`
// / `unescapeWire` — it operates on the abstract stubs via callbacks,
// so it's connector-agnostic). Connector-specific wire shapes and
// state walks live in the connector packages that own them (e.g.
// `@fxtr-ui/connector-workstation`).
//
// Runtime dependency on `jotai` — the abstract `Session` exposes
// jotai atoms for its lifecycle and user state, which connector
// packages implement.

export {
	CallbackHandleStub,
	CallbackInboxStub,
	EntityRefStub,
	OpResultStub,
	type ResultStatus,
	ServiceRefStub,
	type ServiceState,
} from "./stubs";

export type {
	InvokeArgValue,
	JsonValue,
	Method,
	MethodArgs,
	MethodKwargs,
	MethodNames,
	MethodReturns,
	ResultValue,
	ServiceProto,
	StateValue,
} from "./types";

export type { EntityMetadata, ServiceMetadata } from "./metadata";

export {
	type ConnectionStatus,
	type Connector,
	type FxtrSessionAtom,
	type FxtrSessionUpdateCtx,
	type FxtrUpdate,
	MutationRolledBackError,
	NotImplementedError,
	type RollbackInfo,
	type Session,
	type SessionLifecycleState,
	type UserState,
	type UserStateWriteOptions,
} from "./session";

export { memoryStorage } from "./storage";

export {
	RESERVED_TAGS,
	TAG_BROKEN_INBOX,
	TAG_BROKEN_OP_RESULT,
	TAG_BYTES,
	TAG_CALLBACK,
	TAG_ENTITY,
	TAG_INBOX,
	TAG_OP_RESULT,
	TAG_SERVICE,
	WireEscapeError,
	escapeInvokeArgs,
	escapeWire,
	unescapeWire,
	type EscapeOptions,
	type ResultUnescapeOptions,
	type StateUnescapeOptions,
	type UnescapeOptions,
	type WireValue,
} from "./escape";
