/**
 * Public surface of ``@fxtr-ui/rd-types-stream``.
 *
 * One dispatch key (``StreamEntry``) plus its default fallback
 * components, for rendering ordered streams of typed entries.
 * Consumers register per-type renderers under this key in their
 * own ``createDispatchRegistry`` calls (entity-side typically; the
 * key admits service-side too).
 *
 * The package intentionally carries no React render-time
 * machinery, no stream layout component, no scroll handling —
 * just the dispatch identity + fallbacks. The owning panel
 * component (e.g. ``AgentMcpFullPanel`` in
 * ``@fxtr-ui/agent-plugin-renderer``) drives the actual list
 * rendering + iteration; each entry's body is dispatched through
 * this key.
 */

export { StreamEntry, type StreamEntryProps } from "./keys.js";
export {
	StreamEntryErrorFallback,
	StreamEntryLoadingFallback,
	StreamEntryNoMatchFallback,
} from "./fallbacks.js";
