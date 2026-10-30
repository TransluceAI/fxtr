/**
 * Service card for the demo's `counter` service. Increments
 * via an invoke; the most recent value is kept in the
 * `useDispatchSharedAtom` so the displayed number survives
 * remounts and is shared across multiple mounts of the same
 * card. Reset zeroes the counter on the server side.
 *
 * Demonstrates shared-state preservation: the
 * `lastInvoke.result` slot is in the per-`(stub, key)` shared
 * atom, so two `<RenderDispatch>` instances for the same
 * counter service both see the same most-recent value.
 */

import type { Method, OpResultStub, ResultStatus, ServiceRefStub } from "@fxtr-ui/core";
import { useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { atom, useAtomValue, useSetAtom } from "jotai";
import { useMemo } from "react";

interface CounterServiceProto {
	readonly increment: Method<readonly [], Record<string, never>, number>;
	readonly reset: Method<readonly [], Record<string, never>, number>;
	readonly value: Method<readonly [], Record<string, never>, number>;
}

interface SharedSlot {
	readonly lastInvoke: OpResultStub<number> | null;
}

const INITIAL_SLOT: SharedSlot = { lastInvoke: null };

export function CounterServiceCard({
	stub,
}: {
	readonly stub: ServiceRefStub<CounterServiceProto> | ServiceRefStub<unknown>;
}) {
	const sharedAtom = useDispatchSharedAtom<SharedSlot>(INITIAL_SLOT);
	const shared = useAtomValue(sharedAtom);
	const setShared = useSetAtom(sharedAtom);

	const resultAtom = useMemo(
		() =>
			shared.lastInvoke !== null
				? shared.lastInvoke.result
				: atom<ResultStatus<number> | null>(null),
		[shared.lastInvoke],
	);
	const result = useAtomValue(resultAtom);

	const callMethod = (method: "increment" | "reset" | "value"): void => {
		setShared((_prev, ctx) => ({
			lastInvoke: ctx.allocateInvoke(stub as ServiceRefStub<CounterServiceProto>, method, []),
		}));
	};

	return (
		<div
			style={{
				border: "1px solid #e5d5c5",
				borderRadius: "6px",
				padding: "10px 12px",
				background: "#fff8f0",
				display: "flex",
				flexDirection: "column",
				gap: "6px",
			}}
		>
			<div style={{ fontSize: "10px", color: "#558", fontFamily: "monospace" }}>
				CounterService · {stub.shortId}
			</div>
			<div style={{ fontSize: "24px", textAlign: "center", fontWeight: 600, color: "#553" }}>
				{result === null
					? "?"
					: result.status === "pending"
						? "…"
						: result.status === "errored"
							? "err"
							: String(result.value)}
			</div>
			<div style={{ display: "flex", gap: "6px", justifyContent: "center" }}>
				<button
					type="button"
					onClick={() => callMethod("increment")}
					style={{ padding: "3px 8px", fontSize: "12px" }}
				>
					+1
				</button>
				<button
					type="button"
					onClick={() => callMethod("reset")}
					style={{ padding: "3px 8px", fontSize: "12px" }}
				>
					reset
				</button>
				<button
					type="button"
					onClick={() => callMethod("value")}
					style={{ padding: "3px 8px", fontSize: "12px" }}
				>
					refresh
				</button>
			</div>
			{result?.status === "errored" && (
				<div style={{ fontSize: "11px", color: "#c00" }}>error: {result.message}</div>
			)}
		</div>
	);
}
