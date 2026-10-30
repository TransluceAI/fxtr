/**
 * Minimal `FullPanel` body rendered while the dispatcher is still
 * resolving the entity's type (before it reaches the
 * `resolved | errored` terminal). Distinct from
 * `EntityFullPanelFallback`, which is the no-match terminal:
 * the loading body deliberately does NOT touch `ownStorage`, so
 * the real renderer can mount with a clean slate once the
 * dispatcher resolves. (Writing the no-match body's allocation
 * state during the loading phase would clobber the eventual
 * matched renderer's `ownStorage` shape.)
 *
 * Reads the static-helper's progress message via
 * `useDispatchProgressMessage` so the user sees the current
 * phase ("loading entity…" / "resolving renderer…").
 */

import type { EntityRefStub } from "@fxtr-ui/core";
import { useDispatchProgressMessage } from "@fxtr-ui/react-dispatch";
import type { CSSProperties } from "react";
import type { FullPanelProps } from "../dispatch/keys.js";

export function EntityFullPanelLoadingFallback(props: { stub: EntityRefStub } & FullPanelProps) {
	const message = useDispatchProgressMessage();
	return (
		<div className="fw-panel__body" style={body}>
			<span style={heading}>entity</span>
			<span style={shortIdLine}>{props.stub.shortId}</span>
			<span style={note}>{message || "loading…"}</span>
		</div>
	);
}

const body: CSSProperties = {
	padding: "var(--sp-3, 12px)",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2)",
};
const heading: CSSProperties = {
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body)",
	fontWeight: 600,
	color: "var(--text-primary)",
};
const shortIdLine: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
};
const note: CSSProperties = {
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-muted)",
};
