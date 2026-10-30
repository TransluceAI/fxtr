/**
 * Hardcoded metadata view for a service ref. Rendered by
 * `PanelWrapper` when the active panel-view entry has
 * `display: "metadata"` (in place of the dispatched FullPanel
 * renderer).
 *
 * Three sections, top-to-bottom:
 *
 *   1. **Metadata** — fields from `allocateGetServiceMetadata`.
 *      Currently `type_id_string` is the only field; rendered as
 *      monospace.
 *
 *   2. **`$fxtr.ctx`** — result of invoking the service's
 *      `"$fxtr.ctx"` pseudo-method. Rendered via JsonRefTree.
 *
 *   3. **`$fxtr.type`** — same, for `"$fxtr.type"`.
 *
 * Per-panel state (the three `OpResultStub`s) lives in the panel-
 * session's `userState.metadataStorage` slot, lifted via
 * `atomWithDefault`. Note that invokes have NO dedup — to keep
 * remounts cheap, the storage caches the slot stubs across
 * re-renders so we don't burn a fresh slot every time the panel
 * re-renders.
 */

import type {
	FxtrSessionAtom,
	OpResultStub,
	ResultStatus,
	ServiceMetadata,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import { atomWithDefault } from "@fxtr-ui/state-util";
import { useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, type ReactNode, useEffect, useMemo } from "react";
import { JsonRefTree } from "./JsonRefTree.js";

export interface ServiceMetadataViewProps {
	readonly stub: ServiceRefStub;
	readonly storage: FxtrSessionAtom<unknown>;
}

interface ServiceMetadataStorage {
	readonly metadata: OpResultStub<ServiceMetadata> | null;
	readonly ctxInvoke: OpResultStub<unknown> | null;
	readonly typeInvoke: OpResultStub<unknown> | null;
}

const INITIAL_STORAGE: ServiceMetadataStorage = {
	metadata: null,
	ctxInvoke: null,
	typeInvoke: null,
};

export function ServiceMetadataView({ stub, storage }: ServiceMetadataViewProps) {
	return (
		<div className="fw-metadata-view" style={rootStyle}>
			<h2 style={headerStyle}>Service metadata · {stub.shortId}</h2>
			<ServiceMetadataBody stub={stub} storage={storage} />
		</div>
	);
}

/**
 * Inner body — three sections, no outer heading or padding. Used
 * by `ServiceMetadataView` (for metadata-mode panels) and by
 * `ServiceFullPanelFallback` (so the fallback can prepend a
 * "no renderer configured" note without duplicating the heading).
 */
export function ServiceMetadataBody({ stub, storage }: ServiceMetadataViewProps) {
	const typedStorage = useMemo(
		// `storage` is typed `FxtrSessionAtom<unknown>` (the
		// metadata-mode renderer's caller hands a typeless slot
		// from `RefPanelChainState.storages`). We own the slot's
		// contents here, so cast to the concrete shape before
		// lifting through `atomWithDefault`.
		() =>
			atomWithDefault<ServiceMetadataStorage>(
				storage as FxtrSessionAtom<ServiceMetadataStorage | null>,
				INITIAL_STORAGE,
			),
		[storage],
	);
	const state = useAtomValue(typedStorage);
	const setState = useSetAtom(typedStorage);

	// Allocate all three slots on first mount. `metadata` dedups at
	// the wire layer; the two invokes do not, so the `prev`-check is
	// load-bearing for them — every fresh allocation would mint a
	// new slot and re-trigger the invoke server-side.
	useEffect(() => {
		setState((prev, ctx) => {
			if (prev.metadata !== null && prev.ctxInvoke !== null && prev.typeInvoke !== null) {
				return prev;
			}
			const metadata = prev.metadata ?? ctx.allocateGetServiceMetadata(stub);
			// Cast through `ServiceRefStub<unknown>` so the
			// (untyped) protocol arg constraint accepts the
			// pseudo-method name. Returns `OpResultStub<unknown>`.
			const ctxInvoke =
				prev.ctxInvoke ??
				ctx.allocateInvoke(stub as ServiceRefStub<unknown>, "$fxtr.ctx", [] as never);
			const typeInvoke =
				prev.typeInvoke ??
				ctx.allocateInvoke(stub as ServiceRefStub<unknown>, "$fxtr.type", [] as never);
			return { metadata, ctxInvoke, typeInvoke };
		});
	}, [stub, setState]);

	return (
		<>
			<Section title="Metadata">
				{state.metadata !== null ? (
					<MetadataSection stub={state.metadata} />
				) : (
					<Muted>allocating…</Muted>
				)}
			</Section>

			<Section title="$fxtr.ctx">
				{state.ctxInvoke !== null ? (
					<InvokeResultSection stub={state.ctxInvoke} />
				) : (
					<Muted>allocating…</Muted>
				)}
			</Section>

			<Section title="$fxtr.type">
				{state.typeInvoke !== null ? (
					<InvokeResultSection stub={state.typeInvoke} />
				) : (
					<Muted>allocating…</Muted>
				)}
			</Section>
		</>
	);
}

function MetadataSection({ stub }: { readonly stub: OpResultStub<ServiceMetadata> }) {
	const status = useResult(stub);
	return (
		<ResultGate status={status}>
			{(metadata) => (
				<dl style={dlStyle}>
					<div style={dlRowStyle}>
						<dt style={dtStyle}>type_id_string</dt>
						<dd style={ddStyle}>
							<code style={monoStyle}>{metadata.type_id_string}</code>
						</dd>
					</div>
				</dl>
			)}
		</ResultGate>
	);
}

function InvokeResultSection({ stub }: { readonly stub: OpResultStub<unknown> }) {
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
	if (status.status === "pending") {
		return <Muted>pending…</Muted>;
	}
	if (status.status === "errored") {
		return <Errored message={status.message} />;
	}
	return <>{children(status.value)}</>;
}

function Section({ title, children }: { readonly title: string; readonly children: ReactNode }) {
	return (
		<section style={sectionStyle}>
			<div style={sectionHeaderStyle}>{title}</div>
			<div style={sectionBodyStyle}>{children}</div>
		</section>
	);
}

function Muted({ children }: { readonly children: ReactNode }) {
	return <span style={mutedStyle}>{children}</span>;
}

function Errored({ message }: { readonly message: string }) {
	return <span style={erroredStyle}>error: {message}</span>;
}

const rootStyle: CSSProperties = {
	padding: "var(--sp-5, 16px)",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-4, 16px)",
	fontFamily: "var(--font-prose)",
	color: "var(--text-primary)",
};

const headerStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 18px)",
	color: "var(--text-primary)",
};

const sectionStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
};

const sectionHeaderStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-muted)",
};

const sectionBodyStyle: CSSProperties = {
	padding: "var(--sp-3, 12px)",
	background: "var(--surface-sunken)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
};

const dlStyle: CSSProperties = {
	margin: 0,
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
};

const dlRowStyle: CSSProperties = {
	display: "grid",
	gridTemplateColumns: "minmax(150px, 0.25fr) 1fr",
	alignItems: "baseline",
	gap: "var(--sp-3, 12px)",
};

const dtStyle: CSSProperties = {
	margin: 0,
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
};

const ddStyle: CSSProperties = {
	margin: 0,
	minWidth: 0,
};

const monoStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-primary)",
};

const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontSize: "var(--text-body-sm)",
	fontStyle: "italic",
};

const erroredStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontSize: "var(--text-body-sm)",
};
