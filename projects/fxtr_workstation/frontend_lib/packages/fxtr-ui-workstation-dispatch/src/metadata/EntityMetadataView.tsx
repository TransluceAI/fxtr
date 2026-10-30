/**
 * Hardcoded metadata view for an entity ref. Rendered by
 * `PanelWrapper` when the active panel-view entry has
 * `display: "metadata"` (in place of the dispatched FullPanel
 * renderer).
 *
 * Three sections, top-to-bottom:
 *
 *   1. **Metadata** — fields from `allocateGetEntityMetadata`.
 *      Ref-typed fields render as `<RefLink as="inline">`; string
 *      fields render as monospace `<code>`-styled spans.
 *
 *   2. **`$fxtr.ctx` / `$fxtr.type`** — pulled from the loaded
 *      data's top-level keys (for entities, these are *fields* on
 *      the value, not methods). Each rendered via the JsonRefTree
 *      so it works whether the field is a primitive, a ref, or a
 *      nested object.
 *
 *   3. **Raw data** — full collapsible JSON tree of the loaded
 *      value with FxtrID / EntityRefStub / ServiceRefStub leaves
 *      rendered as `<RefLink as="inline">`.
 *
 * Per-panel state (the two `OpResultStub`s for metadata + load)
 * lives in the panel-session's `userState.metadataStorage` slot.
 * The view receives that slot as a sub-atom from `PanelWrapper`
 * and lifts it through `atomWithDefault` to its own shape.
 */

import type { EntityMetadata, EntityRefStub, OpResultStub, ResultStatus } from "@fxtr-ui/core";
import type { FxtrSessionAtom } from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import { atomWithDefault } from "@fxtr-ui/state-util";
import { useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, type ReactNode, useEffect, useMemo } from "react";
import { JsonRefTree } from "./JsonRefTree.js";
import { MetadataFields } from "./MetadataFields.js";

export interface EntityMetadataViewProps {
	readonly stub: EntityRefStub;
	readonly storage: FxtrSessionAtom<unknown>;
}

interface EntityMetadataStorage {
	readonly metadata: OpResultStub<EntityMetadata> | null;
	readonly load: OpResultStub<unknown> | null;
}

const INITIAL_STORAGE: EntityMetadataStorage = {
	metadata: null,
	load: null,
};

export function EntityMetadataView({ stub, storage }: EntityMetadataViewProps) {
	const typedStorage = useMemo(
		// `storage` is typed `FxtrSessionAtom<unknown>` (the
		// metadata-mode renderer's caller hands a typeless slot
		// from `RefPanelChainState.storages`). We own the slot's
		// contents here, so cast to the concrete shape before
		// lifting through `atomWithDefault`.
		() =>
			atomWithDefault<EntityMetadataStorage>(
				storage as FxtrSessionAtom<EntityMetadataStorage | null>,
				INITIAL_STORAGE,
			),
		[storage],
	);
	const state = useAtomValue(typedStorage);
	const setState = useSetAtom(typedStorage);

	// Allocate both slots on first mount for this stub. Dedup at
	// the wire layer means re-mounting (e.g. after navigating away
	// and back) reuses the existing slots — no double allocation.
	// The `prev`-checks guard against StrictMode double-fire.
	useEffect(() => {
		setState((prev, ctx) => {
			if (prev.metadata !== null && prev.load !== null) return prev;
			const metadata = prev.metadata ?? ctx.allocateGetEntityMetadata(stub);
			const load = prev.load ?? ctx.allocateLoad<unknown>(stub);
			return { metadata, load };
		});
	}, [stub, setState]);

	return (
		<div className="fw-metadata-view" style={rootStyle}>
			<h2 style={headerStyle}>Entity metadata · {stub.shortId}</h2>

			<Section title="Metadata">
				{state.metadata !== null ? (
					<MetadataSection stub={state.metadata} />
				) : (
					<Muted>allocating…</Muted>
				)}
			</Section>

			<Section title="$fxtr.ctx / $fxtr.type">
				{state.load !== null ? <FxtrFieldsSection stub={state.load} /> : <Muted>allocating…</Muted>}
			</Section>

			<Section title="Raw data">
				{state.load !== null ? <RawDataSection stub={state.load} /> : <Muted>allocating…</Muted>}
			</Section>
		</div>
	);
}

// ---------------------------------------------------------------------------
// Metadata section.
// ---------------------------------------------------------------------------

function MetadataSection({ stub }: { readonly stub: OpResultStub<EntityMetadata> }) {
	const status = useResult(stub);
	return (
		<ResultGate status={status}>{(metadata) => <MetadataFields metadata={metadata} />}</ResultGate>
	);
}

// ---------------------------------------------------------------------------
// $fxtr.ctx / $fxtr.type extraction from the loaded value.
// ---------------------------------------------------------------------------

function FxtrFieldsSection({ stub }: { readonly stub: OpResultStub<unknown> }) {
	const status = useResult(stub);
	return (
		<ResultGate status={status}>
			{(value) => {
				const ctx = pickField(value, "$fxtr.ctx");
				const type = pickField(value, "$fxtr.type");
				if (ctx === MISSING && type === MISSING) {
					return <Muted>(none — loaded value is not an object with these fields)</Muted>;
				}
				return (
					<dl style={dlStyle}>
						{ctx !== MISSING && (
							<div style={dlRowStyle}>
								<dt style={dtStyle}>$fxtr.ctx</dt>
								<dd style={ddStyle}>
									<JsonRefTree value={ctx} initiallyExpanded />
								</dd>
							</div>
						)}
						{type !== MISSING && (
							<div style={dlRowStyle}>
								<dt style={dtStyle}>$fxtr.type</dt>
								<dd style={ddStyle}>
									<JsonRefTree value={type} initiallyExpanded />
								</dd>
							</div>
						)}
					</dl>
				);
			}}
		</ResultGate>
	);
}

const MISSING = Symbol("missing");

function pickField(value: unknown, key: string): unknown | typeof MISSING {
	if (value === null || typeof value !== "object") return MISSING;
	if (!(key in value)) return MISSING;
	return (value as Record<string, unknown>)[key];
}

// ---------------------------------------------------------------------------
// Raw data section.
// ---------------------------------------------------------------------------

function RawDataSection({ stub }: { readonly stub: OpResultStub<unknown> }) {
	const status = useResult(stub);
	return (
		<ResultGate status={status}>
			{(value) => <JsonRefTree value={value} initiallyExpanded />}
		</ResultGate>
	);
}

// ---------------------------------------------------------------------------
// Shared bits.
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Styles.
// ---------------------------------------------------------------------------

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

const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontSize: "var(--text-body-sm)",
	fontStyle: "italic",
};

const erroredStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontSize: "var(--text-body-sm)",
};
