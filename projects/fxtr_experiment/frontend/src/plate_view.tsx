/**
 * The plate view (design: `../notes/plate_view.md`): the grids drawn as a
 * plate-notation graph — a node per grid, containment/internals as nested
 * container boxes (ELK compound layout, `plate_layout.ts`), grant-class
 * plates as a colored overlay, and the four relationship families as typed
 * edges. Node fill encodes the grid **kind**; the border encodes intent
 * (targeted / grounding); counts + the config come from the workflow
 * summary mirror, the overlay (edges + grants + correspondences) from the
 * grid-graph mirror. Clicking a node navigates to its grid in the tree.
 *
 * This is the id-free, presence-class read path — no invokes here; the
 * layout recomputes when either mirror's structure changes.
 */

import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { statusBadges } from "./derive/badges.js";
import type { GraphMirror, WorkflowMirror } from "./derive/folds.js";
import { classifyRelations, grantClasses } from "./derive/plates.js";
import { buildGridTree } from "./derive/tree.js";
import type { RollupCounts } from "./derive/tree.js";
import {
	type EdgeStyle,
	type PlateLayout,
	type PositionedNode,
	type PositionedPlate,
	layoutPlates,
} from "./plate_layout.js";
import type { GridKind, GridSummary } from "./wire/types.js";

const CLASS_COLORS = [
	"#3b82f6",
	"#8b5cf6",
	"#ec4899",
	"#f59e0b",
	"#10b981",
	"#06b6d4",
	"#ef4444",
	"#84cc16",
];

const KIND_GLYPH: Record<GridKind, string> = {
	data: "▪",
	dataframe: "▦",
	handle: "⚭",
	task: "⚙",
	checkpoint: "⚑",
	subtree: "▤",
};

/** Node fill by grid kind (the primary encoding). */
const KIND_TINT: Record<GridKind, string> = {
	data: "rgba(59,130,246,0.12)", // blue
	dataframe: "rgba(99,102,241,0.12)", // indigo
	handle: "rgba(139,92,246,0.12)", // violet
	task: "rgba(16,185,129,0.14)", // green
	checkpoint: "rgba(148,163,184,0.16)", // slate
	subtree: "rgba(148,163,184,0.07)", // faint slate
};

function kindFill(s: GridSummary | null): string {
	return s ? KIND_TINT[s.kind] : "var(--fxtr-surface, #ffffff)";
}

/** Border encodes intent: targeted (blue) wins, then a grounding problem
 * (orphaned red / unanchored amber); a grounded, untargeted grid is
 * neutral. */
function nodeBorder(s: GridSummary | null): { stroke: string; width: number } {
	if (s?.targets_attached) return { stroke: "#2563eb", width: 1.8 };
	if (s?.grounding === "orphaned") return { stroke: "#ef4444", width: 1.4 };
	if (s?.grounding === "unanchored") return { stroke: "#f59e0b", width: 1.4 };
	return { stroke: "var(--fxtr-border, #ccd5df)", width: 1 };
}

/** The line-end marker: a filled arrowhead for dependencies (reads /
 * grants point INTO a task), a thin open arrowhead for cell writes, a
 * **circle** for grid creation — "this task was the creator", deliberately
 * not an arrowhead so it doesn't read as an input — and **none** for a
 * rail tie (an undirected membership line, not a dependency). */
type EdgeHead = "arrow" | "hollow" | "circle" | "none";

type EdgeVisual = {
	readonly stroke: string;
	readonly width: number;
	readonly dash: string | null;
	readonly head: EdgeHead;
};

const EDGE_VISUALS: Record<EdgeStyle, EdgeVisual> = {
	// Two tiers: **black** = the declared rule wiring (input grants —
	// solid when read, dashed when held-unread); **grey** = everything
	// merely observed at runtime (an incidental read the grants don't
	// cover, and the write / creation provenance).
	"grant-used": { stroke: "#1f2937", width: 1.6, dash: null, head: "arrow" },
	"grant-held": { stroke: "#1f2937", width: 1.4, dash: "5 4", head: "arrow" },
	read: { stroke: "#64748b", width: 1.4, dash: null, head: "arrow" },
	write: { stroke: "#64748b", width: 1.2, dash: null, head: "hollow" },
	create: { stroke: "#64748b", width: 1.6, dash: null, head: "circle" },
	// Rail ties (rail node → each box of its split class); the stroke is a
	// placeholder — the renderer recolors each tie with its class color.
	rail: { stroke: "#64748b", width: 1.4, dash: "2 5", head: "none" },
};

/** The populated total + per-status counts as coloured SVG segments —
 * same glyphs/colours as the tree (`derive/badges.ts`). Returns null when
 * there is nothing to show. */
function StatusText({
	agg,
	x,
	y,
	anchor = "start",
}: {
	readonly agg: RollupCounts;
	readonly x: number;
	readonly y: number;
	readonly anchor?: "start" | "end";
}): React.JSX.Element | null {
	const badges = statusBadges(agg);
	const showPop = agg.populated > 0;
	if (!showPop && badges.length === 0) return null;
	let first = true;
	const seg = (): number => {
		const dx = first ? 0 : 6;
		first = false;
		return dx;
	};
	return (
		<text x={x} y={y} fontSize={10} textAnchor={anchor}>
			{showPop ? (
				<tspan dx={seg()} fill="var(--fxtr-fg-muted, #64748b)">
					{agg.populated}
				</tspan>
			) : null}
			{badges.map((b) => (
				<tspan key={b.label} dx={seg()} fill={b.color}>
					{b.sym}
					{b.count}
				</tspan>
			))}
		</text>
	);
}

function header(s: GridSummary | null, key: string): string {
	// Axis labels live on the surrounding plates, not the node — the node
	// carries only grid/task identity.
	return `${s ? KIND_GLYPH[s.kind] : "▫"} ${key}`;
}

function polyline(points: readonly Pt[]): string {
	return points.map((p) => `${p.x},${p.y}`).join(" ");
}

type Pt = { readonly x: number; readonly y: number };

/** Drop bends within `min` px of the endpoint so the arrowhead sits on a
 * clean straight run (ELK sometimes elbows right at the target, putting a
 * corner under the arrowhead). The endpoint itself is always kept. */
function trimTail(points: readonly Pt[], min = 16): readonly Pt[] {
	const pts = [...points];
	while (pts.length >= 3) {
		const end = pts[pts.length - 1] as Pt;
		const prev = pts[pts.length - 2] as Pt;
		if (Math.hypot(end.x - prev.x, end.y - prev.y) < min) pts.splice(pts.length - 2, 1);
		else break;
	}
	return pts;
}

export function PlateView({
	workflow,
	graph,
	onSelect,
}: {
	readonly workflow: WorkflowMirror;
	readonly graph: GraphMirror;
	readonly onSelect: (path: string) => void;
}): React.JSX.Element {
	const tree = useMemo(
		() => (workflow.snapshotted ? buildGridTree(Object.values(workflow.rows)) : null),
		[workflow.snapshotted, workflow.rows],
	);
	const derived = useMemo(() => {
		if (graph.graph === null) return null;
		return {
			relations: classifyRelations(graph.graph),
			classes: grantClasses(graph.graph.correspondences),
		};
	}, [graph.graph]);

	const [layout, setLayout] = useState<PlateLayout | null>(null);
	const [error, setError] = useState<string | null>(null);

	useEffect(() => {
		if (tree === null || derived === null) return;
		let cancelled = false;
		layoutPlates(tree, derived.relations, derived.classes)
			.then((l) => {
				if (!cancelled) {
					setLayout(l);
					setError(null);
				}
			})
			.catch((e: unknown) => {
				if (!cancelled) setError(e instanceof Error ? e.message : String(e));
			});
		return () => {
			cancelled = true;
		};
	}, [tree, derived]);

	// -- pan / zoom (infinite-canvas): a transform on the content <g> --
	const [view, setView] = useState({ scale: 1, tx: 0, ty: 0 });
	const panRef = useRef<{ px: number; py: number; tx: number; ty: number } | null>(null);
	// True once a pointer-drag has actually moved — suppresses the click
	// that would otherwise select a node at the end of a pan.
	const pannedRef = useRef(false);
	// The edge whose slot label is showing (hovered); labels are on-demand.
	const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);

	// Wheel-to-zoom toward the cursor, via a non-passive listener (React's
	// onWheel is passive, so it can't preventDefault the page scroll).
	const attachWheel = useCallback((el: HTMLDivElement | null) => {
		if (el === null) return;
		const handler = (e: WheelEvent) => {
			e.preventDefault();
			const rect = el.getBoundingClientRect();
			const mx = e.clientX - rect.left;
			const my = e.clientY - rect.top;
			const factor = Math.exp(-e.deltaY * 0.0015);
			setView((v) => {
				const scale = Math.min(4, Math.max(0.15, v.scale * factor));
				const k = scale / v.scale;
				return { scale, tx: mx - k * (mx - v.tx), ty: my - k * (my - v.ty) };
			});
		};
		el.addEventListener("wheel", handler, { passive: false });
		return () => el.removeEventListener("wheel", handler);
	}, []);

	const onPointerDown = (e: React.PointerEvent): void => {
		if (e.button !== 0) return;
		panRef.current = { px: e.clientX, py: e.clientY, tx: view.tx, ty: view.ty };
		pannedRef.current = false;
	};
	const onPointerMove = (e: React.PointerEvent): void => {
		const p = panRef.current;
		if (p === null) return;
		const dx = e.clientX - p.px;
		const dy = e.clientY - p.py;
		if (!pannedRef.current && Math.hypot(dx, dy) < 3) return;
		pannedRef.current = true;
		setView((v) => ({ ...v, tx: p.tx + dx, ty: p.ty + dy }));
	};
	const endPan = (): void => {
		panRef.current = null;
	};

	if (!workflow.snapshotted || !graph.snapshotted) return <div style={mutedStyle}>loading…</div>;
	if (error !== null) return <div style={errorStyle}>plate layout failed: {error}</div>;
	if (layout === null) return <div style={mutedStyle}>laying out…</div>;
	if (layout.nodes.length === 0) return <div style={mutedStyle}>no grids yet</div>;

	// A stable color per grant-class (by sorted class key; internals
	// regions are not classes and get a neutral grey).
	const classKeys = [
		...new Set(layout.plates.filter((p) => p.variant === "class").map((p) => p.classKey)),
	].sort();
	const classColor = (k: string): string =>
		CLASS_COLORS[Math.max(0, classKeys.indexOf(k)) % CLASS_COLORS.length] as string;

	// All box-shaped nodes (axis plates, internals regions, subtree/task
	// containers), outermost first, so nested boxes draw over their parents.
	type Box =
		| { readonly kind: "plate"; readonly plate: PositionedPlate }
		| { readonly kind: "grid"; readonly node: PositionedNode };
	const boxes: Box[] = [
		...layout.plates.map((plate) => ({ kind: "plate" as const, plate })),
		...layout.nodes
			.filter((node) => node.container)
			.map((node) => ({ kind: "grid" as const, node })),
	].sort((a, b) => {
		const area = (x: Box): number =>
			x.kind === "plate" ? x.plate.width * x.plate.height : x.node.width * x.node.height;
		return area(b) - area(a);
	});

	const clickProps = (node: PositionedNode) =>
		node.summary
			? {
					style: { cursor: "pointer" } as CSSProperties,
					// Suppress the click that ends a pan-drag.
					onClick: () => {
						if (!pannedRef.current) onSelect(node.path);
					},
					onKeyDown: (e: React.KeyboardEvent) => {
						if (e.key === "Enter") onSelect(node.path);
					},
					role: "button" as const,
					tabIndex: 0,
				}
			: {};

	const pad = 16;
	return (
		<div
			ref={attachWheel}
			style={viewportStyle}
			onPointerDown={onPointerDown}
			onPointerMove={onPointerMove}
			onPointerUp={endPan}
			onPointerLeave={endPan}
		>
			<button
				type="button"
				style={resetBtnStyle}
				onClick={() => setView({ scale: 1, tx: 0, ty: 0 })}
			>
				reset view
			</button>
			<svg
				width="100%"
				height="100%"
				style={{ display: "block", fontFamily: "inherit", touchAction: "none" }}
				aria-label="plate view"
			>
				<title>plate view</title>
				<defs>
					{(Object.keys(EDGE_VISUALS) as EdgeStyle[]).map((s) => {
						const v = EDGE_VISUALS[s];
						if (v.head === "none") return null;
						return (
							<marker
								key={s}
								id={`arrow-${s}`}
								viewBox="0 0 10 10"
								// The circle is centred on the line end; arrowheads sit
								// with their tip at it.
								refX={v.head === "circle" ? "5" : "9"}
								refY="5"
								markerWidth="9"
								markerHeight="9"
								orient="auto-start-reverse"
							>
								{v.head === "circle" ? (
									<circle
										cx="5"
										cy="5"
										r="3.4"
										fill="var(--fxtr-surface, #ffffff)"
										stroke={v.stroke}
										strokeWidth={1.4}
									/>
								) : v.head === "hollow" ? (
									<path
										d="M1,1 L9,5 L1,9"
										fill="none"
										stroke={v.stroke}
										strokeWidth={1.5}
										strokeLinejoin="round"
										strokeLinecap="round"
									/>
								) : (
									<path d="M0,0 L10,5 L0,10 z" fill={v.stroke} />
								)}
							</marker>
						);
					})}
				</defs>
				<g transform={`translate(${view.tx},${view.ty}) scale(${view.scale})`}>
					<g transform={`translate(${pad},${pad})`}>
						{/* boxes: axis plates, internals regions, task/subtree
					    containers — outer first, so nested boxes draw on top */}
						{boxes.map((b) => {
							if (b.kind === "grid") {
								return (
									<ContainerBox
										key={`c-${b.node.path}`}
										node={b.node}
										clickProps={clickProps(b.node)}
									/>
								);
							}
							if (b.plate.variant === "rail") {
								// The rail node: the anchor a split class's boxes tie to,
								// styled like the class's plates so it reads as "the axis".
								const color = classColor(b.plate.classKey);
								return (
									<g key={b.plate.id}>
										<rect
											x={b.plate.x}
											y={b.plate.y}
											width={b.plate.width}
											height={b.plate.height}
											rx={10}
											fill={color}
											fillOpacity={0.12}
											stroke={color}
											strokeOpacity={0.55}
											strokeDasharray="6 4"
										/>
										<text
											x={b.plate.x + b.plate.width / 2}
											y={b.plate.y + b.plate.height / 2 + 4}
											fontSize={11}
											textAnchor="middle"
											fill={color}
											fontWeight={600}
										>
											{b.plate.label}
										</text>
									</g>
								);
							}
							const internals = b.plate.variant === "internals";
							const color = internals
								? "var(--fxtr-border, #ccd5df)"
								: classColor(b.plate.classKey);
							return (
								<g key={b.plate.id}>
									<rect
										x={b.plate.x}
										y={b.plate.y}
										width={b.plate.width}
										height={b.plate.height}
										rx={internals ? 6 : 10}
										fill={internals ? "var(--fxtr-fg, #1a2b3c)" : color}
										fillOpacity={internals ? 0.03 : 0.05}
										stroke={color}
										strokeOpacity={internals ? 0.9 : 0.55}
										strokeDasharray={internals ? "4 3" : "6 4"}
									/>
									{internals ? (
										<text
											x={b.plate.x + 7}
											y={b.plate.y + 13}
											fontSize={9}
											fill="var(--fxtr-fg-muted, #94a3b8)"
											fontStyle="italic"
										>
											internals
										</text>
									) : (
										<text
											x={b.plate.x + 8}
											y={b.plate.y + 15}
											fontSize={11}
											fill={color}
											fontWeight={600}
										>
											{b.plate.label}
										</text>
									)}
								</g>
							);
						})}

						{/* edges */}
						{layout.edges.map((e) => {
							const v = EDGE_VISUALS[e.style];
							// A rail tie is drawn in its class's color; every other
							// family has a fixed stroke.
							const stroke = e.style === "rail" ? classColor(e.classKey) : v.stroke;
							const pts = trimTail(e.points);
							const end = pts[pts.length - 1];
							// The slot label (a grant's arg name) shows on hover, just
							// OUTSIDE the target node — backed off the arrowhead along
							// the incoming segment, so it never collides with node text.
							let label = null;
							if (e.label !== "" && hoveredEdge === e.id && end !== undefined) {
								const prev = pts[pts.length - 2] ?? end;
								const dx = end.x - prev.x;
								const dy = end.y - prev.y;
								const len = Math.hypot(dx, dy) || 1;
								const lx = end.x - (dx / len) * 18;
								const ly = end.y - (dy / len) * 18;
								const w = e.label.length * 6 + 8;
								label = (
									<g pointerEvents="none">
										<rect
											x={lx - w / 2}
											y={ly - 8}
											width={w}
											height={15}
											rx={3}
											fill="var(--fxtr-surface, #ffffff)"
											stroke={v.stroke}
											strokeOpacity={0.45}
										/>
										<text x={lx} y={ly + 3} fontSize={9} textAnchor="middle" fill={v.stroke}>
											{e.label}
										</text>
									</g>
								);
							}
							return (
								<g
									key={e.id}
									onPointerEnter={() => e.label !== "" && setHoveredEdge(e.id)}
									onPointerLeave={() => setHoveredEdge((h) => (h === e.id ? null : h))}
								>
									{/* fat invisible hit line so the thin edge is easy to hover */}
									<polyline
										points={polyline(pts)}
										fill="none"
										stroke="transparent"
										strokeWidth={12}
									/>
									<polyline
										points={polyline(pts)}
										fill="none"
										stroke={stroke}
										strokeWidth={v.width}
										strokeDasharray={v.dash ?? undefined}
										markerEnd={v.head === "none" ? undefined : `url(#arrow-${e.style})`}
									/>
									{label}
								</g>
							);
						})}

						{/* leaf nodes (on top) */}
						{layout.nodes
							.filter((nd) => !nd.container)
							.map((nd) => (
								<LeafNode key={nd.path} node={nd} clickProps={clickProps(nd)} />
							))}
					</g>
				</g>
			</svg>
		</div>
	);
}

type ClickProps = Record<string, unknown>;

/** A subtree / task grid with children: a kind-colored box with its
 * header + count badge. (A task's internals sit in their own dashed
 * "internals" plate container, laid out inside this box.) */
function ContainerBox({
	node,
	clickProps,
}: {
	readonly node: PositionedNode;
	readonly clickProps: ClickProps;
}): React.JSX.Element {
	const border = nodeBorder(node.summary);
	return (
		<g>
			<rect
				x={node.x}
				y={node.y}
				width={node.width}
				height={node.height}
				rx={8}
				fill={kindFill(node.summary)}
				fillOpacity={0.5}
				stroke={border.stroke}
				strokeWidth={border.width}
			/>
			<text
				x={node.x + 10}
				y={node.y + 18}
				fontSize={12}
				fontWeight={600}
				fill="var(--fxtr-fg, #1a2b3c)"
				{...clickProps}
			>
				{header(node.summary, node.key)}
			</text>
			<StatusText agg={node.agg} x={node.x + node.width - 10} y={node.y + 18} anchor="end" />
		</g>
	);
}

function LeafNode({
	node,
	clickProps,
}: {
	readonly node: PositionedNode;
	readonly clickProps: ClickProps;
}): React.JSX.Element {
	const border = nodeBorder(node.summary);
	return (
		<g transform={`translate(${node.x},${node.y})`} {...clickProps}>
			<rect
				width={node.width}
				height={node.height}
				rx={8}
				fill={kindFill(node.summary)}
				stroke={border.stroke}
				strokeWidth={border.width}
			/>
			<text x={10} y={20} fontSize={12} fontWeight={600} fill="var(--fxtr-fg, #1a2b3c)">
				{header(node.summary, node.key)}
			</text>
			<StatusText agg={node.agg} x={10} y={node.height - 10} />
		</g>
	);
}

const viewportStyle: CSSProperties = {
	flex: 1,
	minHeight: 0,
	overflow: "hidden",
	position: "relative",
	cursor: "grab",
	touchAction: "none",
};
const resetBtnStyle: CSSProperties = {
	position: "absolute",
	right: 8,
	bottom: 8,
	zIndex: 1,
	padding: "2px 8px",
	fontSize: 11,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 6,
	background: "var(--fxtr-surface, #ffffff)",
	color: "var(--fxtr-fg, #1a2b3c)",
	cursor: "pointer",
};
const mutedStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic", fontSize: 12, marginTop: 8 };
const errorStyle: CSSProperties = { color: "#c04040", fontSize: 12, padding: 8 };
