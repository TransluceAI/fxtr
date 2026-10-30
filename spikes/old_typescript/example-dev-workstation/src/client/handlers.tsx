/**
 * Demo dispatch handlers.
 *
 * - `DemoNote` has FullPanel, BlockLink, HoverPreview (InlineLink falls
 *   back to workstation-core's default).
 * - `DemoSection` has a FullPanel handler that lists its items as
 *   BlockLinks — the main surface for exercising link interactions.
 * - `DemoArchive` has no handlers at all, so opening it exercises the
 *   JsonTree FullPanel fallback (with the two nested section IDs
 *   rendered as interactive inline-link fallbacks).
 * - `DemoListNode` has FullPanel, BlockLink, HoverPreview. The
 *   HoverPreview embeds an `<EntityLink>` to `next` so nested-hover
 *   drags traverse the linked list — a good stress-test for the
 *   breadcrumb history model.
 */

import {
	BlockLink,
	EntityLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type LinkProps,
} from "@fxtr-workstation/core";
import type { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { type RegistryEntry, entry } from "@fxtr/react-dispatch";
import { Suspense, useEffect } from "react";
import { DemoListNode, DemoNote, DemoSection } from "../entities.js";

const pad = "var(--sp-5, 16px)";

// ----------------------------------------------------------------------------
// DemoNote
// ----------------------------------------------------------------------------

function DemoNoteFullPanel(props: { entityRef: EntityRef<DemoNote> } & FullPanelProps) {
	const note = useFxtrLoad(props.entityRef);
	const { setPanelTitle } = props;
	useEffect(() => {
		setPanelTitle(note.title);
	}, [setPanelTitle, note.title]);
	return (
		<div
			className="fw-panel__body"
			style={{
				padding: pad,
				overflow: "auto",
				display: "flex",
				flexDirection: "column",
				gap: "var(--sp-3, 12px)",
			}}
		>
			<h2 style={{ margin: 0, fontSize: "var(--text-heading)", color: "var(--text-primary)" }}>
				{note.title}
			</h2>
			<p style={{ margin: 0, lineHeight: 1.5, color: "var(--text-primary)" }}>{note.body}</p>
		</div>
	);
}

function DemoNoteBlockLink(props: { entityRef: EntityRef<DemoNote> } & LinkProps) {
	const note = useFxtrLoad(props.entityRef);
	return (
		<span
			style={{
				display: "inline-flex",
				alignItems: "center",
				gap: "var(--sp-2)",
				padding: "var(--sp-1) var(--sp-3)",
				border: "1px solid var(--border-subtle)",
				borderRadius: "var(--radius-badge)",
				background: "var(--surface-raised)",
				fontSize: "var(--text-body-sm)",
			}}
		>
			<span
				style={{
					textTransform: "uppercase",
					fontSize: "var(--text-caption)",
					color: "var(--text-muted)",
					letterSpacing: "0.05em",
				}}
			>
				note
			</span>
			<span style={{ color: "var(--text-primary)" }}>{props.linkText ?? note.title}</span>
		</span>
	);
}

function DemoNoteHoverPreview(props: { entityRef: EntityRef<DemoNote> }) {
	const note = useFxtrLoad(props.entityRef);
	const excerpt = note.body.length > 160 ? `${note.body.slice(0, 160)}…` : note.body;
	return (
		<div style={{ minWidth: "220px" }}>
			<div
				style={{
					fontWeight: 600,
					color: "var(--text-primary)",
					marginBottom: "var(--sp-2)",
				}}
			>
				{note.title}
			</div>
			<div style={{ color: "var(--text-secondary)", lineHeight: "var(--lh-compact)" }}>
				{excerpt}
			</div>
		</div>
	);
}

// ----------------------------------------------------------------------------
// DemoSection
// ----------------------------------------------------------------------------

function DemoSectionFullPanel(props: { entityRef: EntityRef<DemoSection> } & FullPanelProps) {
	const section = useFxtrLoad(props.entityRef);
	const { setPanelTitle } = props;
	useEffect(() => {
		setPanelTitle(section.title);
	}, [setPanelTitle, section.title]);

	return (
		<div
			className="fw-panel__body"
			style={{
				padding: pad,
				overflow: "auto",
				display: "flex",
				flexDirection: "column",
				gap: "var(--sp-4)",
			}}
		>
			<header style={{ display: "flex", flexDirection: "column", gap: "var(--sp-2)" }}>
				<h2
					style={{
						margin: 0,
						fontSize: "var(--text-heading)",
						color: "var(--text-primary)",
					}}
				>
					{section.title}
				</h2>
				<p
					style={{
						margin: 0,
						color: "var(--text-secondary)",
						lineHeight: "var(--lh-comfortable)",
					}}
				>
					{section.summary}
				</p>
			</header>
			<div
				style={{
					display: "flex",
					flexDirection: "column",
					gap: "var(--sp-2)",
				}}
			>
				<div
					style={{
						fontSize: "var(--text-caption)",
						textTransform: "uppercase",
						letterSpacing: "0.05em",
						color: "var(--text-muted)",
					}}
				>
					Items ({section.items.length})
				</div>
				<ul
					style={{
						listStyle: "none",
						padding: 0,
						margin: 0,
						display: "flex",
						flexDirection: "column",
						gap: "var(--sp-2)",
					}}
				>
					{section.items.map((ref) => (
						<li key={ref.entityId.idString()}>
							<Suspense fallback={<span style={{ color: "var(--text-muted)" }}>…</span>}>
								<EntityLink entity={ref.entityId} as="block" />
							</Suspense>
						</li>
					))}
				</ul>
			</div>
		</div>
	);
}

// DemoSection HoverPreview deliberately embeds a nested EntityLink so
// you can exercise the nested-hover logic: hovering a section's
// preview card opens the popup; then hovering the first note's
// EntityLink inside that popup opens *its* hover card on top of the
// section's, and both stay open while the cursor moves between them.
function DemoSectionHoverPreview(props: { entityRef: EntityRef<DemoSection> }) {
	const section = useFxtrLoad(props.entityRef);
	const firstItem = section.items[0];
	return (
		<div style={{ minWidth: "240px", maxWidth: "320px" }}>
			<div
				style={{
					fontWeight: 600,
					color: "var(--text-primary)",
					marginBottom: "var(--sp-2)",
				}}
			>
				{section.title}
			</div>
			<div
				style={{
					color: "var(--text-secondary)",
					lineHeight: "var(--lh-compact)",
					marginBottom: "var(--sp-3)",
				}}
			>
				{section.summary}
			</div>
			{firstItem ? (
				<div
					style={{
						display: "flex",
						alignItems: "center",
						gap: "var(--sp-2)",
						paddingTop: "var(--sp-2)",
						borderTop: "1px solid var(--border-subtle)",
					}}
				>
					<span
						style={{
							fontSize: "var(--text-caption)",
							textTransform: "uppercase",
							letterSpacing: "0.05em",
							color: "var(--text-muted)",
						}}
					>
						first item
					</span>
					<Suspense fallback={<span style={{ color: "var(--text-muted)" }}>…</span>}>
						<EntityLink entity={firstItem.entityId} as="block" />
					</Suspense>
				</div>
			) : null}
		</div>
	);
}

// ----------------------------------------------------------------------------
// DemoListNode
// ----------------------------------------------------------------------------

function DemoListNodeFullPanel(props: { entityRef: EntityRef<DemoListNode> } & FullPanelProps) {
	const node = useFxtrLoad(props.entityRef);
	const { setPanelTitle } = props;
	useEffect(() => {
		setPanelTitle(node.label);
	}, [setPanelTitle, node.label]);
	return (
		<div
			className="fw-panel__body"
			style={{
				padding: pad,
				overflow: "auto",
				display: "flex",
				flexDirection: "column",
				gap: "var(--sp-4)",
			}}
		>
			<h2 style={{ margin: 0, fontSize: "var(--text-heading)", color: "var(--text-primary)" }}>
				{node.label}
			</h2>
			<div style={{ display: "flex", alignItems: "center", gap: "var(--sp-2)" }}>
				<span
					style={{
						fontSize: "var(--text-caption)",
						textTransform: "uppercase",
						letterSpacing: "0.05em",
						color: "var(--text-muted)",
					}}
				>
					next
				</span>
				{node.next ? (
					<Suspense fallback={<span style={{ color: "var(--text-muted)" }}>…</span>}>
						<EntityLink entity={node.next.entityId} as="block" />
					</Suspense>
				) : (
					<span style={{ color: "var(--text-muted)" }}>(end of list)</span>
				)}
			</div>
		</div>
	);
}

function DemoListNodeBlockLink(props: { entityRef: EntityRef<DemoListNode> } & LinkProps) {
	const node = useFxtrLoad(props.entityRef);
	return (
		<span
			style={{
				display: "inline-flex",
				alignItems: "center",
				gap: "var(--sp-2)",
				padding: "var(--sp-1) var(--sp-3)",
				border: "1px solid var(--border-subtle)",
				borderRadius: "var(--radius-badge)",
				background: "var(--surface-raised)",
				fontSize: "var(--text-body-sm)",
			}}
		>
			<span
				style={{
					textTransform: "uppercase",
					fontSize: "var(--text-caption)",
					color: "var(--text-muted)",
					letterSpacing: "0.05em",
				}}
			>
				chain
			</span>
			<span style={{ color: "var(--text-primary)" }}>{props.linkText ?? node.label}</span>
		</span>
	);
}

// Nested `next` EntityLink inside the preview lets you hover a chain-
// node preview, then hover its `next` preview, etc. — stacking hovers
// deep enough that dragging out exercises the breadcrumb trail beyond
// a single segment.
function DemoListNodeHoverPreview(props: { entityRef: EntityRef<DemoListNode> }) {
	const node = useFxtrLoad(props.entityRef);
	return (
		<div style={{ minWidth: "200px" }}>
			<div
				style={{
					fontWeight: 600,
					color: "var(--text-primary)",
					marginBottom: "var(--sp-2)",
				}}
			>
				{node.label}
			</div>
			<div
				style={{
					display: "flex",
					alignItems: "center",
					gap: "var(--sp-2)",
					paddingTop: "var(--sp-2)",
					borderTop: "1px solid var(--border-subtle)",
				}}
			>
				<span
					style={{
						fontSize: "var(--text-caption)",
						textTransform: "uppercase",
						letterSpacing: "0.05em",
						color: "var(--text-muted)",
					}}
				>
					next
				</span>
				{node.next ? (
					<Suspense fallback={<span style={{ color: "var(--text-muted)" }}>…</span>}>
						<EntityLink entity={node.next.entityId} as="block" />
					</Suspense>
				) : (
					<span style={{ color: "var(--text-muted)" }}>(end)</span>
				)}
			</div>
		</div>
	);
}

// ----------------------------------------------------------------------------
// Registry
// ----------------------------------------------------------------------------

// biome-ignore lint/suspicious/noExplicitAny: registry entries hold heterogeneous codecs/components
export const demoEntries: RegistryEntry<any, any>[] = [
	entry(FullPanel, DemoNote, DemoNoteFullPanel),
	entry(BlockLink, DemoNote, DemoNoteBlockLink),
	entry(HoverPreview, DemoNote, DemoNoteHoverPreview),
	entry(FullPanel, DemoSection, DemoSectionFullPanel),
	entry(HoverPreview, DemoSection, DemoSectionHoverPreview),
	entry(FullPanel, DemoListNode, DemoListNodeFullPanel),
	entry(BlockLink, DemoListNode, DemoListNodeBlockLink),
	entry(HoverPreview, DemoListNode, DemoListNodeHoverPreview),
];
