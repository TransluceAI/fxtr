/**
 * Substitution layer for the ``["service", short_id]`` placeholder.
 *
 * Background. When the host-side log walker processes a payload
 * containing a workstation service ref, it stores a
 * :class:`WorkstationServicePlaceholder` entity (capturing
 * ``short_id`` + ``service_type_id`` + ``service_ctx``) and the log
 * entry holds an :class:`EntityRef` to that placeholder. Without
 * substitution, frontend renderers showing the payload through
 * :class:`JsonRefTree` see only the placeholder's
 * :class:`EntityRefStub` and render it as a bare ``"type
 * workstation_service_placeholder"`` link — losing the original
 * service identity.
 *
 * Solution. Wrap :class:`JsonRefTree` so refs route through a new
 * dispatch key, :data:`InlineLinkWithServiceSubstitution`, whose
 * NoMatch fallback delegates back to the standard ``<RefLink
 * as="inline">``. A single registered renderer is configured for
 * the placeholder type id — it loads the placeholder, reads
 * :attr:`short_id`, asks the active :class:`RefStubResolver` to
 * classify it, and:
 *
 * - If the connector's resource list still has the short_id bound
 *   to a service: render ``<RefLink stub=…> (snapshotted to log)``
 *   where the inner ref is the live service stub. ``<RefLink>``
 *   (rather than the bare ``<RenderDispatch>``) ensures the
 *   shell's :class:`RefLinkImpl` wraps the dispatched output with
 *   click + drag handlers — so operators get the same
 *   interactivity they'd get on a service ref anywhere else.
 * - Otherwise: render ``{short_id} (snapshotted to log, dead)``.
 *   Service short_ids are never rebound to a different kind, so
 *   a missing / non-service classification is treated uniformly
 *   as a stale reference.
 *
 * The substitution is one level deep: the inner ``<RefLink>`` uses
 * the standard :data:`InlineLink` dispatch key (not the
 * substitution key), so the rendered service ref doesn't itself
 * get re-substituted. That matches what we want — the placeholder
 * is the only entity type we replace; all other refs render
 * normally.
 */

import type { EntityRefStub, OpResultStub, ServiceRefStub } from "@fxtr-ui/core";
import {
	type DispatchKey,
	type DispatchedComponent,
	RenderDispatch,
	declareDispatchKey,
	useDispatchSharedAtom,
} from "@fxtr-ui/react-dispatch";
import {
	JsonRefTree,
	type LinkProps,
	type RefLeafRenderer,
	RefLink,
	useRefStubResolver,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { type CSSProperties, type ReactNode, useMemo } from "react";

import type { WorkstationServicePlaceholderValue } from "./data_shapes.js";
import { makeLoadResultAtom } from "./load_effect.js";

// Wire-stable UUID. Hardcoded; do not change.
const UUID_INLINE_LINK_WITH_SERVICE_SUBSTITUTION = "f1e2d3c4-b5a6-4798-8c1d-2e3f405162a7";

/**
 * NoMatch / Loading / Error fallback for the new key: delegate
 * back to the standard :data:`InlineLink` dispatch via
 * :class:`RefLink`. Any stub type that doesn't have a registered
 * substitution renderer lands here and renders exactly like a
 * normal inline link.
 */
function InlineLinkSubstitutionFallback(
	props: { stub: EntityRefStub | ServiceRefStub } & LinkProps,
) {
	const { stub, ...linkProps } = props;
	return <RefLink stub={stub} as="inline" {...linkProps} />;
}

/**
 * The new dispatch key. Same ``LinkProps`` shape as
 * :data:`InlineLink`; consumers register entity-side renderers
 * here that get a chance to substitute the rendering before the
 * standard :data:`InlineLink` path takes over.
 *
 * No Builtin slot — builtin-tag stubs (``.`` / ``!`` / ``c``) fall
 * through to NoMatch, which delegates back to ``InlineLink`` and
 * surfaces the builtin fallback there. One layer of indirection
 * but no duplicate logic.
 */
export const InlineLinkWithServiceSubstitution: DispatchKey<LinkProps> =
	declareDispatchKey<LinkProps>({
		uuid: UUID_INLINE_LINK_WITH_SERVICE_SUBSTITUTION,
		description: "InlineLinkWithServiceSubstitution",
		fallbacks: {
			Loading: InlineLinkSubstitutionFallback,
			NoMatch: InlineLinkSubstitutionFallback,
			Error: InlineLinkSubstitutionFallback,
		},
	});

/**
 * Override renderer for :class:`WorkstationServicePlaceholder`.
 *
 * Loads the placeholder, reads :attr:`short_id`, asks
 * :class:`RefStubResolver` whether it's currently classified as a
 * service, and either renders the live service ref or a "(dead)"
 * label. The label prefix ``Snapshot of`` cues operators that the
 * link points to whatever the service was when the log entry was
 * recorded, not necessarily what it is right now.
 */
export const WorkstationServicePlaceholderInlineLink: DispatchedComponent<
	EntityRefStub,
	LinkProps
> = ({ stub }) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<WorkstationServicePlaceholderValue> | null>(
		null,
	);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<WorkstationServicePlaceholderValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const resolver = useRefStubResolver();

	if (result.kind === "loading") {
		// Bare standard inline link while the placeholder load is
		// in flight — same look as any other resolving link.
		return <RefLink stub={stub} as="inline" />;
	}
	if (result.kind === "errored") {
		// Placeholder failed to decode; degrade to the standard
		// link rather than rendering a noisy error chip inline.
		return <RefLink stub={stub} as="inline" />;
	}

	const shortId = result.value.short_id;

	// Without a resolver we can't classify — render a "(dead)"
	// label since we can't produce a live ref. Outside the shell
	// (e.g. tests) this is the expected codepath.
	if (resolver === null) {
		return <DeadSnapshotLabel shortId={shortId} />;
	}

	const kind = resolver.classifyShortId(shortId);
	if (kind === "service") {
		const live = resolver.serviceRef(shortId);
		// ``<RefLink>`` (not ``<RenderDispatch>``) so the shell's
		// :class:`RefLinkImpl` wraps the rendered link with its
		// click + drag interactivity. ``<RenderDispatch>`` here
		// would skip the shell impl and render a non-interactive
		// chip.
		return (
			<>
				<RefLink stub={live} as="inline" />
				<span style={suffixStyle}> (snapshotted to log)</span>
			</>
		);
	}

	// ``null`` (connector doesn't recognise the short_id) or
	// ``"entity"`` (unexpected — service short_ids should never
	// be rebound, but treat it as dead for safety). Both cases
	// render the dead-snapshot label.
	return <DeadSnapshotLabel shortId={shortId} />;
};

function DeadSnapshotLabel({ shortId }: { shortId: string }) {
	return (
		<span style={deadStyle}>
			<span style={deadShortIdStyle}>{shortId}</span> (snapshotted to log, dead)
		</span>
	);
}

const suffixStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontStyle: "italic",
};

const deadStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontStyle: "italic",
};

const deadShortIdStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	color: "var(--text-secondary)",
};

/**
 * Wrapper around :class:`JsonRefTree` that routes ref leaves through
 * :data:`InlineLinkWithServiceSubstitution` instead of the default
 * ``<RefLink as="inline">``. Use this in place of bare
 * :class:`JsonRefTree` anywhere the rendered payload may contain
 * walker-substituted :class:`WorkstationServicePlaceholder` refs —
 * which is everywhere in this package's stream-entry renderers.
 */
export function JsonRefTreeWithServiceSubstitution(props: {
	readonly value: unknown;
	readonly initiallyExpanded?: boolean;
}): ReactNode {
	const renderRef: RefLeafRenderer = (stub) => (
		<RenderDispatch as={InlineLinkWithServiceSubstitution} stub={stub} />
	);
	return (
		<JsonRefTree
			value={props.value}
			{...(props.initiallyExpanded !== undefined
				? { initiallyExpanded: props.initiallyExpanded }
				: {})}
			renderRef={renderRef}
		/>
	);
}
