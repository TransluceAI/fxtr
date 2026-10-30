/**
 * Default `FullPanel` body for an entity stub when no consumer
 * renderer is registered for its type (or while the dispatcher is
 * still resolving). Shows the short id + "no renderer configured"
 * message, then loads the entity's raw value and renders it as a
 * `<JsonRefTree>` so the user can still inspect what's there.
 *
 * Per-mount slot state rides in the dispatched `ownStorage` atom
 * (`{ load: OpResultStub<unknown> | null }`), lifted via
 * `atomWithDefault`. Wire-level slot dedup means re-mounts cheap.
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub, ResultStatus } from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import { atomWithDefault } from "@fxtr-ui/state-util";
import { useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, type ReactNode, useEffect, useMemo } from "react";
import type { FullPanelProps } from "../dispatch/keys.js";
import { JsonRefTree } from "../metadata/JsonRefTree.js";

interface FallbackStorage {
	readonly load: OpResultStub<unknown> | null;
}

const INITIAL_STORAGE: FallbackStorage = { load: null };

export function EntityFullPanelFallback(props: { stub: EntityRefStub } & FullPanelProps) {
	const { stub, ownStorage } = props;

	const typedStorage = useMemo(
		// `ownStorage` is typed `FxtrSessionAtom<unknown>` (the
		// dispatcher hands renderers a typeless slot). The renderer
		// owns the slot's contents, so we cast to its concrete
		// shape before lifting through `atomWithDefault`.
		() =>
			atomWithDefault<FallbackStorage>(
				ownStorage as FxtrSessionAtom<FallbackStorage | null>,
				INITIAL_STORAGE,
			),
		[ownStorage],
	);
	const state = useAtomValue(typedStorage);
	const setState = useSetAtom(typedStorage);

	useEffect(() => {
		setState((prev, ctx) => {
			if (prev.load !== null) return prev;
			return { load: ctx.allocateLoad<unknown>(stub) };
		});
	}, [stub, setState]);

	return (
		<div className="fw-panel__body" style={body}>
			<span style={heading}>entity</span>
			<span style={shortIdLine}>{stub.shortId}</span>
			<span style={note}>no renderer configured for this entity type</span>
			<div style={sectionStyle}>
				<div style={sectionHeaderStyle}>Raw data</div>
				<div style={sectionBodyStyle}>
					{state.load !== null ? <LoadGate stub={state.load} /> : <Muted>allocating…</Muted>}
				</div>
			</div>
		</div>
	);
}

function LoadGate({ stub }: { readonly stub: OpResultStub<unknown> }) {
	const status = useResult(stub);
	return (
		<ResultGate status={status}>
			{(value) => <JsonRefTree value={value} initiallyExpanded />}
		</ResultGate>
	);
}

function ResultGate<T>({
	status,
	children,
}: {
	readonly status: ResultStatus<T>;
	readonly children: (value: T) => ReactNode;
}) {
	if (status.status === "pending") return <Muted>pending…</Muted>;
	if (status.status === "errored") return <span style={errorStyle}>error: {status.message}</span>;
	return <>{children(status.value)}</>;
}

function Muted({ children }: { readonly children: ReactNode }) {
	return <span style={mutedStyle}>{children}</span>;
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
const sectionStyle: CSSProperties = {
	marginTop: "var(--sp-3, 12px)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2)",
};
const sectionHeaderStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-muted)",
	fontFamily: "var(--font-prose)",
};
const sectionBodyStyle: CSSProperties = {
	padding: "var(--sp-3, 12px)",
	background: "var(--surface-sunken)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
};
const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontSize: "var(--text-body-sm)",
	fontStyle: "italic",
};
const errorStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontSize: "var(--text-body-sm)",
};
