// Public API of @fxtr-ui/connector-http.
//
// A stateless, CDN-friendly connector against a fxtr HTTP/ws backend
// (the `@fxtr-ui/core` `Connector` + `Session` surfaces, implemented
// over plain request→response endpoints plus transient per-ws-connection
// routing — no server-side session state). Builds on the shared
// list-escape codec in `@fxtr-ui/core` (resolved mode). See
// `projects/fxtr_workstation/plans/phase0/part7a_stateless_ws_server/`.

export {
	type ServiceDescriptor,
	type ServiceDescriptorKind,
	ServiceDescriptorError,
	canonicalizeDescriptor,
	descriptorFromWire,
	descriptorToWire,
} from "./descriptor";

export {
	type SerializedShortIdTable,
	type ShortIdEntry,
	ShortIdTable,
} from "./shortid_table";

export { type CborDecodeOptions, cborToResultValue } from "./entity_decode";

export { type FetchFn, type PrecompEntry, EntityFetcher, EntityLoadError } from "./entity_data";

export {
	type HttpConnectorOptions,
	type HttpEntityRecord,
	type HttpServiceRecord,
	type TransientContext,
	HttpConnector,
} from "./connector";

export { type ReactiveList, type ReactiveListStatus, DiscoveryList } from "./resource_list";

export {
	type SerializedFill,
	type SerializedInbox,
	type SerializedSession,
	type SessionRegistryEntry,
	SESSION_BLOB_VERSION,
} from "./persistence";

export { type SessionConnector, HttpSession } from "./session";

export { MissingServiceTypeError, buildServiceMetadata } from "./metadata";

export {
	type DropHandler,
	type NotifyHandler,
	type WebSocketCtor,
	type WebSocketLike,
	type WsInvokeRequest,
	type WsPoolOptions,
	WsInvokeError,
	WsPool,
} from "./ws_pool";

export {
	type FillRecord,
	HttpCallbackHandleStub,
	HttpCallbackInboxStub,
	HttpEntityRefStub,
	HttpOpResultStub,
	HttpServiceRefStub,
	type HttpStubSession,
	type InboxEntry,
	type InboxLog,
	type InboxSubscriptionState,
} from "./stubs";
