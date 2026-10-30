/**
 * EntityLink — the user-facing component for rendering an entity as an
 * interactive link inside another view.
 *
 * Composes:
 *   - `<Render entity={id} as={BlockLink | InlineLink}>` for the display
 *     content (dispatched through the registry).
 *   - The four interaction hooks (hover preview, context menu, click,
 *     drag) around that content.
 *
 * Consumers who want pure display without interactions (e.g. the open-
 * panels sidebar, which owns its own click/focus behavior) render
 * `<Render entity={id} as={BlockLink} />` directly and skip this
 * component. Consumers with unusual layouts can instead compose the
 * individual hooks (`useDraggableEntity`, `useEntityClick`,
 * `useEntityHoverCard`, `useEntityContextMenu`) around their own
 * content.
 *
 * `as="inline"` renders a `<span>` so the link flows with surrounding
 * prose; `as="block"` renders a `<div>` that fills its row. The same
 * `as` is threaded into `useEntityHoverCard` so the PreviewCard trigger
 * has a real bounding box for popup positioning.
 */

import type { FxtrID } from "@fxtr/core";
import { Render } from "@fxtr/react-dispatch";
import type { CSSProperties, ReactNode } from "react";
import { useDraggableEntity } from "./interactions/useDraggableEntity.js";
import { useEntityClick } from "./interactions/useEntityClick.js";
import { useEntityContextMenu } from "./interactions/useEntityContextMenu.js";
import { useEntityHoverCard } from "./interactions/useEntityHoverCard.js";
import { BlockLink, InlineLink, type LinkProps } from "./keys.js";

export interface EntityLinkProps extends LinkProps {
	entity: FxtrID;
	as?: "inline" | "block";
}

const TITLE = "Click to navigate, right-click for menu, drag to open in new panel";

const BLOCK_STYLE: CSSProperties = {
	cursor: "pointer",
	userSelect: "none",
};

const INLINE_STYLE: CSSProperties = {
	cursor: "pointer",
	userSelect: "none",
};

export function EntityLink(props: EntityLinkProps): ReactNode {
	const { entity, as = "block", ...linkProps } = props;

	const HoverWrap = useEntityHoverCard(entity, as);
	const ContextMenuWrap = useEntityContextMenu(entity);
	const { onClick } = useEntityClick(entity);
	const { draggable, onDragStart } = useDraggableEntity(entity);

	const key = as === "inline" ? InlineLink : BlockLink;
	const commonProps = {
		onClick,
		draggable,
		onDragStart,
		title: TITLE,
	};

	const content = <Render entity={entity} as={key} {...linkProps} />;

	const inner =
		as === "inline" ? (
			<span {...commonProps} style={INLINE_STYLE}>
				{content}
			</span>
		) : (
			<div {...commonProps} style={BLOCK_STYLE}>
				{content}
			</div>
		);

	return (
		<HoverWrap>
			<ContextMenuWrap>{inner}</ContextMenuWrap>
		</HoverWrap>
	);
}
