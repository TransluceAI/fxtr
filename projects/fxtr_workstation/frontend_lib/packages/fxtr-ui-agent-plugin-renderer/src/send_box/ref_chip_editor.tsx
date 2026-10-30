/**
 * ``<RefChipEditor>`` — CodeMirror 6 wrapper used by the send-box.
 *
 * Plain text is the source of truth: literal ``@[shortid]`` tokens
 * mark refs in the document. A ``ViewPlugin`` decorates each match
 * with a ``RefChipWidget`` whose DOM placeholder is mounted into
 * the editor; the React layer reads the widget registry and renders
 * a portal-based ``<RefLink as="inline">`` into each placeholder so
 * the chip inherits ``RefLinkImplContext`` (shell-supplied drag /
 * hover / click handlers).
 *
 * Drop handling: the editor's outer wrapper div spreads
 * ``useRefDropRegion().regionProps`` so the shell's
 * ``DockviewLayout`` skips its drop overlay while a drag is over
 * the editor. CodeMirror's own ``domEventHandlers`` for ``drop`` /
 * ``dragover`` decode ``application/x-fxtr-ref`` payloads and
 * insert the literal ``@[shortid]`` text at the drop position; the
 * decoration plugin picks the new text up automatically.
 *
 * Controlled component contract: parent passes ``value`` /
 * ``onChange``. When the parent's ``value`` differs from the
 * editor's current doc, a transaction replaces the entire doc — the
 * same pattern ``<textarea>`` consumers use. Internally the
 * component dispatches updates synchronously on user typing.
 */

import { Prec } from "@codemirror/state";
import {
	type DOMEventHandlers,
	Decoration,
	type DecorationSet,
	EditorView,
	type KeyBinding,
	ViewPlugin,
	type ViewUpdate,
	keymap,
} from "@codemirror/view";
import { DRAG_MIME_FXTR_REF, parseRefDragData } from "@fxtr-ui/workstation-dispatch";
import {
	type CSSProperties,
	type DragEvent as ReactDragEvent,
	type ReactNode,
	useCallback,
	useEffect,
	useLayoutEffect,
	useRef,
	useState,
} from "react";

import { RefChipWidget, widgetRegistry } from "./chip_widget.js";
import { REF_TOKEN_RE } from "./parse_refs.js";

const wrapperBaseStyle: CSSProperties = {
	flex: 1,
	minHeight: "2.5em",
	maxHeight: "10em",
	overflowY: "auto",
	padding: "var(--sp-2) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-input)",
	background: "var(--surface-sunken)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-primary)",
	cursor: "text",
	transition: "border-color 0.15s, background-color 0.15s",
};

const wrapperDragOverStyle: CSSProperties = {
	borderColor: "var(--text-accent, #2563eb)",
	background: "var(--surface-accent-soft, var(--surface-sunken))",
};

const editorThemeExtension = EditorView.theme({
	"&": {
		backgroundColor: "transparent",
		color: "inherit",
		fontFamily: "inherit",
		fontSize: "inherit",
	},
	".cm-content": {
		caretColor: "var(--text-primary)",
		padding: 0,
		fontFamily: "inherit",
	},
	".cm-line": {
		padding: 0,
	},
	".cm-scroller": {
		fontFamily: "inherit",
	},
	"&.cm-focused": {
		outline: "none",
	},
});

const placeholderTheme = EditorView.theme({
	".cm-placeholder": {
		color: "var(--text-muted)",
		fontStyle: "italic",
	},
});

/**
 * Build a ``ViewPlugin`` that scans the visible viewport for
 * ``@[shortid]`` matches and emits a ``Decoration.replace`` widget
 * for each one. The plugin runs on construction, on doc changes,
 * and on viewport changes (so chips render correctly after
 * scrolling through long messages — not a concern at the agent
 * panel's max-height of ``10em`` but cheap to handle).
 */
function createRefChipDecorationPlugin() {
	return ViewPlugin.fromClass(
		class {
			decorations: DecorationSet;
			constructor(view: EditorView) {
				this.decorations = this.build(view);
			}
			update(update: ViewUpdate): void {
				if (update.docChanged || update.viewportChanged) {
					this.decorations = this.build(update.view);
				}
			}
			private build(view: EditorView): DecorationSet {
				const builder: { from: number; to: number; decoration: Decoration }[] = [];
				const doc = view.state.doc.toString();
				const re = new RegExp(REF_TOKEN_RE.source, "g");
				let m: RegExpExecArray | null;
				// biome-ignore lint/suspicious/noAssignInExpressions: idiomatic exec loop
				while ((m = re.exec(doc)) !== null) {
					const shortId = m[1];
					if (shortId === undefined) continue; // capture group always matches
					const from = m.index;
					const to = from + m[0].length;
					builder.push({
						from,
						to,
						decoration: Decoration.replace({ widget: new RefChipWidget(shortId) }),
					});
				}
				return Decoration.set(
					builder.map((b) => b.decoration.range(b.from, b.to)),
					true,
				);
			}
		},
		{
			decorations: (v) => v.decorations,
		},
	);
}

/**
 * Render-time props for the chip portals. The editor publishes a
 * snapshot of the widget registry on every change; the consumer's
 * portal-renderer callback turns each entry into a React node.
 */
export interface ChipRenderProps {
	readonly id: string;
	readonly element: HTMLElement;
	readonly shortId: string;
}

export interface RefChipEditorProps {
	readonly value: string;
	readonly onChange: (next: string) => void;
	readonly onSubmit: () => void;
	readonly placeholder?: string;
	readonly disabled?: boolean;
	/**
	 * Spread onto the wrapper element to coordinate with the shell's
	 * drop-region context. The send-box passes through
	 * ``useRefDropRegion().regionProps``.
	 */
	readonly regionProps?: {
		readonly onDragEnter: (e: ReactDragEvent) => void;
		readonly onDragLeave: (e: ReactDragEvent) => void;
		readonly onDrop: (e: ReactDragEvent) => void;
	};
	/**
	 * Render one chip's React subtree. The editor exposes the
	 * registry snapshot; the consumer chooses what to draw inside
	 * each placeholder (typically a ``<RefLink as="inline">``).
	 */
	readonly renderChip: (props: ChipRenderProps) => ReactNode;
}

interface InternalState {
	readonly view: EditorView;
	readonly externalOnChangeRef: { current: (next: string) => void };
	readonly externalOnSubmitRef: { current: () => void };
}

/**
 * Build the editor's static extension list. ``onChange`` /
 * ``onSubmit`` are read through refs so changing the prop value
 * doesn't require rebuilding the editor.
 */
function buildExtensions(
	externalOnChangeRef: { current: (next: string) => void },
	externalOnSubmitRef: { current: () => void },
) {
	const submitBinding: KeyBinding = {
		key: "Enter",
		run: () => {
			externalOnSubmitRef.current();
			return true;
		},
	};
	const updateListener = EditorView.updateListener.of((vu) => {
		if (vu.docChanged) {
			externalOnChangeRef.current(vu.state.doc.toString());
		}
	});
	const dom: DOMEventHandlers<unknown> = {
		dragover: (e) => {
			if (e.dataTransfer?.types.includes(DRAG_MIME_FXTR_REF)) {
				e.preventDefault();
				e.dataTransfer.dropEffect = "copy";
				return true;
			}
			return false;
		},
		drop: (e, view) => {
			if (!e.dataTransfer) return false;
			const data = parseRefDragData(e.dataTransfer);
			if (data === null) return false;
			e.preventDefault();
			const pos = view.posAtCoords({ x: e.clientX, y: e.clientY }, false) ?? view.state.doc.length;
			const insert = `@[${data.shortId}]`;
			view.dispatch({
				changes: { from: pos, to: pos, insert },
				selection: { anchor: pos + insert.length },
			});
			return true;
		},
	};
	return [
		Prec.high(keymap.of([submitBinding])),
		EditorView.contentAttributes.of({}),
		EditorView.lineWrapping,
		editorThemeExtension,
		placeholderTheme,
		createRefChipDecorationPlugin(),
		EditorView.domEventHandlers(dom),
		updateListener,
	];
}

/**
 * Mount the editor into ``parent``. Returns the EditorView instance
 * + a setter that pushes new ``value`` into the doc when the parent
 * controlled prop changes externally.
 */
function mountEditor(
	parent: HTMLElement,
	initialValue: string,
	externalOnChangeRef: { current: (next: string) => void },
	externalOnSubmitRef: { current: () => void },
): InternalState {
	const view = new EditorView({
		parent,
		doc: initialValue,
		extensions: buildExtensions(externalOnChangeRef, externalOnSubmitRef),
	});
	return { view, externalOnChangeRef, externalOnSubmitRef };
}

export function RefChipEditor(props: RefChipEditorProps) {
	const { value, onChange, onSubmit, placeholder, disabled, regionProps, renderChip } = props;
	const parentRef = useRef<HTMLDivElement | null>(null);
	const stateRef = useRef<InternalState | null>(null);
	const onChangeRef = useRef(onChange);
	const onSubmitRef = useRef(onSubmit);
	onChangeRef.current = onChange;
	onSubmitRef.current = onSubmit;

	// Local drag-over flag used to highlight the editor wrapper while
	// a fxtr-ref drag is hovering. Separate from the shared
	// `RefDropRegionContext` flag so we only light up *this* region
	// even when multiple regions exist on screen.
	const [isDragOver, setIsDragOver] = useState(false);

	// Mount / unmount the editor exactly once. We avoid re-creating
	// on prop changes — instead, the controlled-value sync below
	// dispatches doc transactions, and refs handle the callbacks.
	// `value` is read through a ref so the effect deps stay empty.
	const initialValueRef = useRef(value);
	useLayoutEffect(() => {
		const parent = parentRef.current;
		if (parent === null) return;
		const state = mountEditor(parent, initialValueRef.current, onChangeRef, onSubmitRef);
		stateRef.current = state;
		return () => {
			state.view.destroy();
			stateRef.current = null;
		};
	}, []);

	// Sync external value into the editor when the parent's
	// controlled prop differs from the current doc. Without this,
	// programmatic clears (e.g. after a successful send) wouldn't
	// flush back into the editor.
	useEffect(() => {
		const state = stateRef.current;
		if (state === null) return;
		const current = state.view.state.doc.toString();
		if (current === value) return;
		state.view.dispatch({
			changes: { from: 0, to: state.view.state.doc.length, insert: value },
		});
	}, [value]);

	// Track widget registry changes so the portal layer rerenders
	// whenever a chip is added / removed.
	const [snapshot, setSnapshot] = useState(() => widgetRegistry.snapshot());
	useEffect(() => {
		return widgetRegistry.subscribe(() => {
			setSnapshot(widgetRegistry.snapshot());
		});
	}, []);

	// Wrap the parent's regionProps so we can drive the local
	// drag-over highlight alongside the shared counter bump. Order:
	// our handler runs first, then we forward to the consumer's.
	const handleDragEnter = useCallback(
		(e: ReactDragEvent) => {
			if (!disabled && e.dataTransfer?.types.includes(DRAG_MIME_FXTR_REF)) {
				setIsDragOver(true);
			}
			regionProps?.onDragEnter(e);
		},
		[disabled, regionProps],
	);
	const handleDragLeave = useCallback(
		(e: ReactDragEvent) => {
			setIsDragOver(false);
			regionProps?.onDragLeave(e);
		},
		[regionProps],
	);
	const handleDrop = useCallback(
		(e: ReactDragEvent) => {
			setIsDragOver(false);
			regionProps?.onDrop(e);
		},
		[regionProps],
	);

	// Placeholder text. CodeMirror has a `placeholder` extension but
	// for our use it's cleaner to render an overlaid span when the
	// doc is empty — keeps the extension list small.
	const showPlaceholder = value.length === 0 && placeholder !== undefined && !isDragOver;

	const wrapperStyle: CSSProperties = isDragOver
		? { ...wrapperBaseStyle, ...wrapperDragOverStyle }
		: wrapperBaseStyle;

	return (
		<div
			style={{ position: "relative", flex: 1, display: "flex" }}
			onDragEnter={handleDragEnter}
			onDragLeave={handleDragLeave}
			onDrop={handleDrop}
		>
			<div ref={parentRef} style={wrapperStyle} aria-disabled={disabled ? "true" : undefined} />
			{showPlaceholder ? (
				<span
					style={{
						position: "absolute",
						left: "calc(var(--sp-3) + 1px)",
						top: "calc(var(--sp-2) + 1px)",
						color: "var(--text-muted)",
						fontStyle: "italic",
						fontFamily: "var(--font-prose)",
						fontSize: "var(--text-body-sm)",
						pointerEvents: "none",
					}}
				>
					{placeholder}
				</span>
			) : null}
			<RegistryPortals snapshot={snapshot} renderChip={renderChip} />
		</div>
	);
}

import { createPortal } from "react-dom";

/**
 * Render the React portals for each currently-registered chip. The
 * editor's ``RefChipWidget`` placeholders are pure DOM nodes; this
 * component sees them via the registry snapshot and mounts the
 * consumer's render output into each one with ``createPortal``.
 *
 * The portal target is keyed by the widget's stable id, so React
 * doesn't churn through mount/unmount when the registry snapshot
 * shuffles between renders.
 */
function RegistryPortals(props: {
	snapshot: ReadonlyArray<[string, import("./chip_widget.js").RegisteredWidget]>;
	renderChip: (props: ChipRenderProps) => ReactNode;
}) {
	const { snapshot, renderChip } = props;
	return (
		<>
			{snapshot.map(([id, w]) =>
				createPortal(renderChip({ id, element: w.element, shortId: w.shortId }), w.element, id),
			)}
		</>
	);
}
