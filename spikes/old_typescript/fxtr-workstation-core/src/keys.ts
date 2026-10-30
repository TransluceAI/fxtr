import { type DispatchKey, declareDispatchKey } from "@fxtr/react-dispatch";

export type SetPanelTitle = (title: string) => void;

export interface FullPanelProps {
	setPanelTitle: SetPanelTitle;
}

/**
 * Extra props accepted by `BlockLink` / `InlineLink` dispatch entries.
 * These dispatch keys render the display content of an entity as a
 * reference from elsewhere; `linkText` lets callers override the
 * renderer's default label.
 */
export interface LinkProps {
	linkText?: string;
}

// HoverPreview takes no extra props beyond `entityRef`; we let the
// DispatchKey default to `object`. Using a stricter empty type
// (`Record<string, never>`) would make `<Render entity={id} as={HoverPreview} />`
// itself fail to type-check, because `RenderProps` intersects extra props
// with `{ entity, as }`.
export type HoverPreviewProps = object;

export const FullPanel: DispatchKey<FullPanelProps> =
	declareDispatchKey<FullPanelProps>("FullPanel");
export const BlockLink: DispatchKey<LinkProps> = declareDispatchKey<LinkProps>("BlockLink");
export const InlineLink: DispatchKey<LinkProps> = declareDispatchKey<LinkProps>("InlineLink");
export const HoverPreview: DispatchKey = declareDispatchKey("HoverPreview");
