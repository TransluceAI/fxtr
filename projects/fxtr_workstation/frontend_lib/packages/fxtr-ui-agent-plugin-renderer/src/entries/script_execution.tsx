/**
 * ``StreamEntry`` renderer for :class:`ScriptExecutionLogEvent`.
 *
 * One of three terminal shapes per the Python class:
 * success (``error`` and ``rejected`` both null), script-raised
 * (``error`` non-null), or workstation-rejected (``rejected``
 * non-null). The header tag reflects the outcome; the body
 * surfaces ``stdout`` / ``stderr`` and (on success) the queued
 * ops.
 *
 * **``present_to_user`` emphasized footer.** If any op in the
 * script's ``ops`` list has ``kind === "present_to_user"``, a
 * dedicated bottom block surfaces each such op's payload through
 * ``<JsonRefTree>``. The block is visually emphasized (border +
 * "Presented" header) so it stands out from the rest of the
 * card body — those are the agent's intentional messages to the
 * operator.
 */

import type { EntityRefStub, OpResultStub, ResultValue } from "@fxtr-ui/core";
import type { StreamEntryProps } from "@fxtr-ui/rd-types-stream";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { useAtomValue } from "jotai";
import type { CSSProperties } from "react";
import { useMemo } from "react";

import {
	OP_KIND_PRESENT_TO_USER,
	type ScriptExecutionValue,
	type ScriptOpRecord,
} from "../data_shapes.js";
import { JsonRefTreeWithServiceSubstitution } from "../inline_link_substitution.js";
import { makeLoadResultAtom } from "../load_effect.js";
import {
	CollapsibleCard,
	StreamCardHeader,
	cardStyle,
	codeBlockStyle,
	errorTextStyle,
	mutedStyle,
	sectionLabelStyle,
} from "./shared.js";
import { summarizeScript } from "./summarize.js";

const sectionStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1)",
};

const opSummaryStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};

/**
 * Column wrapper that stacks the main script block on top of the
 * optional "presented to user" companion block. Keeps the row's
 * single timestamp prefix correct: the row renders one timestamp
 * + one body cell containing this column.
 */
const columnStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2)",
};

function describeOp(op: ScriptOpRecord): string {
	switch (op.kind) {
		case "export_entity":
			return `export_entity ${op.slot_key} = entity ${op.entity.shortId}`;
		case "export_service":
			return `export_service ${op.slot_key} = service ${op.service.shortId}`;
		case "forget":
			return `forget ${op.slot_key}`;
		case "schedule_invoke":
			return `schedule_invoke ${op.slot_key}: ${op.service_short_id}.${op.method_name}(…)`;
		case "create_callback":
			return `create_callback ${op.slot_key}`;
		case "forget_callback_notification":
			return `forget_callback_notification ${op.slot_key}[${op.notif_index}]`;
		case "present_to_user":
			return "present_to_user (…)";
	}
}

export const ScriptExecutionStreamEntry: DispatchedComponent<EntityRefStub, StreamEntryProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<ScriptExecutionValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<ScriptExecutionValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);

	if (result.kind === "loading") {
		return (
			<div style={cardStyle}>
				<StreamCardHeader tag="script" />
				<div style={mutedStyle}>…</div>
			</div>
		);
	}
	if (result.kind === "errored") {
		return (
			<div style={cardStyle}>
				<StreamCardHeader tag="script" />
				<div style={errorTextStyle}>entry failed to load: {result.message}</div>
			</div>
		);
	}

	const v = result.value;
	const tag =
		v.error !== null ? "script errored" : v.rejected !== null ? "script rejected" : "script ran";
	const detail = v.committed_seq !== null ? `seq ${v.committed_seq}` : undefined;
	const presentedOps = v.ops.filter(
		(op): op is Extract<ScriptOpRecord, { kind: typeof OP_KIND_PRESENT_TO_USER }> =>
			op.kind === OP_KIND_PRESENT_TO_USER,
	);
	const otherOps = v.ops.filter((op) => op.kind !== OP_KIND_PRESENT_TO_USER);
	const summary = summarizeScript(v);

	const presentedSummary = presentedOps.length === 1 ? "1 value" : `${presentedOps.length} values`;

	// Main script block — collapsed by default. The "presented to
	// user" entries live in a sibling block that defaults to open,
	// so the agent's intentional messages are visible at a glance
	// even when the rest of the script details are hidden.
	return (
		<div style={columnStyle}>
			<CollapsibleCard tag={tag} detail={detail} summary={summary}>
				{v.script_src.length > 0 ? (
					<div style={sectionStyle}>
						<span style={sectionLabelStyle}>source</span>
						<div style={codeBlockStyle}>{v.script_src}</div>
					</div>
				) : null}
				{v.stdout.length > 0 ? (
					<div style={sectionStyle}>
						<span style={sectionLabelStyle}>stdout</span>
						<div style={codeBlockStyle}>{v.stdout}</div>
					</div>
				) : null}
				{v.stderr.length > 0 ? (
					<div style={sectionStyle}>
						<span style={sectionLabelStyle}>stderr</span>
						<div style={codeBlockStyle}>{v.stderr}</div>
					</div>
				) : null}
				{v.error !== null ? (
					<div style={sectionStyle}>
						<span style={sectionLabelStyle}>error</span>
						<div style={errorTextStyle}>{v.error}</div>
					</div>
				) : null}
				{v.rejected !== null ? (
					<div style={sectionStyle}>
						<span style={sectionLabelStyle}>rejected</span>
						<div style={errorTextStyle}>{v.rejected}</div>
					</div>
				) : null}
				{otherOps.length > 0 ? (
					<div style={sectionStyle}>
						<span style={sectionLabelStyle}>ops</span>
						{otherOps.map((op, i) => (
							// biome-ignore lint/suspicious/noArrayIndexKey: ops are stable order within a record
							<div key={i} style={opSummaryStyle}>
								{describeOp(op)}
							</div>
						))}
					</div>
				) : null}
			</CollapsibleCard>
			{presentedOps.length > 0 ? (
				<CollapsibleCard tag="presented to user" defaultOpen emphasis summary={presentedSummary}>
					{presentedOps.map((op, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: ops are stable order within a record
						<JsonRefTreeWithServiceSubstitution key={i} value={op.payload as ResultValue} />
					))}
				</CollapsibleCard>
			) : null}
		</div>
	);
};
