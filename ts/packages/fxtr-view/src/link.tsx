// `EntityLink`: how a renderer links to another entity. It renders the shell's `EntityLinkImpl`,
// which is the entity's inline link view with the shell's link behavior, and nothing else.
//
// A link opens one of two things:
//
// - **Where its entity sits** (`at`): the path to it from the value the view was handed, and the dim
//   keys of the row or invocation it belongs to. The shell sets that value's own place around the
//   view (`LinkBase`), and the link's place is the two joined, so the shell opens "this field of
//   that step's output" rather than the bare entity. A link without `at`, or outside any
//   `LinkBase`, has no place, and the shell opens the entity itself. This is the way to link to
//   anything inside what the view was handed: it needs no location syntax, and generic components
//   (a data tree) can extend the place without knowing where they are drawn.
// - **Somewhere the view names** (`to`, `view`, `params`), as `useLink` does: for an entity the view
//   knows a better place of than inside its own value, such as a step's input or another step's
//   output.
//
// A renderer links to another view with `useLink`, naming the target by its declaration
// (`declareView`), a location (the host's location syntax, relative to the location of the view
// drawing it: `.inputs.prompt` an input of its step, `../reply` a sibling step, or an absolute
// `/prompts.output`), or both, and the params the target is opened at, which must bind every param
// a declared target reads. The host keeps what it opens tied to the view the link is in, so it
// follows as that view's own location and params change.
//
// `useEntityLinks` is `EntityLink`'s behavior for an element that stands for an entity but cannot be
// a link view of its own (a chart's point, a heatmap's cell): the renderer draws the element and
// calls `open` when it is clicked, and the shell decides what opening does, as it does for a link.
// It opens the entity alone; one hook serves every mark, so it fits marks drawn in a loop.

import { createContext, useContext, type DragEvent, type MouseEvent, type ReactNode } from "react";
import type { EntityID } from "./ids";
import type { AnyViewDecl, ViewDecl } from "./renderer";
import { useViewShell } from "./shell";

/** A dim's key value, as a spawn's key holds it. */
export type LinkKeyValue = string | number;

/** Where a value sits: `base` is the shell's own name for the value a view was handed (opaque to
 *  renderers), `path` the fields and list indices from it, and `keys` the dim keys picking it out. */
export interface LinkPlace {
  readonly base: unknown;
  readonly path: readonly string[];
  readonly keys: Readonly<Record<string, LinkKeyValue>>;
}

/** A link's place relative to the enclosing `LinkBase`: more path, and more keys. */
export interface LinkAt {
  readonly path?: readonly (string | number)[];
  readonly keys?: Readonly<Record<string, LinkKeyValue>>;
}

const LinkPlaceContext = createContext<LinkPlace | null>(null);

/** Set the place of the value the views below were handed; `base` null means it has none, so the
 *  links below open their entities alone. Inside another `LinkBase`, `at` extends it instead. */
export function LinkBase({
  base,
  at,
  children,
}: {
  base?: unknown;
  at?: LinkAt;
  children?: ReactNode;
}) {
  const enclosing = useContext(LinkPlaceContext);
  const value =
    base === null
      ? null
      : base !== undefined
        ? extend({ base, path: [], keys: {} }, at)
        : enclosing && extend(enclosing, at);
  return <LinkPlaceContext.Provider value={value}>{children}</LinkPlaceContext.Provider>;
}

/** The place of the value the enclosing views were handed, or null: for a component that links
 *  from inside such a value, the row keys an enclosing `LinkBase` set, say. */
export function useLinkPlace(): LinkPlace | null {
  return useContext(LinkPlaceContext);
}

function extend(place: LinkPlace, at: LinkAt | undefined): LinkPlace {
  if (!at) return place;
  return {
    base: place.base,
    path: [...place.path, ...(at.path ?? []).map(String)],
    keys: { ...place.keys, ...at.keys },
  };
}

/** Values bound to params by name: the dims of a step, and the params views declare. */
export type ViewParams = Readonly<Record<string, LinkKeyValue>>;

/** The params a link to a view reading `Params` is opened at: a row's full keys, say, among which
 *  each of `Params` is bound. */
export type LinkParams<Params extends string> = ViewParams & { readonly [Name in Params]: LinkKeyValue };

/** A link's `params`: required, and binding each of `Params`, when the target declares any. */
type ParamsProp<Params extends string> = [Params] extends [never]
  ? { readonly params?: ViewParams }
  : { readonly params: LinkParams<Params> };

/** A link to where its entity sits in what the view was handed (`at`; none: the entity alone). */
interface PlacedLinkProps {
  readonly id: EntityID;
  readonly at?: LinkAt;
  readonly to?: never;
  readonly view?: never;
  readonly params?: never;
}

/** A link to somewhere the view names in place of where the entity sits: a location (`to`, as
 *  `useLink` takes it), a declared view (`view`), or both, opened at `params`. */
type NamedLinkProps<Params extends string> = { readonly id: EntityID; readonly at?: never } & (
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  | { readonly to: string; readonly view?: ViewDecl<any, Params> }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  | { readonly to?: string; readonly view: ViewDecl<any, Params> }
) &
  ParamsProp<Params>;

export type EntityLinkProps<Params extends string = never> = PlacedLinkProps | NamedLinkProps<Params>;

/** What a link names, as a shell is handed it: a view's id (null: the default for what the
 *  location shows) and the id of the view it is declared within, and a location in the host's
 *  syntax (null: the location of the view the link is in). */
export interface ViewLinkSpec {
  readonly view: string | null;
  readonly within: string | null;
  readonly to: string | null;
}

/** What the shell's `EntityLinkImpl` is given: the id, the link's place when it has one, and what
 *  the link opens when the view named it. */
export interface EntityLinkImplProps {
  readonly id: EntityID;
  readonly place?: LinkPlace | null;
  readonly to?: { readonly spec: ViewLinkSpec; readonly params: ViewParams };
}

export function EntityLink<Params extends string = never>(props: EntityLinkProps<Params>) {
  const { EntityLinkImpl } = useViewShell();
  const enclosing = useContext(LinkPlaceContext);
  const { id, at, to, view, params } = props;
  const place = at && enclosing ? extend(enclosing, at) : null;
  const named =
    to === undefined && view === undefined ? undefined : { spec: specOf(view ?? null, to ?? null), params: params ?? NO_PARAMS };
  return <EntityLinkImpl id={id} place={place} {...(named === undefined ? {} : { to: named })} />;
}

/** A link as the props to spread onto whatever element is picked (an SVG mark, a row), and whether
 *  what it opens is open now (`open`), and in the view a plain click on it goes to (`selected`), so
 *  the element can show itself picked. */
export interface ViewLink {
  readonly props: {
    readonly onClick: (event: MouseEvent) => void;
    readonly onMouseEnter: () => void;
    readonly onMouseLeave: () => void;
    readonly draggable: boolean;
    readonly onDragStart: (event: DragEvent) => void;
  };
  readonly open: boolean;
  readonly selected: boolean;
}

/** What `useLink` opens: a declared view (at the location of the view the link is in), a location
 *  (`to`) through its default view, or a declared view at a location. */
export type LinkTo<Params extends string = string> =
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  | ViewDecl<any, Params>
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  | { readonly view?: ViewDecl<any, Params>; readonly to: string };

/** `useLink`'s params: required, and binding each of `Params`, when the target declares any. */
type ParamsArg<Params extends string> = [Params] extends [never] ? [params?: ViewParams] : [params: LinkParams<Params>];

/** A link to `target`, opened at `params` (null target: no link); null when the shell cannot open
 *  views from here, so the renderer shows its detail in place instead. Pass a row's full keys: the
 *  host keeps only what they add to this view's. A declared view's params must be among them. A
 *  view declared within another is linked to only from that one. */
export function useLink<Params extends string = never>(target: LinkTo<Params> | null, ...[params]: ParamsArg<Params>): ViewLink | null {
  const shell = useViewShell();
  if (shell.useLink === undefined) return null;
  const spec = target === null ? null : "slot" in target ? specOf(target, null) : specOf(target.view ?? null, target.to);
  return shell.useLink(spec, params ?? NO_PARAMS);
}

function specOf(view: AnyViewDecl | null, to: string | null): ViewLinkSpec {
  return { view: view?.id ?? null, within: view?.within ?? null, to };
}

const NO_PARAMS: ViewParams = Object.freeze({});
/** Following links from elements a renderer draws itself. */
export interface EntityLinks {
  /** Follow a link to the entity: what clicking its `EntityLink` does in this shell. */
  readonly open: (id: EntityID) => void;
  /** Whether the shell is showing the entity now, the state its `EntityLink` is highlighted in.
   *  A renderer draws the element standing for it the same way. */
  readonly isOpen: (id: EntityID) => boolean;
}

/** What `useEntityLinks` returns under a shell that does not follow links: nothing opens. */
const NO_LINKS: EntityLinks = Object.freeze({
  open: () => {},
  isOpen: () => false,
});

/** Following links through the shell, for elements that stand for entities. A shell's members are
 *  stable for the provider's lifetime, so whether the shell's hook is called is fixed per mount and
 *  the rules of hooks hold. */
export function useEntityLinks(): EntityLinks {
  const shell = useViewShell();
  return shell.useEntityLinks === undefined ? NO_LINKS : shell.useEntityLinks();
}
