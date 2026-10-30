/**
 * ``StreamEntry`` renderer for :class:`InvokeErrorLogEvent`.
 *
 * The workstation reported an error for the agent's invoke at
 * this slot. We surface slot identity + the error message in
 * error styling.
 */

import type { EntityRefStub, OpResultStub } from "@fxtr-ui/core";
import type { StreamEntryProps } from "@fxtr-ui/rd-types-stream";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { useAtomValue } from "jotai";
import type { CSSProperties } from "react";
import { useMemo } from "react";

import type { InvokeErrorValue } from "../data_shapes.js";
import { makeLoadResultAtom } from "../load_effect.js";
import {
	CollapsibleCard,
	StreamCardHeader,
	cardStyle,
	cardTagStyle,
	errorTextStyle,
	mutedStyle,
} from "./shared.js";
import { summarizeInvokeError } from "./summarize.js";

const errorTagStyle: CSSProperties = {
	...cardTagStyle,
	color: "var(--text-danger, #b3261e)",
};

export const InvokeErrorStreamEntry: DispatchedComponent<EntityRefStub, StreamEntryProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<InvokeErrorValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<InvokeErrorValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);

	if (result.kind === "loading") {
		return (
			<div style={cardStyle}>
				<StreamCardHeader tag="invoke error" />
				<div style={mutedStyle}>…</div>
			</div>
		);
	}
	if (result.kind === "errored") {
		return (
			<div style={cardStyle}>
				<StreamCardHeader tag="invoke error" tagStyle={errorTagStyle} />
				<div style={errorTextStyle}>entry failed to load: {result.message}</div>
			</div>
		);
	}

	const v = result.value;
	const detail = `${v.slot_key} · slot ${v.slot_n} · seq ${v.seq}`;
	return (
		<CollapsibleCard
			tag="invoke error"
			tagStyle={errorTagStyle}
			detail={detail}
			summary={summarizeInvokeError(v)}
		>
			<div style={errorTextStyle}>{v.message}</div>
		</CollapsibleCard>
	);
};
