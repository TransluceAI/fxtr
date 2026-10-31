// Dispatch over the registry a `RegistryProvider` puts in context, one component per kind of
// subject. `RenderEntity` renders an entity slot from an id alone: it loads the entity through the
// view shell, reads its type name off the data, and dispatches on it with the id threaded in.
// `RenderInvocation` renders an invocation slot for a step / workflow name the host already has
// from its own data; the renderer loads records and arrays as it needs them. Both go through
// `RenderSlot`, this module's one dispatch (not exported: a caller always knows which kind it is
// rendering): a slot, the subject type, optionally the id of the renderer to show among those
// registered for it, and the slot's props. `useRenderers` lists a subject type's renderers, which is
// what a host builds a switcher from. What a dispatch shows in place of a renderer is the host's:
// its `fallbacks` (loading, error, no renderer), each defaulting to this package's placeholders.
// Each dispatch sits in an error boundary rendering the error fallback and a Suspense boundary
// rendering the loading one (for a component that `unwrap`s a pending load), and is keyed so a change of what is dispatched (the subject type, the
// renderer, the entity id, the spawns) remounts the dispatched subtree, which is also what clears a
// caught error. So does a new registry: a host may replace its registry while it runs (the
// frontend does when a project's renderers are rebuilt), and a renderer that threw gets another
// chance with the new code.
import { Component, Suspense, createContext, useContext, type ErrorInfo, type ReactNode } from "react";
import {
  idKey,
  invocationRefKey,
  typeNameOf,
  useEntity,
  type EntityViewProps,
  type InvocationViewProps,
  type Renderer,
  type ViewSlot,
} from "fxtr-view";
import { DispatchFault } from "./errors";
import { PlaceholderError, PlaceholderLoading, PlaceholderNoRenderer } from "./placeholders";
import { renderersFor, shownRenderer, type DispatchRegistry } from "./registry";

const RegistryContext = createContext<DispatchRegistry | null>(null);

/** Each registry dispatch has rendered through, numbered in order: part of every dispatch's key,
 *  so replacing the registry remounts what dispatch rendered. */
const generations = new WeakMap<DispatchRegistry, number>();
let nextGeneration = 0;

function generationOf(registry: DispatchRegistry): number {
  let generation = generations.get(registry);
  if (generation === undefined) {
    generation = nextGeneration++;
    generations.set(registry, generation);
  }
  return generation;
}

/** Provide the registry this package's dispatch components render through. A shell mounts it
 *  beside fxtr-view's `ViewShellProvider`; which is outside the other does not matter. A new
 *  registry remounts everything dispatched through the old one. */
export function RegistryProvider({ registry, children }: { registry: DispatchRegistry; children?: ReactNode }) {
  return <RegistryContext.Provider value={registry}>{children}</RegistryContext.Provider>;
}

/** The registry in context. Throws `DispatchFault` outside a `RegistryProvider`. */
export function useRegistry(): DispatchRegistry {
  const registry = useContext(RegistryContext);
  if (registry === null) throw new DispatchFault("fxtr-view-shell-util: dispatch used outside a RegistryProvider");
  return registry;
}

/** What a dispatch shows in place of a renderer, chosen by the host where it dispatches. Each one
 *  left out is this package's placeholder. */
export interface DispatchFallbacks {
  /** While the entity loads (`RenderEntity`) or the renderer suspends. */
  readonly loading?: ReactNode;
  /** For a failed load, a renderer that throws, or a renderer id nothing has. */
  readonly error?: (error: unknown) => ReactNode;
  /** For a subject type with no renderer at all. */
  readonly noRenderer?: (subjectType: string) => ReactNode;
}

const DEFAULT_FALLBACKS: Required<DispatchFallbacks> = {
  loading: <PlaceholderLoading />,
  error: (error) => <PlaceholderError error={error} />,
  noRenderer: (subjectType) => <PlaceholderNoRenderer subjectType={subjectType} />,
};

function withDefaults(fallbacks: DispatchFallbacks | undefined): Required<DispatchFallbacks> {
  return fallbacks === undefined ? DEFAULT_FALLBACKS : { ...DEFAULT_FALLBACKS, ...fallbacks };
}

interface BoundaryProps {
  readonly renderError: (error: unknown) => ReactNode;
  readonly children: ReactNode;
}

class DispatchErrorBoundary extends Component<BoundaryProps, { error: unknown; failed: boolean }> {
  override state = { error: undefined as unknown, failed: false };

  static getDerivedStateFromError(error: unknown) {
    return { error, failed: true };
  }

  override componentDidCatch(_error: unknown, _info: ErrorInfo) {
    // The fallback shows the error; nothing else to record.
  }

  override render() {
    if (this.state.failed) return this.props.renderError(this.state.error);
    return this.props.children;
  }
}

/** The renderers of `slot` for a subject type (see `renderersFor`): the ones registered for it,
 *  then the slot's wildcards. What a host offers the user to switch between; the first is what
 *  shows when nothing is chosen. Empty for a null subject type. */
export function useRenderers<Props extends object>(slot: ViewSlot<Props>, subjectType: string | null): readonly Renderer<Props>[] {
  const registry = useRegistry();
  return subjectType === null ? [] : renderersFor(registry, slot, subjectType);
}

/** Props of `RenderSlot`: the slot, the subject type, optionally the id of the renderer to show
 *  and the host's fallbacks, and the slot's own props. `slot`, `subjectType`, `renderer`, and
 *  `fallbacks` are reserved names a slot's props must not use. */
export type RenderSlotProps<Props extends object> = {
  readonly slot: ViewSlot<Props>;
  readonly subjectType: string;
  /** One of the ids `useRenderers` lists for the subject type. Left out, the first shows. */
  readonly renderer?: string;
  readonly fallbacks?: DispatchFallbacks;
} & Props;

/** Dispatch: render the renderer of `slot` for `subjectType` with id `renderer`, else the
 *  first, with the remaining props, inside an error boundary showing the error fallback and a
 *  Suspense boundary showing the loading one. A subject type with no renderer at all shows the
 *  no-renderer fallback; an id no renderer has shows the error fallback with a `DispatchFault`
 *  saying so (a host reads a remembered choice back through `chosenRenderer` first, so reaching
 *  this is a bug). */
export function RenderSlot<Props extends object>(props: RenderSlotProps<Props>) {
  const { slot, subjectType, renderer, fallbacks, ...rest } = props;
  const componentProps = rest as unknown as Props;
  const shown = withDefaults(fallbacks);
  const registry = useRegistry();

  const renderers = renderersFor(registry, slot, subjectType);
  const found = shownRenderer(renderers, renderer ?? null);
  if (renderer !== undefined && found === undefined) {
    return shown.error(new DispatchFault(`no renderer "${renderer}" for ${subjectType} in ${slot.debugName}`));
  }
  return (
    <DispatchErrorBoundary
      key={`${generationOf(registry)}:${slot.debugName}:${subjectType}:${found?.id ?? ""}`}
      renderError={shown.error}
    >
      <Suspense fallback={shown.loading}>
        {found === undefined ? shown.noRenderer(subjectType) : <found.Component {...componentProps} />}
      </Suspense>
    </DispatchErrorBoundary>
  );
}

/** Props of `RenderEntity`: the entity slot, optionally the renderer id and the host's fallbacks,
 *  and the slot's props (which include the id). */
export type RenderEntityProps<Props extends EntityViewProps> = {
  readonly slot: ViewSlot<Props>;
  readonly renderer?: string;
  readonly fallbacks?: DispatchFallbacks;
} & Props;

/** Entity dispatch: load the entity through the view shell, show the loading fallback until it
 *  arrives, then dispatch raw on its type name with the id (and the other props) threaded in. A
 *  failed load, or data without a type tag, shows the error fallback. Only slots whose props carry
 *  the id are accepted. */
export function RenderEntity<Props extends EntityViewProps>(props: RenderEntityProps<Props>) {
  const { slot, renderer, fallbacks, ...rest } = props;
  const componentProps = rest as unknown as Props;
  const result = useEntity(componentProps.id);
  const shown = withDefaults(fallbacks);

  if (result.status === "loading") return shown.loading;
  if (result.status === "error") return shown.error(result.error);

  let typeName: string;
  try {
    typeName = typeNameOf(result.value);
  } catch (error) {
    return shown.error(error);
  }
  return (
    <RenderSlot
      key={idKey(componentProps.id)}
      slot={slot}
      subjectType={typeName}
      {...(renderer === undefined ? {} : { renderer })}
      {...(fallbacks === undefined ? {} : { fallbacks })}
      {...componentProps}
    />
  );
}

/** Props of `RenderInvocation`: the invocation slot, the registered step / workflow name,
 *  optionally the renderer id and the host's fallbacks, and the slot's props (which include the
 *  spawns). `slot`, `name`, `renderer`, and `fallbacks` are reserved names a slot's props must not
 *  use. */
export type RenderInvocationProps<Props extends InvocationViewProps> = {
  readonly slot: ViewSlot<Props>;
  /** The registered `@step` / `@workflow` name: what the slot's renderers are keyed on. */
  readonly name: string;
  readonly renderer?: string;
  readonly fallbacks?: DispatchFallbacks;
} & Props;

/** Invocation dispatch: the renderer of `slot` for the step / workflow `name`, with the spawns
 *  (and the other props), remounted when the spawns shown change. Only slots whose props carry
 *  the spawns are accepted. */
export function RenderInvocation<Props extends InvocationViewProps>(props: RenderInvocationProps<Props>) {
  const { slot, name, renderer, fallbacks, ...rest } = props;
  const componentProps = rest as unknown as Props;
  return (
    <RenderSlot
      key={JSON.stringify(componentProps.invocations.map((spawn) => invocationRefKey(spawn.ref)))}
      slot={slot}
      subjectType={name}
      {...(renderer === undefined ? {} : { renderer })}
      {...(fallbacks === undefined ? {} : { fallbacks })}
      {...componentProps}
    />
  );
}
