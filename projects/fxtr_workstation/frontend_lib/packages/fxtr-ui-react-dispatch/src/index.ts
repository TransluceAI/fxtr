// Public API of @fxtr-ui/react-dispatch.
//
// Type-driven dispatch over `EntityRefStub` / `ServiceRefStub`
// values. Consumers declare a `DispatchKey` (a "rendering slot"
// — e.g. Card, DetailView, Inline), register components for it
// per stub-type-id, and render via `<RenderDispatch stub={...}
// as={key} {...props}/>`. The provider mounts a state-machine
// effect per `(stub, key)` pair that resolves the right
// component and re-resolves reactively as the underlying
// session state changes.
//
// Depends on `@fxtr-ui/core` (for `EntityRefStub` /
// `ServiceRefStub` / `FxtrSessionAtom`), `@fxtr-ui/state-util`
// (for `subAtom` / `atomWithDefault`), `@fxtr-ui/react` (for
// the `SessionContext` re-used to hide the parent session
// inside the dispatch boundary), `jotai` + `jotai-effect`, and
// `react` as a peer dependency (>=18).

export {
	type AnyDispatchEntry,
	type DispatchEntry,
	type Dispatcher,
	type DispatcherStatus,
	DispatcherClosedError,
} from "./dispatcher";

export {
	type AnyDispatchKey,
	type DeclareDispatchKeyOptions,
	type DeclareEntityDispatchKeyOptions,
	type DeclareServiceDispatchKeyOptions,
	type DispatchKey,
	type ExtractDispatchAllowed,
	type ExtractDispatchProps,
	type NoProps,
	type RefKind,
	declareDispatchKey,
	declareEntityDispatchKey,
	declareServiceDispatchKey,
} from "./keys";

export type {
	AnyKeyFallbacks,
	EntityKeyFallbacks,
	KeyFallbacksFor,
	ServiceKeyFallbacks,
} from "./fallbacks";

export {
	type KeyFallbackOverride,
	overrideKeyFallbacks,
} from "./overrides";

export type {
	ComponentAtom,
	DispatchEffectFactory,
	DispatchKeyRegistryEntry,
	DispatchedComponent,
	EntityDispatchEntry,
	ErrorFallbackComponent,
	ProgressMessageAtom,
	ServiceDispatchEntry,
	StubFallbacks,
} from "./registry_types";

export type { DispatchRegistry } from "./registry";

export {
	type CreateDispatchRegistryInput,
	DuplicateDispatchKeyError,
	type EntityDispatchRegistryForKey,
	type ServiceDispatchRegistryForKey,
	createDispatchRegistry,
	entityEntry,
	serviceEntry,
} from "./static_registry";

export {
	type StaticEntityDispatchState,
	type StaticEntityTypeDispatchOptions,
	staticEntityTypeDispatch,
} from "./static_entity_registry";

export {
	type StaticServiceDispatchState,
	type StaticServiceTypeDispatchOptions,
	staticServiceTypeDispatch,
} from "./static_service_registry";

export {
	type CreateStaticDispatcherOptions,
	type DispatcherUserState,
	EMPTY_DISPATCHER_USER_STATE,
	createStaticDispatcher,
} from "./static_dispatcher";

export {
	type CreateBaseEntryOptions,
	type EntryUserStateSlot,
	createBaseEntry,
} from "./base_entry";

export {
	DispatcherContext,
	DispatcherProvider,
	type DispatcherProviderProps,
	useDispatcher,
} from "./dispatcher_provider";

export {
	RenderDispatch,
	type RenderDispatchProps,
} from "./render_dispatch";

export {
	useDispatchProgressMessage,
	useDispatchProgressState,
	useDispatchSharedAtom,
} from "./hooks";

export {
	type CompiledModuleStatus,
	type CompiledRendererModule,
	type CreateDynamicDispatcherOptions,
	type DynamicDispatcherUserState,
	type DynamicEntityDispatchState,
	type DynamicServiceDispatchState,
	EMPTY_DYNAMIC_DISPATCHER_USER_STATE,
	type RendererEntityLoadedShape,
	type RendererResolution,
	createDynamicDispatcher,
} from "./dynamic_dispatcher";

// Staleness wrap helper used internally by the dynamic dispatcher.
// `makeEntryWrapper` is exposed mainly for tests that want to
// exercise the wrap behaviour directly without spinning up a full
// dispatcher. `getStaleAwareInner` is the inverse — recovers the
// inner component identity from a wrap. Production code should
// treat wrapped components as opaque `DispatchedComponent`s.
export { type EntryWrapper, getStaleAwareInner, makeEntryWrapper } from "./staleness_wrapper";

// Debug-view component types — concrete dispatchers expose a
// `debugView()` method that returns a pre-bound element. These
// types are exported in case consumers want to compose the debug
// view themselves (e.g. inside a larger debug panel).
export type { StaticDispatcherDebugViewProps } from "./static_debug_view";
export { StaticDispatcherDebugView } from "./static_debug_view";
export type { DynamicDispatcherDebugViewProps } from "./dynamic_debug_view";
export { DynamicDispatcherDebugView } from "./dynamic_debug_view";
