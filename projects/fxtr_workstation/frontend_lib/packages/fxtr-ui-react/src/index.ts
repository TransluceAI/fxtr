// Public API of @fxtr-ui/react.
//
// React bindings on top of @fxtr-ui/core + @fxtr-ui/state-util:
// `<FxtrSessionProvider>` (wraps a jotai Provider scoped to the
// session's store + supplies a Context carrying the session), plus
// minimal hooks for reading session state, projecting sub-atoms,
// and reading stub results.
//
// There is intentionally no `<ConnectorProvider>`. The connector's
// store and its full surface (`listSessions`, `listEntities`, etc.)
// have very different shapes per connector type, so an abstract
// React surface over them would either be useless (`{ store }` only)
// or strongly typed to one connector. Consumers wrap their own
// `<JotaiProvider store={connector.store}>` and pass the connector
// reference around via whatever mechanism fits — prop drilling, a
// consumer-owned context, a module-scoped singleton.
//
// Depends on `react` (>=18) as a peer dependency.

export {
	FxtrSessionProvider,
	type FxtrSessionProviderProps,
	SessionContext,
	useSession,
} from "./session_context";

export { useResult, useSessionRootStateAtom, useSubAtom } from "./hooks";
