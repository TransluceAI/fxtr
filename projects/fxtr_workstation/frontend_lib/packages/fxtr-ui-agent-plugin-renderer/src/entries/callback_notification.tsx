/**
 * ``StreamEntry`` renderer for :class:`CallbackNotificationLogEvent`.
 *
 * Two visual variants depending on ``slot_key``:
 *
 * - ``slot_key === "workstation_inbox"`` — the reserved inbox the
 *   frontend's ``send()`` lands on. Rendered as a "User said"
 *   message with the payload as text (when the payload is a
 *   string) or through ``<JsonRefTree>`` (when it's structured).
 * - Other slot keys — generic "callback notification" card with
 *   slot identity + payload through ``<JsonRefTree>``.
 */

import { EntityRefStub, type OpResultStub, type ResultValue, ServiceRefStub } from "@fxtr-ui/core";
import type { StreamEntryProps } from "@fxtr-ui/rd-types-stream";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { RefLink } from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import type { CSSProperties, ReactElement } from "react";
import { useMemo } from "react";

import type { CallbackNotificationValue } from "../data_shapes.js";
import { JsonRefTreeWithServiceSubstitution } from "../inline_link_substitution.js";
import { makeLoadResultAtom } from "../load_effect.js";
import { tokenizeMessage } from "../send_box/parse_refs.js";
import {
	CollapsibleCard,
	StreamCardHeader,
	cardStyle,
	errorTextStyle,
	mutedStyle,
} from "./shared.js";
import { summarizeCallbackNotif } from "./summarize.js";

const WORKSTATION_INBOX = "workstation_inbox";

const userMsgBodyStyle: CSSProperties = {
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body)",
	color: "var(--text-primary)",
};

const userTagStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-accent, #2563eb)",
	fontWeight: 600,
};

const unresolvedRefStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	border: "1px dashed var(--border-subtle)",
	borderRadius: "var(--radius-input)",
	padding: "0 4px",
};

/**
 * Wire shape for the structured user-message variant — what the
 * send-box emits when at least one ``@[shortid]`` ref is present
 * in the draft. ``attached`` is the ordered list of stubs the
 * shell-side resolver minted from each shortId; bare strings in
 * ``message`` between tokens are rendered verbatim, ``@[shortid]``
 * tokens render as inline ``<RefLink>``s keyed by shortId.
 */
interface StructuredUserMessage {
	readonly message: string;
	readonly attached: ReadonlyArray<EntityRefStub | ServiceRefStub>;
}

/**
 * Type predicate distinguishing the structured payload shape from
 * bare strings (legacy / non-chip sends) and arbitrary other
 * shapes (third-party callers that overload ``workstation_inbox``).
 *
 * Required shape: ``{message: string, attached: ReadonlyArray<refs>}``
 * with every ``attached`` entry being an ``EntityRefStub`` or
 * ``ServiceRefStub`` instance (post-``unescapeWire`` lift).
 */
function isStructuredUserMessage(v: unknown): v is StructuredUserMessage {
	if (v === null || typeof v !== "object" || Array.isArray(v) || v instanceof Uint8Array) {
		return false;
	}
	const o = v as Record<string, ResultValue>;
	if (typeof o.message !== "string") return false;
	if (!Array.isArray(o.attached)) return false;
	for (const entry of o.attached) {
		if (!(entry instanceof EntityRefStub) && !(entry instanceof ServiceRefStub)) return false;
	}
	return true;
}

/**
 * Render the structured user-message body: tokenize ``message``
 * via the shared ``tokenizeMessage`` helper, build a shortId →
 * stub lookup from ``attached``, and emit a flat sequence of
 * text spans + inline ``<RefLink>``s.
 *
 * Unmatched tokens (the message references a shortId that isn't
 * in ``attached``) render as a muted literal ``@[shortid]``. This
 * matches the editor's render-time behavior for unresolved
 * shortIds.
 */
function StructuredMessageBody(props: { value: StructuredUserMessage }) {
	const { message, attached } = props.value;
	const byShortId = new Map<string, EntityRefStub | ServiceRefStub>();
	for (const s of attached) byShortId.set(s.shortId, s);
	const tokens = tokenizeMessage(message);
	return (
		<div style={userMsgBodyStyle}>
			{tokens.map((tok, i) => {
				if (tok.kind === "text") {
					// biome-ignore lint/suspicious/noArrayIndexKey: tokens are stable order within a payload
					return <span key={i}>{tok.value}</span>;
				}
				const stub = byShortId.get(tok.shortId);
				if (stub === undefined) {
					return (
						// biome-ignore lint/suspicious/noArrayIndexKey: tokens are stable order within a payload
						<span key={i} style={unresolvedRefStyle}>
							@[{tok.shortId}]
						</span>
					);
				}
				// biome-ignore lint/suspicious/noArrayIndexKey: tokens are stable order within a payload
				return <RefLink key={i} as="inline" stub={stub} />;
			})}
		</div>
	);
}

export const CallbackNotificationStreamEntry: DispatchedComponent<
	EntityRefStub,
	StreamEntryProps
> = ({ stub }) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<CallbackNotificationValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<CallbackNotificationValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);

	if (result.kind === "loading") {
		return (
			<div style={cardStyle}>
				<StreamCardHeader tag="callback notification" />
				<div style={mutedStyle}>…</div>
			</div>
		);
	}
	if (result.kind === "errored") {
		return (
			<div style={cardStyle}>
				<StreamCardHeader tag="callback notification" />
				<div style={errorTextStyle}>entry failed to load: {result.message}</div>
			</div>
		);
	}

	const v = result.value;
	const summary = summarizeCallbackNotif(v);

	if (v.slot_key === WORKSTATION_INBOX) {
		// User message branch. Three payload shapes:
		//   - structured `{message, attached}` (modern send-box) →
		//     `<StructuredMessageBody>` with inline RefLinks.
		//   - bare string (back-compat / no-chip send) → plain text.
		//   - anything else (third-party callers) → `<JsonRefTree>`.
		let body: ReactElement;
		if (isStructuredUserMessage(v.payload)) {
			body = <StructuredMessageBody value={v.payload} />;
		} else if (typeof v.payload === "string") {
			body = <div style={userMsgBodyStyle}>{v.payload}</div>;
		} else {
			body = <JsonRefTreeWithServiceSubstitution value={v.payload} />;
		}
		return (
			<CollapsibleCard
				tag="user said"
				tagStyle={userTagStyle}
				detail={`seq ${v.seq}`}
				summary={summary}
			>
				{body}
			</CollapsibleCard>
		);
	}

	const detail = `${v.slot_key} · slot ${v.slot_n} · seq ${v.seq}`;
	return (
		<CollapsibleCard tag="callback notification" detail={detail} summary={summary}>
			<JsonRefTreeWithServiceSubstitution value={v.payload} />
		</CollapsibleCard>
	);
};
