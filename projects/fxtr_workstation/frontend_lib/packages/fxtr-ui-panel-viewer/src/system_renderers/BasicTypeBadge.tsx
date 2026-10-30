/**
 * Dispatched renderers for entities of type `FxtrBasicType`
 * (the system-defined entity that backs every user-defined
 * type label — see `fxtr2.data.system_entities.FxtrBasicType`).
 * Both renderers display the ref as a colored badge reading
 * `type {type_name}`, distinguishing "this ref points to a
 * type definition" from "this ref points to data."
 *
 * Registered on the `BlockLink` and `InlineLink` keys at
 * id_string `BASIC_TYPE_ID_STRING` (`"!basic_type"`). The
 * static entity dispatcher's load → metadata → type-id lookup
 * matches that id_string and routes to these renderers.
 *
 * Components do their own load (`useDispatchSharedAtom` +
 * `allocateLoad`) since the static dispatcher drops its
 * load slot when transitioning to terminal `resolved` — same
 * pattern as other entity-side renderers in this codebase.
 */

import type { EntityRefStub, OpResultStub } from "@fxtr-ui/core";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { type LoadResult, makeLoadResultAtom } from "@fxtr-ui/state-util";
import type { LinkProps } from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { type CSSProperties, useMemo } from "react";

/**
 * id_string of the `FxtrBasicType` system entity. Static entity
 * dispatchers register renderers under this key to pick up
 * "this ref is a type definition" routing.
 */
export const BASIC_TYPE_ID_STRING = "!basic_type";

/**
 * TS mirror of `FxtrBasicType`'s wire-decoded shape. mashumaro
 * encodes `UUID` fields as strings, so `namespace_uuid` arrives
 * as a plain string here. `namespace_uuid` is unused in the
 * default badge but typed for symmetry — future variants might
 * color the badge per namespace.
 */
interface FxtrBasicTypeValue {
	readonly namespace_uuid: string;
	readonly type_name: string;
}

interface BadgeProps {
	readonly stub: EntityRefStub;
	readonly variant: "block" | "inline";
	readonly linkText: string | undefined;
}

function BasicTypeBadge({ stub, variant, linkText }: BadgeProps) {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<FxtrBasicTypeValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<FxtrBasicTypeValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);

	const label = linkText ?? labelFor(result);
	const style = variant === "inline" ? inlineStyle : blockStyle;
	return <span style={style}>{label}</span>;
}

function labelFor(result: LoadResult<FxtrBasicTypeValue>): string {
	if (result.kind === "loading") return "type …";
	if (result.kind === "errored") return "type ?";
	return `type ${result.value.type_name}`;
}

export const BasicTypeBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => <BasicTypeBadge stub={stub} variant="block" linkText={linkText} />;
BasicTypeBlockLink.displayName = "BasicTypeBlockLink";

export const BasicTypeInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => <BasicTypeBadge stub={stub} variant="inline" linkText={linkText} />;
BasicTypeInlineLink.displayName = "BasicTypeInlineLink";

// ----- Styles ----------------------------------------------------

const baseStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	background: "var(--accent-soft, #e7f0ff)",
	color: "var(--accent-strong, #1f4dbf)",
	border: "1px solid var(--accent-border, #b8cdf0)",
	borderRadius: "var(--radius-badge, 999px)",
	fontFamily: "var(--font-mono)",
	whiteSpace: "nowrap",
};

const blockStyle: CSSProperties = {
	...baseStyle,
	padding: "var(--sp-1, 4px) var(--sp-3, 12px)",
	fontSize: "var(--text-body-sm)",
};

const inlineStyle: CSSProperties = {
	...baseStyle,
	padding: "1px 6px",
	fontSize: "0.9em",
};
