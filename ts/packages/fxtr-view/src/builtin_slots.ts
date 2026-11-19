// The built-in slots: the panel, the inline link, and the hover preview for entities; the panel
// for a value still being produced (a step's live preview); the panel for invocations; and pages. Panels receive a storage atom for their state; entity views receive the one id
// they show, invocation panels every spawn they show.

import type { ComponentType } from "react";
import type { PrimitiveAtom } from "jotai";
import type { JsonValue } from "./ids";
import type { Spawn } from "./invocation";
import type { ViewParams } from "./link";
import { renderer, type DeclaredViewProps, type Renderer, type ViewDecl } from "./renderer";
import { declareViewSlot, type EntityViewProps, type InvocationViewProps, type ValueViewProps } from "./slots";

/** A panel's persistent per-mount state. JSON only, so a shell may persist it. */
export type StorageAtom = PrimitiveAtom<JsonValue>;

/** Props of an `EntityPanel` view: the entity shown, per-mount storage, and the params it
 *  declares that are bound where it is opened (all optional: a plain link to the entity binds
 *  none). A host that binds none leaves `params` out. */
export interface EntityPanelProps extends EntityViewProps {
  readonly storage: StorageAtom;
  readonly params?: ViewParams;
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

/** Props of a `PartialEntityPanel` view: the value a step is producing, whether it is still
 *  partial, whether the step is still running, and per-mount storage. */
export interface PartialEntityPanelProps extends ValueViewProps {
  /** Whether the value is still being produced. A partial value's fields not yet known are null,
   *  whatever their type says, and a list may be shorter than it will be; a renderer that reads a
   *  field unconditionally must treat null as "not yet". Which parts inside it are still being
   *  produced, `isPartial` says. False for a finished value the step published as its preview. */
  readonly partial: boolean;
  /** Whether the step is still running, so the value may still change. False for the last preview
   *  of a step that failed or stopped: a partial value then will never be finished, and a renderer
   *  should not say it is in progress. */
  readonly live: boolean;
  readonly storage: StorageAtom;
}

/** The full body of a panel showing a value a step is still producing: its latest preview
 *  (`ctx.preview` in Python), which is not a stored entity. The host dispatches on the value's
 *  type name, so a type's renderer for this slot shows its streaming values the way its
 *  `EntityPanel` renderer shows its finished ones; registering one is how a renderer opts in to
 *  partial values. A type with none gets the slot's wildcard, the same data tree a finished
 *  entity's raw view shows. */
export const PartialEntityPanel = declareViewSlot<PartialEntityPanelProps>("PartialEntityPanel");

/** Props of a `PartialEntityInline` view: a `PartialEntityPanel` view's, without storage. */
export type PartialEntityInlineProps = Omit<PartialEntityPanelProps, "storage">;

/** A one-line summary of a value a step is still producing, flowing with surrounding text
 *  (`<span>`): what a host shows in a running invocation's table row, where a finished one shows
 *  its result's `EntityInlineLink`, so it says how far the value has got ("5 responses so far").
 *  Static phrasing content, like `EntityInlineLink`. A type with none gets the slot's wildcard, its
 *  type name and that it is partial. */
export const PartialEntityInline = declareViewSlot<PartialEntityInlineProps>("PartialEntityInline");

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
