/**
 * Per-type renderers for the workstation demo.
 *
 * Entity types:
 *
 *   - `DemoNote { title; body }` — FullPanel, BlockLink,
 *     HoverPreview.
 *   - `DemoListNode { label; next: EntityRefStub | null }` —
 *     FullPanel, BlockLink, HoverPreview. HoverPreview embeds
 *     a nested `<RefLink>` to `next` so nested-hover drag paths
 *     are exercisable live.
 *
 * Service types:
 *
 *   - `echo(value) -> value` — `EchoServiceFullPanel` with a
 *     text input + invoke button + history of results.
 *   - `increment / reset / value` counter —
 *     `CounterServiceFullPanel` with three buttons + history.
 *   - `notify_n(cb, n, prefix) -> int` —
 *     `NotifyNServiceFullPanel` with n/prefix inputs + a "fire"
 *     button. Each firing mints a fresh `CallbackInbox` and an
 *     invoke whose `cb` arg is the inbox's `handle`; renders the
 *     streamed `CallbackEvent` payloads as a live list.
 *
 * Per-mount state for the service FullPanels lives in the
 * `ownStorage` sub-atom passed by `PanelWrapper`. Each service
 * narrows that atom via `atomWithDefault` to a typed shape.
 */

import type {
	CallbackInboxStub,
	EntityRefStub,
	FxtrSessionAtom,
	OpResultStub,
	ResultStatus,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { type LoadResult, atomWithDefault, makeLoadResultAtom } from "@fxtr-ui/state-util";
import {
	type FullPanelProps,
	type HoverPreviewProps,
	type LinkProps,
	RefLink,
} from "@fxtr-ui/workstation";
import { useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, type ReactNode, useMemo, useState } from "react";

// ----------------------------------------------------------------------------
// Loaded-value types — the wire codec returns these shapes for our
// flat Python `DataclassEntity` types. Ref leaves decode to
// `EntityRefStub` instances.
// ----------------------------------------------------------------------------

interface DemoNoteValue {
	readonly title: string;
	readonly body: string;
}

interface DemoListNodeValue {
	readonly label: string;
	readonly next: EntityRefStub | null;
}

// ----------------------------------------------------------------------------
// Shared bits: status fallback + style tokens.
// ----------------------------------------------------------------------------

function renderLoadResult<T>(
	result: LoadResult<T>,
	whenFilled: (value: T) => ReactNode,
): ReactNode {
	if (result.kind === "loading") {
		return <span style={mutedStyle}>…</span>;
	}
	if (result.kind === "errored") {
		return <span style={errorStyle}>load failed: {result.message}</span>;
	}
	return whenFilled(result.value);
}

const pad = "var(--sp-5, 16px)";

const fullPanelBodyStyle: CSSProperties = {
	padding: pad,
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3, 12px)",
};

const titleStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 20px)",
	color: "var(--text-primary)",
};

const bodyTextStyle: CSSProperties = {
	margin: 0,
	lineHeight: 1.5,
	color: "var(--text-primary)",
};

const linkPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: "var(--sp-2)",
	padding: "var(--sp-1) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-raised)",
	fontSize: "var(--text-body-sm)",
};

const linkTagStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	letterSpacing: "0.05em",
};

const linkLabelStyle: CSSProperties = {
	color: "var(--text-primary)",
};

const hoverCardStyle: CSSProperties = {
	minWidth: 220,
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3, 12px)",
};

const hoverTitleStyle: CSSProperties = {
	fontWeight: 600,
	color: "var(--text-primary)",
};

const hoverBodyStyle: CSSProperties = {
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
	lineHeight: 1.4,
};

const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontSize: "var(--text-body-sm)",
};

const errorStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontSize: "var(--text-body-sm)",
};

// ----------------------------------------------------------------------------
// DemoNote
// ----------------------------------------------------------------------------

export const DemoNoteFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({ stub }) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<DemoNoteValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<DemoNoteValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return (
		<div className="fw-panel__body" style={fullPanelBodyStyle}>
			{renderLoadResult(result, (value) => (
				<>
					<h2 style={titleStyle}>{value.title}</h2>
					<p style={bodyTextStyle}>{value.body}</p>
				</>
			))}
		</div>
	);
};

export const DemoNoteBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<DemoNoteValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<DemoNoteValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>note</span>
			<span style={linkLabelStyle}>
				{renderLoadResult(result, (value) => linkText ?? value.title)}
			</span>
		</span>
	);
};

export const DemoNoteHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<DemoNoteValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<DemoNoteValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return (
		<div style={hoverCardStyle}>
			{renderLoadResult(result, (value) => {
				const excerpt = value.body.length > 160 ? `${value.body.slice(0, 160)}…` : value.body;
				return (
					<>
						<div style={hoverTitleStyle}>{value.title}</div>
						<div style={hoverBodyStyle}>{excerpt}</div>
					</>
				);
			})}
		</div>
	);
};

// ----------------------------------------------------------------------------
// DemoListNode
// ----------------------------------------------------------------------------

export const DemoListNodeFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<DemoListNodeValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<DemoListNodeValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return (
		<div className="fw-panel__body" style={fullPanelBodyStyle}>
			{renderLoadResult(result, (value) => (
				<>
					<h2 style={titleStyle}>{value.label}</h2>
					<div style={{ display: "flex", alignItems: "center", gap: "var(--sp-2, 8px)" }}>
						<span style={mutedStyle}>next →</span>
						{value.next !== null ? (
							<RefLink stub={value.next} as="inline" />
						) : (
							<span style={mutedStyle}>(end of list)</span>
						)}
					</div>
				</>
			))}
		</div>
	);
};

export const DemoListNodeBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<DemoListNodeValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<DemoListNodeValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>node</span>
			<span style={linkLabelStyle}>
				{renderLoadResult(result, (value) => linkText ?? value.label)}
			</span>
		</span>
	);
};

export const DemoListNodeHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<DemoListNodeValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<DemoListNodeValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return (
		<div style={hoverCardStyle}>
			{renderLoadResult(result, (value) => (
				<>
					<div style={hoverTitleStyle}>{value.label}</div>
					{value.next !== null ? (
						<div style={{ display: "flex", alignItems: "center", gap: "var(--sp-2, 8px)" }}>
							<span style={mutedStyle}>next →</span>
							<RefLink stub={value.next} as="inline" />
						</div>
					) : (
						<div style={mutedStyle}>(end of list)</div>
					)}
				</>
			))}
		</div>
	);
};

// ----------------------------------------------------------------------------
// Service-side shared bits.
// ----------------------------------------------------------------------------

/** Next `id` for a `{id: number}` list — `max + 1`. */
function nextId<T extends { readonly id: number }>(list: ReadonlyArray<T>): number {
	let max = 0;
	for (const e of list) if (e.id > max) max = e.id;
	return max + 1;
}

const serviceRootStyle: CSSProperties = {
	...fullPanelBodyStyle,
	gap: "var(--sp-4, 16px)",
};

const inputRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2, 8px)",
	flexWrap: "wrap",
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

const serviceSectionHeaderStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 18px)",
	color: "var(--text-primary)",
};

function ResultDisplay({
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
				<div style={mutedStyle}>pending…</div>
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

// ----------------------------------------------------------------------------
// EchoService
// ----------------------------------------------------------------------------

interface EchoInvokeSlot {
	readonly id: number;
	readonly label: string;
	readonly result: OpResultStub<unknown>;
}

interface EchoServiceStorage {
	readonly invokes: ReadonlyArray<EchoInvokeSlot>;
}

const ECHO_INITIAL: EchoServiceStorage = { invokes: [] };

export const EchoServiceFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const typedStorage = useMemo(
		// `ownStorage` is typed `FxtrSessionAtom<unknown>` (the
		// dispatcher hands renderers a typeless slot). This
		// renderer owns the slot's contents, so cast to its
		// concrete shape before lifting through `atomWithDefault`.
		() =>
			atomWithDefault<EchoServiceStorage>(
				ownStorage as FxtrSessionAtom<EchoServiceStorage | null>,
				ECHO_INITIAL,
			),
		[ownStorage],
	);
	const state = useAtomValue(typedStorage);
	const setState = useSetAtom(typedStorage);
	const [draft, setDraft] = useState("hello");

	const fire = (): void => {
		const value = draft;
		setState((prev, ctx) => ({
			invokes: [
				...prev.invokes,
				{
					id: nextId(prev.invokes),
					label: `echo(${JSON.stringify(value)})`,
					result: ctx.allocateInvoke(stub as ServiceRefStub<unknown>, "echo", [value]),
				},
			],
		}));
	};

	const clear = (): void => {
		setState({ invokes: [] });
	};

	return (
		<div className="fw-panel__body" style={serviceRootStyle}>
			<h2 style={serviceSectionHeaderStyle}>echo · {stub.shortId}</h2>
			<div style={inputRowStyle}>
				<input
					type="text"
					value={draft}
					onChange={(e) => setDraft(e.target.value)}
					style={textInputStyle}
					placeholder="value"
				/>
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
					<span style={mutedStyle}>no invokes yet — type a value and click "invoke echo"</span>
				) : (
					state.invokes
						.slice()
						.reverse()
						.map((slot) => <ResultDisplay key={slot.id} label={slot.label} stub={slot.result} />)
				)}
			</div>
		</div>
	);
};

export const EchoServiceBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>echo</span>
			<span style={linkLabelStyle}>{linkText ?? `service · ${stub.shortId}`}</span>
		</span>
	);
};

// ----------------------------------------------------------------------------
// CounterService
// ----------------------------------------------------------------------------

type CounterMethod = "increment" | "reset" | "value";

interface CounterInvokeSlot {
	readonly id: number;
	readonly label: string;
	readonly result: OpResultStub<unknown>;
}

interface CounterServiceStorage {
	readonly invokes: ReadonlyArray<CounterInvokeSlot>;
}

const COUNTER_INITIAL: CounterServiceStorage = { invokes: [] };

export const CounterServiceFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const typedStorage = useMemo(
		// `ownStorage` is typed `FxtrSessionAtom<unknown>` (the
		// dispatcher hands renderers a typeless slot). This
		// renderer owns the slot's contents, so cast to its
		// concrete shape before lifting through `atomWithDefault`.
		() =>
			atomWithDefault<CounterServiceStorage>(
				ownStorage as FxtrSessionAtom<CounterServiceStorage | null>,
				COUNTER_INITIAL,
			),
		[ownStorage],
	);
	const state = useAtomValue(typedStorage);
	const setState = useSetAtom(typedStorage);

	const fire = (method: CounterMethod): void => {
		setState((prev, ctx) => ({
			invokes: [
				...prev.invokes,
				{
					id: nextId(prev.invokes),
					label: `${method}()`,
					result: ctx.allocateInvoke(stub as ServiceRefStub<unknown>, method, []),
				},
			],
		}));
	};

	const clear = (): void => {
		setState({ invokes: [] });
	};

	return (
		<div className="fw-panel__body" style={serviceRootStyle}>
			<h2 style={serviceSectionHeaderStyle}>counter · {stub.shortId}</h2>
			<div style={inputRowStyle}>
				<button type="button" onClick={() => fire("increment")} style={buttonStyle}>
					increment
				</button>
				<button type="button" onClick={() => fire("reset")} style={buttonStyle}>
					reset
				</button>
				<button type="button" onClick={() => fire("value")} style={buttonStyle}>
					value
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
					<span style={mutedStyle}>no invokes yet — click a button above</span>
				) : (
					state.invokes
						.slice()
						.reverse()
						.map((slot) => <ResultDisplay key={slot.id} label={slot.label} stub={slot.result} />)
				)}
			</div>
		</div>
	);
};

export const CounterServiceBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>counter</span>
			<span style={linkLabelStyle}>{linkText ?? `service · ${stub.shortId}`}</span>
		</span>
	);
};

// ----------------------------------------------------------------------------
// NotifyNService
// ----------------------------------------------------------------------------

interface NotifyNFireSlot {
	readonly id: number;
	readonly label: string;
	readonly inbox: CallbackInboxStub<string>;
	readonly result: OpResultStub<unknown>;
}

interface NotifyNServiceStorage {
	readonly fires: ReadonlyArray<NotifyNFireSlot>;
}

const NOTIFY_N_INITIAL: NotifyNServiceStorage = { fires: [] };

export const NotifyNServiceFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const typedStorage = useMemo(
		// `ownStorage` is typed `FxtrSessionAtom<unknown>` (the
		// dispatcher hands renderers a typeless slot). This
		// renderer owns the slot's contents, so cast to its
		// concrete shape before lifting through `atomWithDefault`.
		() =>
			atomWithDefault<NotifyNServiceStorage>(
				ownStorage as FxtrSessionAtom<NotifyNServiceStorage | null>,
				NOTIFY_N_INITIAL,
			),
		[ownStorage],
	);
	const state = useAtomValue(typedStorage);
	const setState = useSetAtom(typedStorage);
	const [n, setN] = useState(3);
	const [prefix, setPrefix] = useState("msg");

	const fire = (): void => {
		const nCaptured = Math.max(0, Math.floor(n));
		const prefixCaptured = prefix;
		setState((prev, ctx) => {
			const inbox = ctx.allocateCallback<string>();
			const result = ctx.allocateInvoke(stub as ServiceRefStub<unknown>, "notify_n", [
				inbox.handle,
				nCaptured,
				prefixCaptured,
			]);
			return {
				fires: [
					...prev.fires,
					{
						id: nextId(prev.fires),
						label: `notify_n(handle, ${nCaptured}, ${JSON.stringify(prefixCaptured)})`,
						inbox,
						result,
					},
				],
			};
		});
	};

	const clear = (): void => {
		setState({ fires: [] });
	};

	return (
		<div className="fw-panel__body" style={serviceRootStyle}>
			<h2 style={serviceSectionHeaderStyle}>notify_n · {stub.shortId}</h2>
			<div style={inputRowStyle}>
				<label style={{ display: "flex", alignItems: "center", gap: "var(--sp-1, 4px)" }}>
					<span style={mutedStyle}>n</span>
					<input
						type="number"
						value={n}
						min={0}
						onChange={(e) => setN(Number.parseInt(e.target.value, 10) || 0)}
						style={{ ...textInputStyle, width: "6ch" }}
					/>
				</label>
				<label style={{ display: "flex", alignItems: "center", gap: "var(--sp-1, 4px)" }}>
					<span style={mutedStyle}>prefix</span>
					<input
						type="text"
						value={prefix}
						onChange={(e) => setPrefix(e.target.value)}
						style={textInputStyle}
					/>
				</label>
				<button type="button" onClick={fire} style={buttonStyle}>
					fire
				</button>
				<span style={{ flex: 1 }} />
				<button
					type="button"
					onClick={clear}
					style={buttonStyle}
					disabled={state.fires.length === 0}
				>
					clear history
				</button>
			</div>
			<div style={historyListStyle}>
				{state.fires.length === 0 ? (
					<span style={mutedStyle}>no fires yet — set n + prefix and click "fire"</span>
				) : (
					state.fires
						.slice()
						.reverse()
						.map((slot) => <NotifyNFireRow key={slot.id} slot={slot} />)
				)}
			</div>
		</div>
	);
};

function NotifyNFireRow({ slot }: { readonly slot: NotifyNFireSlot }) {
	const events = useAtomValue(slot.inbox.events);
	const inboxState = useAtomValue(slot.inbox.state);
	return (
		<div style={resultCardStyle}>
			<div style={resultLabelStyle}>
				{slot.label} · inbox {inboxState}
			</div>
			{events.length === 0 ? (
				<div style={mutedStyle}>(no events yet)</div>
			) : (
				<ul
					style={{
						margin: 0,
						paddingLeft: "var(--sp-4, 16px)",
						fontFamily: "var(--font-mono)",
						fontSize: "var(--text-body-sm)",
					}}
				>
					{events.map((ev, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: payloads aren't unique; index is fine for display
						<li key={i}>{ev}</li>
					))}
				</ul>
			)}
			<ResultDisplay label="result" stub={slot.result} />
		</div>
	);
}

export const NotifyNServiceBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>notify_n</span>
			<span style={linkLabelStyle}>{linkText ?? `service · ${stub.shortId}`}</span>
		</span>
	);
};
