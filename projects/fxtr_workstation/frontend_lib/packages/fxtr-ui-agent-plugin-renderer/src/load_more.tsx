/**
 * "Load more" affordance shown above an entry whose chain-head
 * ``prev`` doesn't match the previous entry's stub — i.e. the
 * watcher skipped one or more intermediate heads.
 *
 * Clicking allocates a load on the gap head and splices a fresh
 * entry into the list at the appropriate position; the new entry
 * may itself reveal a further gap, so iterating peels the back-
 * walk one head at a time.
 *
 * The component is rendered by the panel's stream; gap detection
 * lives in the panel (it has the cross-entry context). This
 * module exports only the click-affordance + its updater helper.
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub } from "@fxtr-ui/core";
import { useSetAtom } from "jotai";
import type { CSSProperties } from "react";

import type { ChainNodeValue } from "./data_shapes.js";
import type { PanelState } from "./state_machine.js";

const linkStyle: CSSProperties = {
	display: "inline-block",
	padding: "var(--sp-1) var(--sp-2)",
	color: "var(--text-link, #2563eb)",
	background: "transparent",
	border: "none",
	cursor: "pointer",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-caption)",
	textDecoration: "underline",
};

const disabledStyle: CSSProperties = {
	...linkStyle,
	color: "var(--text-muted)",
	cursor: "default",
	textDecoration: "none",
};

const containerStyle: CSSProperties = {
	display: "flex",
	justifyContent: "center",
	padding: "var(--sp-1) 0",
};

/**
 * "Load more" button: a small underlined link above the entry
 * whose ``prev`` chain-stub doesn't equal the preceding entry's
 * stub. Disabled while ``priorLoad`` is pending (we don't know
 * what the gap stub is until the prior entry's chain node has
 * loaded).
 */
export function LoadMoreLink(props: {
	panelStateAtom: FxtrSessionAtom<PanelState>;
	// The chain entry that has a non-matching ``prev`` — i.e.
	// the entry below the gap. Its ``load`` must be filled
	// before clicking can do anything meaningful.
	belowLoad: OpResultStub<ChainNodeValue>;
	belowLoadStatus: "pending" | "filled" | "errored";
	belowLoadValue: ChainNodeValue | null;
	// Position to splice the new entry at — between the prior
	// entry (index belowIndex - 1) and the gap entry
	// (index belowIndex).
	belowIndex: number;
}) {
	const setPanel = useSetAtom(props.panelStateAtom);

	const disabled = props.belowLoadStatus !== "filled" || props.belowLoadValue?.prev === null;

	const onClick = (): void => {
		if (disabled) return;
		const gapHead = props.belowLoadValue?.prev;
		if (gapHead === null || gapHead === undefined) return;
		setPanel((prev, ctx) => {
			const load = ctx.allocateLoad(gapHead as EntityRefStub<ChainNodeValue>);
			return {
				...prev,
				entries: [
					...prev.entries.slice(0, props.belowIndex),
					{ stub: gapHead, load },
					...prev.entries.slice(props.belowIndex),
				],
			};
		});
	};

	return (
		<div style={containerStyle}>
			<button
				type="button"
				style={disabled ? disabledStyle : linkStyle}
				onClick={onClick}
				disabled={disabled}
			>
				{props.belowLoadStatus === "pending" ? "loading…" : "Load missing entries"}
			</button>
		</div>
	);
}
