/**
 * Generic fallback used both as:
 *   - the initial `componentAtom` value before the dispatcher
 *     resolves the type (state machine in `init` /
 *     `loading-entity` / `loading-type-metadata`);
 *   - the terminal value when the resolved type-id has no
 *     matching entry in the registry's `lookupMap`.
 *
 * For debugging the dispatch flow, the fallback also surfaces
 * the full dispatch state-machine state — current phase, the
 * progress message, and the contents of any in-flight
 * `OpResultStub` (load slot, metadata slot). When a dispatch
 * gets stuck, this exposes whether the underlying slot is
 * pending, errored, or filled (and what value).
 */

import type { EntityRefStub, OpResultStub, ResultStatus, ServiceRefStub } from "@fxtr-ui/core";
import {
	type StaticEntityDispatchState,
	type StaticServiceDispatchState,
	useDispatchProgressMessage,
	useDispatchProgressState,
} from "@fxtr-ui/react-dispatch";
import { stringifyForDebug } from "@fxtr-ui/state-util";
import { useAtomValue } from "jotai";

// Combined-state view: the fallback is the same component for
// entity + service dispatches, so it has to inspect either
// shape. We narrow each phase explicitly below.
type AnyDispatchState = StaticEntityDispatchState | StaticServiceDispatchState;

export function CardFallback({
	stub,
}: {
	readonly stub: EntityRefStub | ServiceRefStub;
}) {
	const progress = useDispatchProgressMessage();
	const state = useDispatchProgressState<AnyDispatchState>();

	return (
		<div
			style={{
				border: "1px dashed #ccc",
				borderRadius: "6px",
				padding: "10px 12px",
				background: "#fafafa",
				color: "#444",
				display: "flex",
				flexDirection: "column",
				gap: "4px",
				fontSize: "11px",
				fontFamily: "monospace",
			}}
		>
			<div style={{ color: "#888" }}>? · {stub.shortId}</div>
			<div>
				<strong>phase:</strong> {state.phase}
			</div>
			<div>
				<strong>progress:</strong> {progress || "(none)"}
			</div>
			{state.phase === "loading-entity" ? <OpResultDebug label="load" stub={state.load} /> : null}
			{state.phase === "loading-type-metadata" ? (
				<>
					{"typeRef" in state ? (
						// Only present on the entity-side state machine
						// (the service-side jumps straight from `init` to
						// `loading-type-metadata` without an intermediate
						// type-ref).
						<div>
							<strong>typeRef:</strong> {state.typeRef.shortId}
						</div>
					) : null}
					<OpResultDebug label="meta" stub={state.meta} />
				</>
			) : null}
			{state.phase === "resolved" ? (
				<div>
					<strong>typeIdString:</strong> {state.typeIdString} (no entry in lookup map)
				</div>
			) : null}
			{state.phase === "errored" ? (
				<div style={{ color: "#c00" }}>
					<strong>error:</strong> {state.message}
				</div>
			) : null}
		</div>
	);
}

/**
 * Inline display of an `OpResultStub`'s reactive state: its
 * `result` status (pending / filled / errored), the `committed`
 * flag, and — for filled results — a JSON-ified peek at the
 * value. Useful for diagnosing where a stuck dispatch is in
 * the load → metadata pipeline.
 */
function OpResultDebug({
	label,
	stub,
}: {
	readonly label: string;
	readonly stub: OpResultStub<unknown>;
}) {
	const result: ResultStatus<unknown> = useAtomValue(stub.result);
	const committed = useAtomValue(stub.committed);
	return (
		<div
			style={{
				border: "1px solid #e0e0e0",
				borderRadius: "3px",
				padding: "4px 6px",
				background: "#fff",
				display: "flex",
				flexDirection: "column",
				gap: "2px",
			}}
		>
			<div>
				<strong>{label}:</strong> {result.status} · {committed ? "committed" : "uncommitted"}
			</div>
			{result.status === "errored" ? (
				<div style={{ color: "#c00" }}>{result.message}</div>
			) : result.status === "filled" ? (
				<pre
					style={{
						margin: 0,
						padding: "3px 5px",
						background: "#f5f5f5",
						borderRadius: "2px",
						maxHeight: "180px",
						overflow: "auto",
						whiteSpace: "pre-wrap",
						wordBreak: "break-all",
					}}
				>
					{stringifyForDebug(result.value, 2)}
				</pre>
			) : null}
		</div>
	);
}
