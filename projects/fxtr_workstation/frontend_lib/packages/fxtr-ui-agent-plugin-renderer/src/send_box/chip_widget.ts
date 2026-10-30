/**
 * CodeMirror 6 widget that visually replaces an ``@[shortid]``
 * token with a styled chip, plus a module-local registry the
 * ``<RefChipEditor>`` React layer subscribes to so it can render
 * portal-based ``<RefLink>``s into each chip's container.
 *
 * Mirrors clydra's pattern: the widget owns the DOM placeholder
 * (created in ``toDOM`` and registered in the module-scoped
 * registry), and the surrounding React tree subscribes to the
 * registry to mount React content into each placeholder via
 * ``createPortal``. This keeps React context (and especially
 * ``RefLinkImplContext``) available inside chips that live inside
 * an otherwise React-agnostic CodeMirror view.
 */

import { WidgetType } from "@codemirror/view";

let widgetIdCounter = 0;

export interface RegisteredWidget {
	readonly element: HTMLElement;
	readonly shortId: string;
}

type Listener = () => void;

class WidgetRegistry {
	private readonly entries = new Map<string, RegisteredWidget>();
	private readonly listeners = new Set<Listener>();

	register(id: string, element: HTMLElement, shortId: string): void {
		this.entries.set(id, { element, shortId });
		this.notify();
	}

	unregister(id: string): void {
		if (this.entries.delete(id)) {
			this.notify();
		}
	}

	subscribe(fn: Listener): () => void {
		this.listeners.add(fn);
		return () => {
			this.listeners.delete(fn);
		};
	}

	snapshot(): ReadonlyArray<[string, RegisteredWidget]> {
		return Array.from(this.entries.entries());
	}

	private notify(): void {
		for (const fn of this.listeners) fn();
	}
}

/**
 * Module-local singleton: all ``RefChipWidget`` instances register
 * their DOM placeholders here, and the ``<RefChipEditor>`` React
 * layer subscribes to the registry to mount portals.
 *
 * Lifecycle is bound to widget construction / destruction: when
 * CodeMirror destroys a widget (decoration removed or editor torn
 * down), the widget's ``destroy`` callback unregisters its entry,
 * which fires the subscribers and prompts React to clean up the
 * portal. When an editor is unmounted entirely, every widget's
 * ``destroy`` runs as part of ``EditorView.destroy()``, so the
 * registry shrinks back to empty.
 */
export const widgetRegistry = new WidgetRegistry();

/**
 * Decoration widget for one ``@[shortid]`` token. The widget's DOM
 * is just a styled inline-block ``<span>`` placeholder; the actual
 * chip content is rendered through a React portal by
 * ``<RefChipEditor>``.
 *
 * ``eq`` compares on ``shortId`` only — same shortId at the same
 * position is the same widget, so React's portal layer doesn't
 * thrash through mount/unmount when the user edits surrounding
 * text.
 */
export class RefChipWidget extends WidgetType {
	private readonly id: string;

	constructor(readonly shortId: string) {
		super();
		widgetIdCounter += 1;
		this.id = `fxtr-ref-chip-${widgetIdCounter}`;
	}

	toDOM(): HTMLElement {
		const span = document.createElement("span");
		span.className = "fxtr-ref-chip";
		span.id = this.id;
		// Inline-block so CodeMirror's caret hops over the chip as
		// a single character. The actual visual styling lives in
		// the portal-rendered content (RefLink).
		span.style.display = "inline-block";
		span.style.verticalAlign = "middle";
		widgetRegistry.register(this.id, span, this.shortId);
		return span;
	}

	override destroy(_dom: HTMLElement): void {
		widgetRegistry.unregister(this.id);
	}

	override eq(other: WidgetType): boolean {
		return other instanceof RefChipWidget && other.shortId === this.shortId;
	}

	override ignoreEvent(): boolean {
		// Forward click / drag / hover events on the chip DOM up
		// the tree so RefLink's interaction handlers fire (drag-out,
		// click-to-navigate, etc.). Without this, CodeMirror
		// swallows clicks on widget DOM.
		return false;
	}
}
