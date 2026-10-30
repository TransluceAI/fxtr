/**
 * Dispatch handlers for SimpleComment: MarginNote, MainView, ReferenceLink.
 */

import type { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { Render } from "@fxtr/react-dispatch";
import type { SimpleComment } from "@fxtr/reports-entities";
import type { JsonValue } from "@fxtr/reports-react";
import { ReferenceLink, useReportNavigation } from "@fxtr/reports-react";
import { Suspense } from "react";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const marginNoteStyle: React.CSSProperties = {
	padding: "10px",
	borderRadius: "6px",
	backgroundColor: "#fff",
	border: "1px solid #e0e0e0",
	fontSize: "13px",
	lineHeight: 1.5,
};

const marginNoteHighlightedStyle: React.CSSProperties = {
	...marginNoteStyle,
	borderColor: "#0066cc",
	boxShadow: "0 0 0 1px #0066cc",
};

const authorStyle: React.CSSProperties = {
	fontWeight: 600,
	fontSize: "12px",
	color: "#666",
	marginBottom: "4px",
};

const commentTextStyle: React.CSSProperties = {
	color: "#333",
};

const mainViewStyle: React.CSSProperties = {
	maxWidth: "720px",
};

const targetLinkStyle: React.CSSProperties = {
	fontSize: "14px",
	color: "#666",
	marginBottom: "12px",
};

const linkStyle: React.CSSProperties = {
	color: "#0066cc",
	cursor: "pointer",
	textDecoration: "none",
	borderBottom: "1px solid transparent",
};

// ---------------------------------------------------------------------------
// MarginNote
// ---------------------------------------------------------------------------

export function SimpleCommentMarginNote({
	entityRef,
	highlighted,
}: {
	entityRef: EntityRef<SimpleComment>;
	highlighted?: boolean;
}) {
	const comment = useFxtrLoad(entityRef);

	return (
		<div style={highlighted ? marginNoteHighlightedStyle : marginNoteStyle}>
			<div style={authorStyle}>{comment.author}</div>
			<div style={commentTextStyle}>{comment.comment}</div>
		</div>
	);
}

// ---------------------------------------------------------------------------
// MainView
// ---------------------------------------------------------------------------

export function SimpleCommentMainView({
	entityRef,
}: {
	entityRef: EntityRef<SimpleComment>;
	navState?: JsonValue | undefined;
	annotations?: unknown[] | undefined;
}) {
	const comment = useFxtrLoad(entityRef);

	return (
		<div style={mainViewStyle}>
			<div style={targetLinkStyle}>
				Comment on:{" "}
				<Suspense fallback={<span>...</span>}>
					<Render entity={comment.target} as={ReferenceLink} />
				</Suspense>
			</div>
			<div
				style={{
					...marginNoteStyle,
					maxWidth: "400px",
				}}
			>
				<div style={authorStyle}>{comment.author}</div>
				<div style={commentTextStyle}>{comment.comment}</div>
			</div>
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function SimpleCommentReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<SimpleComment>;
	linkText?: string;
}) {
	const comment = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? `Comment by ${comment.author}`;

	return (
		<span
			style={linkStyle}
			onClick={() => nav.navigateTo(entityRef.entityId)}
			onKeyDown={(e) => {
				if (e.key === "Enter") nav.navigateTo(entityRef.entityId);
			}}
			role="link"
			tabIndex={0}
		>
			{text}
		</span>
	);
}
