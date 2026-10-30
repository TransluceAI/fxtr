/**
 * ``StreamEntry`` renderer for :class:`InvokeResultLogEvent`.
 *
 * The MCP received a successful workstation invoke result and
 * recorded it in the chain. We surface the slot identity
 * (slot_key / slot_n / seq) + the payload through
 * ``<JsonRefTree>`` so any entity / service refs in the payload
 * render as clickable links.
 */

import type { EntityRefStub, OpResultStub } from "@fxtr-ui/core";
import type { StreamEntryProps } from "@fxtr-ui/rd-types-stream";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { useAtomValue } from "jotai";
import { useMemo } from "react";

import type { InvokeResultValue } from "../data_shapes.js";
import { JsonRefTreeWithServiceSubstitution } from "../inline_link_substitution.js";
import { makeLoadResultAtom } from "../load_effect.js";
import {
	CollapsibleCard,
	StreamCardHeader,
	cardStyle,
	errorTextStyle,
	mutedStyle,
} from "./shared.js";
import { summarizeInvokeResult } from "./summarize.js";

export const InvokeResultStreamEntry: DispatchedComponent<EntityRefStub, StreamEntryProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<InvokeResultValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<InvokeResultValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);

	if (result.kind === "loading") {
		return (
			<div style={cardStyle}>
				<StreamCardHeader tag="invoke result" />
				<div style={mutedStyle}>…</div>
			</div>
		);
	}
	if (result.kind === "errored") {
		return (
			<div style={cardStyle}>
				<StreamCardHeader tag="invoke result" />
				<div style={errorTextStyle}>entry failed to load: {result.message}</div>
			</div>
		);
	}

	const v = result.value;
	const detail = `${v.slot_key} · slot ${v.slot_n} · seq ${v.seq}`;
	return (
		<CollapsibleCard tag="invoke result" detail={detail} summary={summarizeInvokeResult(v)}>
			<JsonRefTreeWithServiceSubstitution value={v.payload} />
		</CollapsibleCard>
	);
};
