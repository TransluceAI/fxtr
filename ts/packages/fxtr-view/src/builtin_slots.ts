// The built-in slots: the panel, the inline link, and the hover preview for entities; the panel
// for invocations; and pages. Panels receive a storage atom for their state; entity views receive the one id
// they show, invocation panels every spawn they show.

import type { ComponentType } from "react";
import type { PrimitiveAtom } from "jotai";
import type { JsonValue } from "./ids";
import type { Spawn } from "./invocation";
import type { ViewParams } from "./link";
import { renderer, type DeclaredViewProps, type Renderer, type ViewDecl } from "./renderer";
import { declareViewSlot, type EntityViewProps, type InvocationViewProps } from "./slots";

/** A panel's persistent per-mount state. JSON only, so a shell may persist it. */
export type StorageAtom = PrimitiveAtom<JsonValue>;

/** Props of an `EntityPanel` view: the entity shown and per-mount storage. */
export interface EntityPanelProps extends EntityViewProps {
  readonly storage: StorageAtom;
}

/** Props of a link view: the id alone. */
export type LinkProps = EntityViewProps;

/** Props of a hover preview: the id alone. A shell shows the preview as the main content of a
 *  card that opens when a link is hovered, and may add content of its own below it. */
export type HoverPreviewProps = EntityViewProps;

/** The full body of a panel showing the entity. */
export const EntityPanel = declareViewSlot<EntityPanelProps>("EntityPanel");
/** A reference to the entity, flowing with surrounding text (`<span>`). The shell renders it as the
 *  content of its own interactive link chrome (a button, a drag source), so it must be static
 *  phrasing content: no `EntityLink`, no buttons or other interactive elements. */
export const EntityInlineLink = declareViewSlot<LinkProps>("EntityInlineLink");
/** A compact preview of the entity, shown when a link to it is hovered. */
export const EntityHoverPreview = declareViewSlot<HoverPreviewProps>("EntityHoverPreview");

/** Props of an invocation panel: the spawns shown and per-mount storage, like an entity
 *  `EntityPanel`. */
export interface InvocationPanelProps extends InvocationViewProps {
  readonly storage: StorageAtom;
  /** The params the renderer declares, bound where the view is opened. A host that binds none
   *  leaves it out. */
  readonly params?: ViewParams;
}

/** The full view of the invocations a host's selection picks out of a run, one or many (the
 *  frontend's step panel), or of a whole workflow as an alternative to its trace (given its one
 *  invocation). A renderer handles every count it can be given: several invocations get a view of
 *  their own (an aggregate, a grid, a section each), not nothing. */
export const InvocationPanel = declareViewSlot<InvocationPanelProps>("InvocationPanel");

/** Props of a page: the run it belongs to (the job's root workflow invocation, for the page to
 *  fetch what it shows from), the params it declares, bound where it is opened, and per-mount
 *  storage. */
export interface PagePanelProps {
  readonly run: Spawn;
  readonly params: ViewParams;
  readonly storage: StorageAtom;
}

/** A page: a view with no subject, reached by its name. It shows whatever it fetches from the run,
 *  often from several steps: a results overview, a custom chart. */
export const PagePanel = declareViewSlot<PagePanelProps>("PagePanel");

/** Declare a page named `name` (its subject type and id both), reading `params`; `within` another
 *  page, it is a detail of that one, its name below that one's (`dashboard/proposition`). */
export function declarePage<Params extends string = never>(
  name: string,
  options: { readonly within?: ViewDecl<PagePanelProps>; readonly params?: readonly Params[] } = {},
): ViewDecl<PagePanelProps, Params> {
  const within = options.within ?? null;
  const id = within === null ? name : `${within.id}/${name}`;
  return Object.freeze({ slot: PagePanel, subjectType: id, id, params: Object.freeze([...(options.params ?? [])]), within: within?.id ?? null });
}

/** A page's renderer: of a declared page, whose component is handed the params it declares, or of
 *  a page named `name` reading no params. */
export function page<Params extends string = never>(
  view: ViewDecl<PagePanelProps, Params> | string,
  Component: ComponentType<NoInfer<DeclaredViewProps<PagePanelProps, Params>>>,
): Renderer<PagePanelProps> {
  // The declared params are the component's to read; the renderer is registered under the slot's props.
  const declared = (typeof view === "string" ? declarePage(view) : view) as ViewDecl<PagePanelProps, never>;
  return renderer(declared, Component as ComponentType<PagePanelProps>);
}
