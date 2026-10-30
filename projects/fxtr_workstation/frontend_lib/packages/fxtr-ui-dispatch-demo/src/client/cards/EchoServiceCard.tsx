/**
 * Service card for the demo's `echo` service. Type a string,
 * click "echo", round-trip it through the server. The last
 * invoke's result stub lives in `useDispatchSharedAtom` so
 * remounting the card (or rendering it twice on the page)
 * preserves the last answer.
 */

import type {
	Method,
	OpResultStub,
	ResultStatus,
	ResultValue,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { atom, useAtomValue, useSetAtom } from "jotai";
import { useMemo, useState } from "react";

interface EchoServiceProto {
	readonly echo: Method<readonly [string], Record<string, never>, ResultValue>;
}

interface SharedSlot {
	readonly lastInvoke: OpResultStub<ResultValue> | null;
}

const INITIAL_SLOT: SharedSlot = { lastInvoke: null };

export function EchoServiceCard({
	stub,
}: {
	readonly stub: ServiceRefStub<EchoServiceProto> | ServiceRefStub<unknown>;
}) {
	const [input, setInput] = useState("hello");

	const sharedAtom = useDispatchSharedAtom<SharedSlot>(INITIAL_SLOT);
	const shared = useAtomValue(sharedAtom);
	const setShared = useSetAtom(sharedAtom);

	// Derive a stable atom over the slot's `result` (or a
	// sentinel-null atom while no invoke has been issued) so we
	// can call `useAtomValue` unconditionally. This card is
	// user-action-driven (no mount-time load), so the
	// `makeLoadResultAtom` helper doesn't apply here.
	const resultAtom = useMemo(
		() =>
			shared.lastInvoke !== null
				? shared.lastInvoke.result
				: atom<ResultStatus<ResultValue> | null>(null),
		[shared.lastInvoke],
	);
	const result = useAtomValue(resultAtom);

	const handleEcho = (): void => {
		setShared((_prev, ctx) => ({
			lastInvoke: ctx.allocateInvoke(stub as ServiceRefStub<EchoServiceProto>, "echo", [input]),
		}));
	};

	return (
		<div
			style={{
				border: "1px solid #c5e5d5",
				borderRadius: "6px",
				padding: "10px 12px",
				background: "#f5fff8",
				display: "flex",
				flexDirection: "column",
				gap: "6px",
			}}
		>
			<div style={{ fontSize: "10px", color: "#558", fontFamily: "monospace" }}>
				EchoService · {stub.shortId}
			</div>
			<div style={{ display: "flex", gap: "6px" }}>
				<input
					type="text"
					value={input}
					onChange={(e) => setInput(e.target.value)}
					style={{ flex: 1, padding: "3px 6px", fontSize: "12px" }}
				/>
				<button type="button" onClick={handleEcho} style={{ padding: "3px 8px", fontSize: "12px" }}>
					echo
				</button>
			</div>
			{result === null ? (
				<div style={{ fontSize: "11px", color: "#888", fontStyle: "italic" }}>(no invokes yet)</div>
			) : result.status === "pending" ? (
				<div style={{ fontSize: "11px", color: "#888", fontStyle: "italic" }}>pending…</div>
			) : result.status === "errored" ? (
				<div style={{ fontSize: "11px", color: "#c00" }}>error: {result.message}</div>
			) : (
				<div style={{ fontSize: "11px", color: "#444", fontFamily: "monospace" }}>
					echoed: <strong>{JSON.stringify(result.value)}</strong>
				</div>
			)}
		</div>
	);
}
