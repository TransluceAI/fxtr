// Public API of @fxtr-ui/connector-workstation.
//
// WebSocket connector against the workstation's `/client` endpoint:
// wire protocol type unions, state walks used by the session reducer,
// and the wire-facing `WorkstationServerConnection` transport. Built
// on top of `@fxtr-ui/core`'s stub classes, abstract `Session`
// interface, and the shared list-escape wire codec.

export {
	ConnectionClosedError,
	ConnectionLostError,
	ProtocolError,
	WorkstationServerConnection,
	type ClientRequestBody,
	type ConnectionStatus,
	type ResponseFor,
	type SubscriptionCloseReason,
	type SubscriptionFrame,
	type SubscriptionHandle,
	type SubscriptionHandler,
	type SubscriptionSpec,
} from "./connection";

// The list-escape wire codec now lives in `@fxtr-ui/core` (shared with
// other connectors). Re-exported here for backward compatibility, so
// existing `@fxtr-ui/connector-workstation` consumers keep working.
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
} from "@fxtr-ui/core";

export type {
	AckResponse,
	CallbackEvent,
	CallbackSubscriptionEvent,
	ClientRequest,
	CloseCallbackOp,
	CloseOp,
	CreateCallbackOp,
	CreateSessionRequest,
	EntityMetadataErrorEvent,
	EntityMetadataResultEvent,
	EntityRecord,
	ErrorResponse,
	GetEntityMetadataOp,
	GetServiceMetadataOp,
	InvokeErrorEvent,
	InvokeOp,
	InvokeResultEvent,
	JsonPatchWireOp,
	LoadEntityErrorResponse,
	LoadEntityOp,
	LoadEntityRequest,
	LoadEntityResponse,
	LoadEntitySilentOp,
	LoadErrorEvent,
	LoadResultEvent,
	LoadResultSilentEvent,
	MutateSessionRequest,
	MutationCommittedResponse,
	MutationOp,
	MutationRejectReason,
	MutationRejectedResponse,
	NonStateSessionEvent,
	OpenInvokeInfo,
	PreserveEventsOp,
	ResourceChangesEvent,
	ResourceKind,
	ResourceSnapshotEvent,
	RpcConnectionRecord,
	ServerFrame,
	ServiceMetadataErrorEvent,
	ServiceMetadataResultEvent,
	ServiceRecord,
	SessionCreatedResponse,
	SessionDebugSnapshotEvent,
	SessionEvent,
	SessionEventFrame,
	SessionRecord,
	SessionSnapshotEvent,
	StateUpdateEvent,
	SubscribeRequest,
	SubscribeSessionDebugRequest,
	SubscribeSessionRequest,
	TrackEntityOp,
	TrackServiceOp,
	UnpreserveEventsOp,
	UnsubscribeRequest,
	UntrackEntityOp,
	UntrackServiceOp,
} from "./protocol";

export { RESOURCE_KEY_FIELD } from "./protocol";

export {
	collectStubsInState,
	diffStateToPatch,
	type CollectedStubs,
	type JsonPatchOp,
} from "./state";

export {
	ConnectorClosedError,
	LoadEntityError,
	WorkstationConnector,
	type ListResourceOptions,
	type WorkstationConnectorOptions,
} from "./connector";

export type { ReactiveList, ReactiveListStatus } from "./resource_list";

export {
	WorkstationSession,
	type WorkstationSessionOptions,
} from "./session";

export {
	type FillRecord,
	type InboxEntry,
	type InboxLog,
	type InboxSubscriptionState,
	WorkstationCallbackHandleStub,
	WorkstationCallbackInboxStub,
	WorkstationEntityRefStub,
	WorkstationOpResultStub,
	WorkstationServiceRefStub,
	type WorkstationStubSession,
} from "./stubs";
