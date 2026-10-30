/**
 * `MetadataFields` renders the populated fields of an
 * `EntityMetadata` value as a label/value table. Ref-typed
 * fields render as `<RefLink as="inline">`; id-string fields
 * render as monospace `<code>` spans.
 *
 * Shared between `EntityMetadataView` (the panel-display
 * metadata-mode view, which also loads + renders the entity's
 * raw data) and `EntityFullPanelBuiltinFallback` (the FullPanel
 * `Builtin` fallback, which only does the metadata call — no
 * load — and uses this to surface the populated fields,
 * including `context_data` for c-tag nodes).
 */

import type { EntityMetadata } from "@fxtr-ui/core";
import type { CSSProperties, ReactNode } from "react";
import { RefLink } from "../reflink/RefLink.js";

export function MetadataFields({ metadata }: { readonly metadata: EntityMetadata }) {
	const rows: Array<{ key: string; value: ReactNode }> = [];
	rows.push({ key: "id_string", value: <Mono>{metadata.id_string}</Mono> });
	if (metadata.unprovenanced !== undefined) {
		rows.push({
			key: "unprovenanced",
			value: <RefLink stub={metadata.unprovenanced} as="inline" />,
		});
	}
	if (metadata.provenance_parent !== undefined) {
		rows.push({
			key: "provenance_parent",
			value: <RefLink stub={metadata.provenance_parent} as="inline" />,
		});
	}
	if (metadata.provenance_parent_id_string !== undefined) {
		rows.push({
			key: "provenance_parent_id_string",
			value: <Mono>{metadata.provenance_parent_id_string}</Mono>,
		});
	}
	if (metadata.unwraps_to !== undefined) {
		rows.push({ key: "unwraps_to", value: <RefLink stub={metadata.unwraps_to} as="inline" /> });
	}
	if (metadata.unwraps_to_id_string !== undefined) {
		rows.push({
			key: "unwraps_to_id_string",
			value: <Mono>{metadata.unwraps_to_id_string}</Mono>,
		});
	}
	if (metadata.context_data !== undefined) {
		rows.push({
			key: "context_data",
			value: <RefLink stub={metadata.context_data} as="inline" />,
		});
	}
	if (metadata.backstep !== undefined) {
		rows.push({ key: "backstep", value: <RefLink stub={metadata.backstep} as="inline" /> });
	}
	return (
		<dl style={dlStyle}>
			{rows.map((row) => (
				<div key={row.key} style={dlRowStyle}>
					<dt style={dtStyle}>{row.key}</dt>
					<dd style={ddStyle}>{row.value}</dd>
				</div>
			))}
		</dl>
	);
}

function Mono({ children }: { readonly children: ReactNode }) {
	return <code style={monoStyle}>{children}</code>;
}

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
