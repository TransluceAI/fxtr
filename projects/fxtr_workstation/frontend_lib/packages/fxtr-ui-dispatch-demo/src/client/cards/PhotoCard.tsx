/**
 * Rendered for the demo's `Photo` entity type. Loads the
 * `caption` + `url` and shows the image alongside the caption.
 */

import type { EntityRefStub, OpResultStub } from "@fxtr-ui/core";
import { useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { makeLoadResultAtom } from "@fxtr-ui/state-util";
import { useAtomValue } from "jotai";
import { useMemo } from "react";

interface PhotoData {
	readonly caption: string;
	readonly url: string;
}

export function PhotoCard({ stub }: { readonly stub: EntityRefStub }) {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<PhotoData> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<PhotoData>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const load = useAtomValue(resultAtom);
	return (
		<div
			style={{
				border: "1px solid #d5c5e5",
				borderRadius: "6px",
				padding: "10px 12px",
				background: "#faf5ff",
				display: "flex",
				flexDirection: "column",
				gap: "4px",
			}}
		>
			<div style={{ fontSize: "10px", color: "#558", fontFamily: "monospace" }}>
				Photo · {stub.shortId}
			</div>
			{load.kind === "filled" ? (
				<>
					<img
						src={load.value.url}
						alt={load.value.caption}
						style={{
							width: "100%",
							maxHeight: "180px",
							objectFit: "cover",
							borderRadius: "4px",
							background: "#eee",
						}}
					/>
					<div style={{ fontSize: "12px", color: "#444" }}>{load.value.caption}</div>
				</>
			) : load.kind === "errored" ? (
				<div style={{ color: "#c00" }}>load failed: {load.message}</div>
			) : (
				<div style={{ color: "#888", fontStyle: "italic" }}>loading photo…</div>
			)}
		</div>
	);
}
