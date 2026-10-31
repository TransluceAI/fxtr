// Renderers: what a renderer package exports. Each is one component for one slot and one subject
// type (an entity type name, a step / workflow name, a page's name, or `ANY_SUBJECT_TYPE` for a
// renderer that works for every subject of the slot), with an id a host remembers a choice by. A
// package exports a list of them; a host gathers the lists into whatever it dispatches through
// (fxtr-view-shell-util's `DispatchRegistry`). Nothing here dispatches.
//
// A view another view links to is **declared** apart from its component (`declareView`), and the
// link names it by that declaration (`useLink`), so a view's module imports other views'
// declarations, never their components, and a typo in a view's name or params is a type error. A
// detail view (a chart's point) is declared `within` the view that links to it: its id is the
// parent's id, `/`, and its own name, and only the parent links to it.

import type { ComponentType } from "react";
import type { LinkKeyValue } from "./link";
import type { ViewSlot } from "./slots";

/** The subject type of a renderer that fits every subject of its slot (the cbor-tree views of any
 *  entity). A host offers such a renderer after a subject type's own, so it is the default only
 *  for a subject type with none of its own. Type names and registered names are identifiers, so
 *  none is spelled this way. */
export const ANY_SUBJECT_TYPE = "*";

/** A view as links name it: its slot, subject type and id, the params it reads, and the view it is
 *  declared within (null for none). `Params` are the names of the params it reads, which a link
 *  to it must bind. */
export interface ViewDecl<Props extends object, Params extends string = string> {
  readonly slot: ViewSlot<Props>;
  readonly subjectType: string;
  readonly id: string;
  readonly params: readonly Params[];
  /** The id of the view it is declared within, which alone links to it; null for none. */
  readonly within: string | null;
}

/** The params a view declaring `Params` is handed: each of them, bound. */
export type BoundParams<Params extends string> = { readonly [Name in Params]: LinkKeyValue };

/** The props of a declared view's component: its slot's, and when it declares params, `params`
 *  binding each of them (a host shows the view only once they are all bound). */
export type DeclaredViewProps<Props extends object, Params extends string> = [Params] extends [never]
  ? Props
  : Props & { readonly params: BoundParams<Params> };

/** The props of the component of the declared view `View`: `ViewProps<typeof SCORE_FIT>`. */
export type ViewProps<View extends AnyViewDecl> =
  View extends ViewDecl<infer Props, infer Params> ? DeclaredViewProps<Props, Params> : never;

/** Any declaration; `any` is existential, as for `AnyRenderer`. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type AnyViewDecl = ViewDecl<any>;

/** Declare a view: `name` is its id, or, `within` another view, the last part of its id after the
 *  parent's (`pander-score.ScoreFit/point`). A view within another shows the same subject. */
export function declareView<Props extends object, Params extends string = never>(
  slot: ViewSlot<Props>,
  subjectType: string,
  name: string,
  options: { readonly within?: ViewDecl<Props>; readonly params?: readonly Params[] } = {},
): ViewDecl<Props, Params> {
  const within = options.within ?? null;
  if (within !== null && (within.slot.symbol !== slot.symbol || within.subjectType !== subjectType))
    throw new Error(`view ${name} is declared within ${within.id}, which shows another subject`);
  return Object.freeze({
    slot,
    subjectType,
    id: within === null ? name : `${within.id}/${name}`,
    params: Object.freeze([...(options.params ?? [])]),
    within: within?.id ?? null,
  });
}

/** One renderer. `id` must be unique among the renderers of its slot and subject type, and a
 *  wildcard's among all of its slot's, since a host offers them side by side. `params` are the
 *  params it reads, which a host binds by name where it is shown and hands in the slot's `params`
 *  prop; `within`, the id of the view it is declared within (a host offers it only through links
 *  from that one). */
export interface Renderer<Props extends object> {
  readonly slot: ViewSlot<Props>;
  readonly subjectType: string;
  readonly id: string;
  readonly Component: ComponentType<Props>;
  readonly params?: readonly string[];
  readonly within?: string;
}

/** Any renderer; `any` is existential (each renderer owns its slot's `Props`). */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type AnyRenderer = Renderer<any>;

/** Declare a renderer: of a declared view, whose component is handed the params it declares
 *  (`DeclaredViewProps`), or of a slot, subject type and id, reading no params. `Props` flows from
 *  the slot only (`NoInfer` on the component), so a component with the wrong props is an error
 *  here, not a widened `Props`. */
export function renderer<Props extends object, Params extends string>(
  view: ViewDecl<Props, Params>,
  Component: ComponentType<NoInfer<DeclaredViewProps<Props, Params>>>,
): Renderer<Props>;
export function renderer<Props extends object>(
  slot: ViewSlot<Props>,
  subjectType: string,
  id: string,
  Component: ComponentType<NoInfer<Props>>,
): Renderer<Props>;
export function renderer<Props extends object>(
  first: ViewDecl<Props> | ViewSlot<Props>,
  second: ComponentType<Props> | string,
  id?: string,
  Component?: ComponentType<Props>,
): Renderer<Props> {
  if (isDecl(first)) {
    const view = first;
    return Object.freeze({
      slot: view.slot,
      subjectType: view.subjectType,
      id: view.id,
      Component: second as ComponentType<Props>,
      ...(view.params.length > 0 ? { params: view.params } : {}),
      ...(view.within === null ? {} : { within: view.within }),
    });
  }
  return Object.freeze({
    slot: first,
    subjectType: second as string,
    id: id!,
    Component: Component!,
  });
}

function isDecl<Props extends object>(value: ViewDecl<Props> | ViewSlot<Props>): value is ViewDecl<Props> {
  return "slot" in value;
}
