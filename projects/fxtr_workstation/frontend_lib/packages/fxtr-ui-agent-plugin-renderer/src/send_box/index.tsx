/**
 * Text input + Send button at the bottom of the agent panel.
 *
 * The text input is a ``<RefChipEditor>`` (CodeMirror 6) rather
 * than a plain ``<textarea>`` so dropped / typed ``@[shortid]``
 * tokens render as styled chips inheriting the shell's
 * ``RefLinkImplContext`` (click navigation, drag-out, hover
 * preview, context menu).
 *
 * Send payload shape:
 *
 * - If the draft contains no ``@[shortid]`` tokens → invoke
 *   ``send(<bare-string>)`` (backwards compatible with
 *   pre-tweak peers).
 * - Otherwise → invoke ``send({message, attached})`` where
 *   ``attached`` is the connector-interned ``EntityRefStub`` /
 *   ``ServiceRefStub`` for every matched shortId in document
 *   order. Including the stubs in ``attached`` ensures the
 *   workstation pins those refs server-side for the duration of
 *   the in-flight invoke (so the agent's session can resolve
 *   them after the callback dispatch).
 *
 * Status-line / retry behavior is unchanged from the previous
 * implementation: a small status line above the input surfaces
 * "sending: …" / "send failed: …" feedback, and an errored send
 * offers a retry button that restores the last draft.
 */

import type { FxtrSessionAtom, OpResultStub, ServiceRefStub } from "@fxtr-ui/core";
import { useRefDropRegion, useRefStubResolver } from "@fxtr-ui/workstation-dispatch";
import { useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, useCallback } from "react";

import type { AgentMcpRunningSessionProto } from "../proto.js";
import type { PanelState } from "../state_machine.js";
import { buildSendPayload } from "./build_payload.js";
import { ChipContent } from "./chip_content.js";
import { RefChipEditor } from "./ref_chip_editor.js";

const containerStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1)",
	padding: "var(--sp-3) var(--sp-4)",
	borderTop: "1px solid var(--border-subtle)",
	background: "var(--surface-base)",
};

const statusLineMutedStyle: CSSProperties = {
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	fontStyle: "italic",
};

const statusLineErrorStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2)",
	fontSize: "var(--text-caption)",
	color: "var(--text-danger, #b3261e)",
};

const inputRowStyle: CSSProperties = {
	display: "flex",
	gap: "var(--sp-2)",
	alignItems: "flex-end",
};

const sendButtonStyle: CSSProperties = {
	alignSelf: "flex-end",
	padding: "var(--sp-2) var(--sp-4)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-input)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
	cursor: "pointer",
};

const sendButtonDisabledStyle: CSSProperties = {
	...sendButtonStyle,
	color: "var(--text-muted)",
	cursor: "default",
};

const retryButtonStyle: CSSProperties = {
	padding: "0 var(--sp-2)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-input)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-caption)",
	cursor: "pointer",
};

/**
 * Inner status line — only rendered when ``lastSend`` is non-null
 * so the ``useAtomValue(lastSend.result)`` call is unconditional.
 */
function SendStatusLineInner(props: {
	panelStateAtom: FxtrSessionAtom<PanelState>;
	lastSend: OpResultStub<null>;
	lastSendDraft: string | null;
}) {
	const setPanel = useSetAtom(props.panelStateAtom);
	const result = useAtomValue(props.lastSend.result);

	if (result.status === "pending") {
		return <div style={statusLineMutedStyle}>sending: {props.lastSendDraft ?? ""}…</div>;
	}
	if (result.status === "errored") {
		const onRetry = (): void => {
			setPanel((prev) => ({
				...prev,
				draftMessage: prev.lastSendDraft ?? prev.draftMessage,
				lastSend: null,
				lastSendDraft: null,
			}));
		};
		return (
			<div style={statusLineErrorStyle}>
				<span>send failed: {result.message}</span>
				<button type="button" style={retryButtonStyle} onClick={onRetry}>
					retry
				</button>
			</div>
		);
	}
	// Filled — hidden (the chain shows the successful message).
	return null;
}

function SendStatusLine(props: { panelStateAtom: FxtrSessionAtom<PanelState> }) {
	const state = useAtomValue(props.panelStateAtom);
	if (state.lastSend === null) return null;
	return (
		<SendStatusLineInner
			panelStateAtom={props.panelStateAtom}
			lastSend={state.lastSend}
			lastSendDraft={state.lastSendDraft}
		/>
	);
}

export function SendBox(props: {
	panelStateAtom: FxtrSessionAtom<PanelState>;
	serviceStub: ServiceRefStub<AgentMcpRunningSessionProto>;
}) {
	const state = useAtomValue(props.panelStateAtom);
	const setPanel = useSetAtom(props.panelStateAtom);
	const resolver = useRefStubResolver();
	const { regionProps } = useRefDropRegion();

	const onSend = useCallback((): void => {
		setPanel((prev, ctx) => {
			const draft = prev.draftMessage;
			if (draft.length === 0) return prev;
			const payload = buildSendPayload(draft, resolver);
			const invoke = ctx.allocateInvoke(props.serviceStub, "send", [payload]);
			return {
				...prev,
				draftMessage: "",
				lastSend: invoke,
				lastSendDraft: draft,
			};
		});
	}, [setPanel, resolver, props.serviceStub]);

	const onChange = useCallback(
		(value: string): void => {
			setPanel((prev) => ({ ...prev, draftMessage: value }));
		},
		[setPanel],
	);

	const renderChip = useCallback(
		({ shortId }: { shortId: string }) => <ChipContent shortId={shortId} />,
		[],
	);

	const canSend = state.draftMessage.length > 0;

	return (
		<div style={containerStyle}>
			<SendStatusLine panelStateAtom={props.panelStateAtom} />
			<div style={inputRowStyle}>
				<RefChipEditor
					value={state.draftMessage}
					onChange={onChange}
					onSubmit={onSend}
					placeholder="Send a message to the agent…"
					regionProps={regionProps}
					renderChip={renderChip}
				/>
				<button
					type="button"
					style={canSend ? sendButtonStyle : sendButtonDisabledStyle}
					onClick={onSend}
					disabled={!canSend}
				>
					Send
				</button>
			</div>
		</div>
	);
}
