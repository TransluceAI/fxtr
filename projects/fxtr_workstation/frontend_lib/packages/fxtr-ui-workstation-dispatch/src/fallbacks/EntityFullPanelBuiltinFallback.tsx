/**
 * Default `FullPanel` body for an entity stub the workstation
 * can't load — context-provenance (`c`), builtin (`!`), or
 * anonymous (`.`) tags. The static entity dispatcher routes
 * these through the key's `Builtin` fallback (see
 * `isBuiltinEntityShortId` in `@fxtr-ui/react-dispatch`), and
 * this component is what the workstation supplies for that
 * slot on the `FullPanel` key.
 *
 * Behavior: no load (would fail server-side); make a metadata
 * call via `ctx.allocateGetEntityMetadata` and surface the
 * populated fields via the shared `MetadataFields` component.
 * For c-tag nodes the user sees the `context_data` row as a
 * clickable inline link — the main user-visible payoff.
 *
 * Per-mount slot state rides in the dispatched `ownStorage`
 * atom (`{ metadata: OpResultStub<EntityMetadata> | null }`),
 * lifted via `atomWithDefault`. Wire-level slot dedup makes
 * re-mounts cheap.
 */

import type {
	EntityMetadata,
	EntityRefStub,
	FxtrSessionAtom,
	OpResultStub,
	ResultStatus,
} from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import { atomWithDefault } from "@fxtr-ui/state-util";
import { useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, type ReactNode, useEffect, useMemo } from "react";
import type { FullPanelProps } from "../dispatch/keys.js";
import { MetadataFields } from "../metadata/MetadataFields.js";

interface BuiltinStorage {
	readonly metadata: OpResultStub<EntityMetadata> | null;
}

const INITIAL_STORAGE: BuiltinStorage = { metadata: null };

export function EntityFullPanelBuiltinFallback(props: { stub: EntityRefStub } & FullPanelProps) {
	const { stub, ownStorage } = props;

	const typedStorage = useMemo(
		// `ownStorage` is typed `FxtrSessionAtom<unknown>` (the
		// dispatcher hands renderers a typeless slot). We own the
		// slot's contents, so cast to its concrete shape before
		// lifting through `atomWithDefault`.
		() =>
			atomWithDefault<BuiltinStorage>(
				ownStorage as FxtrSessionAtom<BuiltinStorage | null>,
				INITIAL_STORAGE,
			),
		[ownStorage],
	);
	const state = useAtomValue(typedStorage);
	const setState = useSetAtom(typedStorage);

	useEffect(() => {
		setState((prev, ctx) => {
			if (prev.metadata !== null) return prev;
			return { metadata: ctx.allocateGetEntityMetadata(stub) };
		});
	}, [stub, setState]);

	return (
		<div className="fw-panel__body" style={body}>
			<span style={heading}>{kindLabel(stub.shortId)}</span>
			<span style={shortIdLine}>{stub.shortId}</span>
			<span style={note}>
				system-defined node — can't be loaded directly; showing metadata instead
			</span>
			<div style={sectionStyle}>
				<div style={sectionHeaderStyle}>Metadata</div>
				<div style={sectionBodyStyle}>
					{state.metadata !== null ? (
						<MetadataGate stub={state.metadata} />
					) : (
						<Muted>allocating…</Muted>
					)}
				</div>
			</div>
		</div>
	);
}

function kindLabel(shortId: string): string {
	if (shortId.startsWith("c")) return "context provenance node";
	if (shortId.startsWith("u")) return "untracked provenance node";
	if (shortId.startsWith("!")) return "builtin";
	if (shortId.startsWith(".")) return "anonymous node";
	return "system node";
}

function MetadataGate({ stub }: { readonly stub: OpResultStub<EntityMetadata> }) {
	const status = useResult(stub);
	return (
		<ResultGate status={status}>{(metadata) => <MetadataFields metadata={metadata} />}</ResultGate>
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
