/**
 * Renderer module for the `Photo` entity type. Sibling of
 * `../note/index.tsx`; see that file's header for the staging
 * mechanics and the dual-key-set rationale.
 */

import { BlockLink, FullPanel, HoverPreview, InlineLink } from "@fxtr-ui/workstation-dispatch";

// Keep in sync with `src/client/dispatch_keys.ts`.
const CARD_KEY_UUID = "00000000-0000-4000-8000-000000010001";

function PhotoCard(props: { readonly stub: { readonly shortId: string } }) {
	return (
		<div
			style={{
				padding: 12,
				borderRadius: 8,
				background: "#eaf5ff",
				border: "1px solid #a8c8e7",
				color: "#0a3a66",
			}}
		>
			<div style={{ fontWeight: 600, marginBottom: 4 }}>PhotoCard</div>
			<div style={{ fontSize: 11, color: "#3a6a99" }}>
				dynamically loaded — short id {props.stub.shortId}
			</div>
		</div>
	);
}

function PhotoFullPanel(props: { readonly stub: { readonly shortId: string } }) {
	return (
		<div
			style={{
				padding: 16,
				background: "#eaf5ff",
				color: "#0a3a66",
				height: "100%",
				overflow: "auto",
			}}
		>
			<h2 style={{ margin: 0, fontSize: 18 }}>Photo</h2>
			<div style={{ marginTop: 8, fontSize: 12, color: "#3a6a99" }}>
				dynamically loaded — short id {props.stub.shortId}
			</div>
			<p style={{ marginTop: 12 }}>
				This Photo FullPanel renderer was compiled at workstation startup and delivered to the
				frontend via the dynamic-dispatch wire.
			</p>
		</div>
	);
}

function PhotoBlockLink(props: { readonly stub: { readonly shortId: string } }) {
	return (
		<span
			style={{
				display: "inline-flex",
				alignItems: "center",
				gap: 6,
				padding: "2px 8px",
				borderRadius: 6,
				background: "#eaf5ff",
				border: "1px solid #a8c8e7",
				color: "#0a3a66",
				fontSize: 12,
			}}
		>
			<span style={{ fontWeight: 600 }}>Photo</span>
			<code style={{ fontSize: 11 }}>{props.stub.shortId}</code>
		</span>
	);
}

function PhotoInlineLink(props: { readonly stub: { readonly shortId: string } }) {
	return (
		<code
			style={{
				padding: "1px 4px",
				borderRadius: 4,
				background: "#eaf5ff",
				color: "#3a6a99",
				fontSize: 12,
			}}
		>
			photo:{props.stub.shortId}
		</code>
	);
}

function PhotoHoverPreview(props: { readonly stub: { readonly shortId: string } }) {
	return (
		<div
			style={{
				padding: 8,
				background: "#eaf5ff",
				border: "1px solid #a8c8e7",
				borderRadius: 6,
				color: "#0a3a66",
				fontSize: 12,
				minWidth: 160,
			}}
		>
			<div style={{ fontWeight: 600 }}>Photo hover</div>
			<div style={{ fontSize: 11, color: "#3a6a99", marginTop: 4 }}>{props.stub.shortId}</div>
		</div>
	);
}

export default {
	default: {
		[CARD_KEY_UUID]: PhotoCard,
		[FullPanel.uuid]: PhotoFullPanel,
		[BlockLink.uuid]: PhotoBlockLink,
		[InlineLink.uuid]: PhotoInlineLink,
		[HoverPreview.uuid]: PhotoHoverPreview,
	},
};
