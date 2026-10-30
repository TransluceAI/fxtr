/**
 * Per-key fallback components for the workstation's dispatch
 * keys, in the shape required by the both-allowed `declareDispatchKey`
 * API: each fallback accepts `EntityRefStub | ServiceRefStub` and
 * dispatches at runtime to the matching kind-specific component
 * from `../fallbacks/`.
 *
 * The kind-specific fallback components themselves predate the
 * fallbacks-on-key refactor and still live in
 * `../fallbacks/`; the wrappers here are the only piece the
 * dispatch keys interact with directly.
 */

import { EntityRefStub, type ServiceRefStub } from "@fxtr-ui/core";
import { EntityBlockLinkFallback } from "../fallbacks/EntityBlockLinkFallback.js";
import { EntityFullPanelBuiltinFallback } from "../fallbacks/EntityFullPanelBuiltinFallback.js";
import { EntityFullPanelFallback } from "../fallbacks/EntityFullPanelFallback.js";
import { EntityFullPanelLoadingFallback } from "../fallbacks/EntityFullPanelLoadingFallback.js";
import { EntityHoverPreviewFallback } from "../fallbacks/EntityHoverPreviewFallback.js";
import { EntityHoverPreviewLoadingFallback } from "../fallbacks/EntityHoverPreviewLoadingFallback.js";
import { EntityInlineLinkFallback } from "../fallbacks/EntityInlineLinkFallback.js";
import { ServiceBlockLinkFallback } from "../fallbacks/ServiceBlockLinkFallback.js";
import { ServiceFullPanelFallback } from "../fallbacks/ServiceFullPanelFallback.js";
import { ServiceFullPanelLoadingFallback } from "../fallbacks/ServiceFullPanelLoadingFallback.js";
import { ServiceHoverPreviewFallback } from "../fallbacks/ServiceHoverPreviewFallback.js";
import { ServiceHoverPreviewLoadingFallback } from "../fallbacks/ServiceHoverPreviewLoadingFallback.js";
import { ServiceInlineLinkFallback } from "../fallbacks/ServiceInlineLinkFallback.js";
import type { FullPanelProps, HoverPreviewProps, LinkProps } from "./keys.js";

type StubUnion = EntityRefStub | ServiceRefStub;

/** Loading fallback for the FullPanel key — branches on stub kind. */
export function FullPanelLoading(props: { stub: StubUnion } & FullPanelProps) {
	if (props.stub instanceof EntityRefStub) {
		return <EntityFullPanelLoadingFallback {...props} stub={props.stub} />;
	}
	return <ServiceFullPanelLoadingFallback {...props} stub={props.stub as ServiceRefStub} />;
}

/** No-match (terminal) fallback for the FullPanel key. */
export function FullPanelNoMatch(props: { stub: StubUnion } & FullPanelProps) {
	if (props.stub instanceof EntityRefStub) {
		return <EntityFullPanelFallback {...props} stub={props.stub} />;
	}
	return <ServiceFullPanelFallback {...props} stub={props.stub as ServiceRefStub} />;
}

/**
 * Builtin (terminal) fallback for the FullPanel key — rendered
 * when the dispatcher's state machine short-circuits because the
 * stub's shortId carries an unloadable tag (`c` / `!` / `.`).
 *
 * `Builtin` is an entity-only slot in
 * `@fxtr-ui/react-dispatch`'s fallback bundle (services have no
 * unloadable-tag convention), so we forward to the entity-side
 * fallback directly.
 */
export function FullPanelBuiltin(props: { stub: EntityRefStub } & FullPanelProps) {
	return <EntityFullPanelBuiltinFallback {...props} />;
}

/**
 * Loading/no-match fallback for the BlockLink key. The shell uses
 * the same component for both states (a link looks the same while
 * loading and when no renderer is registered) — see the registry
 * file's note on this convention.
 */
export function BlockLinkFallback(props: { stub: StubUnion } & LinkProps) {
	if (props.stub instanceof EntityRefStub) {
		return <EntityBlockLinkFallback {...props} stub={props.stub} />;
	}
	return <ServiceBlockLinkFallback {...props} stub={props.stub as ServiceRefStub} />;
}

/** Loading/no-match fallback for the InlineLink key. Same convention as BlockLink. */
export function InlineLinkFallback(props: { stub: StubUnion } & LinkProps) {
	if (props.stub instanceof EntityRefStub) {
		return <EntityInlineLinkFallback {...props} stub={props.stub} />;
	}
	return <ServiceInlineLinkFallback {...props} stub={props.stub as ServiceRefStub} />;
}

/** Loading fallback for the HoverPreview key. */
export function HoverPreviewLoading(props: { stub: StubUnion } & HoverPreviewProps) {
	if (props.stub instanceof EntityRefStub) {
		return <EntityHoverPreviewLoadingFallback {...props} stub={props.stub} />;
	}
	return <ServiceHoverPreviewLoadingFallback {...props} stub={props.stub as ServiceRefStub} />;
}

/** No-match fallback for the HoverPreview key. */
export function HoverPreviewNoMatch(props: { stub: StubUnion } & HoverPreviewProps) {
	if (props.stub instanceof EntityRefStub) {
		return <EntityHoverPreviewFallback {...props} stub={props.stub} />;
	}
	return <ServiceHoverPreviewFallback {...props} stub={props.stub as ServiceRefStub} />;
}
