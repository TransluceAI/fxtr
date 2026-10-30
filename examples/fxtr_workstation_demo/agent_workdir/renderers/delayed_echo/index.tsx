/**
 * Renderer for the `delayed_echo` SERVICE (the sandboxable service
 * defined in `packages/delayed_echo/src/delayed_echo/__init__.py`,
 * protocol `DelayedEchoProto.echo(value, delay) -> value`).
 *
 * A service renderer is registered exactly like an entity renderer
 * — keyed by the service's `_service_type_id().id_string()` in this
 * dir's `fxtr_renderer.nix` slice, under the same four shell
 * dispatch keys. The only difference from the `note` example is
 * that the dispatched component receives a `ServiceRefStub` (not an
 * `EntityRefStub`): there's no entity payload to `load`, so instead
 * of `makeLoadResultAtom` the FullPanel drives *invokes* via
 * `ctx.allocateInvoke(stub, "echo", [value, delay])` and renders a
 * history of their results.
 *
 * Modeled on `EchoServiceFullPanel` /  `EchoServiceBlockLink` in
 * `@fxtr-ui-workstation-demo/src/client/handlers.tsx`, extended
 * with a `delay` input to match `echo(value, delay)`.
 *
 * Imports are restricted to the curated externals list (see the
 * `note` renderer header) — react, jotai, and the `@fxtr-ui/*`
 * packages.
 */

import type {
	FxtrSessionAtom,
	OpResultStub,
	ResultStatus,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import { type DispatchedComponent } from "@fxtr-ui/react-dispatch";
import { atomWithDefault } from "@fxtr-ui/state-util";
import {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, useMemo, useState } from "react";

// ----- FullPanel -----
//
// Per-mount state lives in the `ownStorage` sub-atom the dispatcher
// hands FullPanel renderers. We cast it to a concrete `{invokes}`
// shape (this renderer owns the slot) and append one entry per
// fired invoke; each entry holds the `OpResultStub` the invoke
// resolves into.

interface EchoInvokeSlot {
	readonly id: number;
	readonly label: string;
	readonly result: OpResultStub<unknown>;
}

// `kind` is a version tag for the persisted shape. The dispatcher
// keeps the `ownStorage` slot across a `set_preset` reload, so a slot
// written by an older build of this renderer can be re-read by a
// newer one. The `isValidStorage` check below makes `atomWithDefault`
// treat any value without this exact tag as stale and substitute
// the default — bump the version string whenever the shape changes.
const STORAGE_KIND = "delayed_echo/v1";

interface DelayedEchoStorage {
	readonly kind: typeof STORAGE_KIND;
	readonly invokes: ReadonlyArray<EchoInvokeSlot>;
}

const INITIAL: DelayedEchoStorage = { kind: STORAGE_KIND, invokes: [] };

// Guards against stale persisted state: a value whose `kind` is
// missing or doesn't match the current version is rejected, so
// `atomWithDefault` swaps in `INITIAL` instead of handing back a
// shape this build doesn't understand.
function isValidStorage(s: DelayedEchoStorage): boolean {
	return s?.kind === STORAGE_KIND && Array.isArray(s.invokes);
}

/** Next `id` for a `{id}` list — `max + 1`. */
function nextId(list: ReadonlyArray<EchoInvokeSlot>): number {
	let max = 0;
	for (const e of list) if (e.id > max) max = e.id;
	return max + 1;
}

export const DelayedEchoFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const typedStorage = useMemo(
		() =>
			atomWithDefault<DelayedEchoStorage>(
				ownStorage as FxtrSessionAtom<DelayedEchoStorage | null>,
				INITIAL,
				isValidStorage,
			),
		[ownStorage],
	);
	const state = useAtomValue(typedStorage);
	const setState = useSetAtom(typedStorage);
	const [draft, setDraft] = useState("hello");
	const [delay, setDelay] = useState(3);

	const fire = (): void => {
		const value = draft;
		const delaySecs = Math.max(0, delay);
		setState((prev, ctx) => ({
			kind: STORAGE_KIND,
			invokes: [
				...prev.invokes,
				{
					id: nextId(prev.invokes),
					label: `echo(${JSON.stringify(value)}, ${delaySecs})`,
					result: ctx.allocateInvoke(stub as ServiceRefStub<unknown>, "echo", [
						value,
						delaySecs,
					]),
				},
			],
		}));
	};

	const clear = (): void => {
		setState({ kind: STORAGE_KIND, invokes: [] });
	};

	return (
		<div className="fw-panel__body" style={rootStyle}>
			<h2 style={headerStyle}>delayed_echo · {stub.shortId}</h2>
			<div style={mutedStyle}>
				Calls <code>echo(value, delay)</code> — the value comes back after{" "}
				<code>delay</code> seconds.
			</div>
			<div style={inputRowStyle}>
				<label style={fieldStyle}>
					<span style={mutedStyle}>value</span>
					<input
						type="text"
						value={draft}
						onChange={(e) => setDraft(e.target.value)}
						style={textInputStyle}
						placeholder="value"
					/>
				</label>
				<label style={fieldStyle}>
					<span style={mutedStyle}>delay (s)</span>
					<input
						type="number"
						value={delay}
						min={0}
						step={0.5}
						onChange={(e) => setDelay(Number.parseFloat(e.target.value) || 0)}
						style={{ ...textInputStyle, width: "7ch" }}
					/>
				</label>
				<button type="button" onClick={fire} style={buttonStyle}>
					invoke echo
				</button>
				<span style={{ flex: 1 }} />
				<button
					type="button"
					onClick={clear}
					style={buttonStyle}
					disabled={state.invokes.length === 0}
				>
					clear history
				</button>
			</div>
			<div style={historyListStyle}>
				{state.invokes.length === 0 ? (
					<span style={mutedStyle}>
						no invokes yet — set a value + delay and click "invoke echo"
					</span>
				) : (
					state.invokes
						.slice()
						.reverse()
						.map((slot) => <ResultRow key={slot.id} label={slot.label} stub={slot.result} />)
				)}
			</div>
		</div>
	);
};

function ResultRow({
	label,
	stub,
}: {
	readonly label: string;
	readonly stub: OpResultStub<unknown>;
}) {
	const status: ResultStatus<unknown> = useResult(stub);
	return (
		<div style={resultCardStyle}>
			<div style={resultLabelStyle}>
				{label} · {status.status}
			</div>
			{status.status === "filled" ? (
				<pre style={resultBodyStyle}>{formatValue(status.value)}</pre>
			) : status.status === "errored" ? (
				<div style={errorStyle}>{status.message}</div>
			) : (
				<div style={mutedStyle}>waiting for the delayed result…</div>
			)}
		</div>
	);
}

function formatValue(v: unknown): string {
	try {
		return JSON.stringify(v, null, 2);
	} catch {
		return String(v);
	}
}

// ----- BlockLink / InlineLink / HoverPreview -----
//
// A service has no loadable payload, so the link keys are static
// pills identifying the service by shortId.

export const DelayedEchoBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>delayed_echo</span>
			<span style={linkLabelStyle}>{linkText ?? `service · ${stub.shortId}`}</span>
		</span>
	);
};

export const DelayedEchoInlineLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	return <code style={inlineLinkStyle}>delayed_echo:{linkText ?? stub.shortId}</code>;
};

export const DelayedEchoHoverPreview: DispatchedComponent<ServiceRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>delayed_echo service</div>
			<div style={hoverBodyStyle}>
				<code>{stub.shortId}</code> — open the panel to call{" "}
				<code>echo(value, delay)</code>.
			</div>
		</div>
	);
};

// ----- styles (theme-aware CSS vars, with fallbacks) -----

const pad = "var(--sp-5, 16px)";

const rootStyle: CSSProperties = {
	padding: pad,
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-4, 16px)",
	height: "100%",
};
const headerStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 18px)",
	color: "var(--text-primary)",
};
const inputRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "flex-end",
	gap: "var(--sp-2, 8px)",
	flexWrap: "wrap",
};
const fieldStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
};
const textInputStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-2)",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-input, 4px)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	minWidth: "10ch",
};
const buttonStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-3)",
	fontSize: "var(--text-body-sm)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-input, 4px)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	cursor: "pointer",
};
const historyListStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
};
const resultCardStyle: CSSProperties = {
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card, 4px)",
	padding: "var(--sp-2) var(--sp-3)",
	background: "var(--surface-sunken)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
};
const resultLabelStyle: CSSProperties = {
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	fontFamily: "var(--font-mono)",
};
const resultBodyStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-primary)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
	margin: 0,
};
const linkPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: "var(--sp-2, 6px)",
	padding: "var(--sp-1) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge, 6px)",
	background: "var(--surface-raised)",
	fontSize: "var(--text-body-sm)",
};
const linkTagStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	letterSpacing: "0.05em",
};
const linkLabelStyle: CSSProperties = { color: "var(--text-primary)" };
const inlineLinkStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "var(--surface-sunken)",
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
};
const hoverCardStyle: CSSProperties = {
	minWidth: 200,
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3, 12px)",
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600, color: "var(--text-primary)" };
const hoverBodyStyle: CSSProperties = {
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
	lineHeight: 1.4,
};
const mutedStyle: CSSProperties = { color: "var(--text-muted)", fontSize: "var(--text-body-sm)" };
const errorStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontSize: "var(--text-body-sm)",
};

export default {
	delayed_echo: {
		[FullPanel.uuid]: DelayedEchoFullPanel,
		[BlockLink.uuid]: DelayedEchoBlockLink,
		[InlineLink.uuid]: DelayedEchoInlineLink,
		[HoverPreview.uuid]: DelayedEchoHoverPreview,
	},
};
