// Adapters from a view of one loaded invocation record to the list props every invocation renderer
// receives: a panel is handed however many spawns the host's selection picks out.
// `singleInvocation` (deprecated) wraps a view of one record so that for one spawn it loads the
// record (suspending onto the host's loading fallback, throwing a failed load into the host's error
// boundary) and renders the view with it, and for any other count says that one is to be selected.
// `eachInvocation` wraps it so several spawns become
// a list of collapsible sections, one per spawn, each loading its own record when opened, and each
// catching its own failure.

import type { ComponentType, FunctionComponent } from "react";
import { invocationRefKey, unwrap, useInvocation, type InvocationRecord, type InvocationViewProps, type Spawn } from "fxtr-view";
import layout from "./layout.module.css";
import { LazySection, Sections } from "./sections";

/** The props of a view of one loaded record. */
export interface SingleInvocationProps {
  readonly invocation: InvocationRecord;
}

/** A spawn's key values, joined; "·" alone for an unmapped run. */
export function spawnKeyLabel(spawn: Spawn): string {
  const values = Object.values(spawn.key);
  return values.length === 0 ? "·" : values.join(" · ");
}

/** The view rendered with one spawn's loaded record. */
function loadedView<P extends SingleInvocationProps>(View: ComponentType<P>) {
  return function Loaded({ spawn, rest }: { spawn: Spawn; rest: Omit<P, "invocation"> }) {
    const invocation = unwrap(useInvocation(spawn.ref));
    return <View {...({ ...rest, invocation } as P)} />;
  };
}

/** Wrap a view of one record as an invocation renderer: the view with the loaded record for one
 *  spawn, and for zero or several a note asking for one to be selected. The view's other props pass
 *  through.
 *
 *  @deprecated A view must be useful however many invocations it is handed, as soon as it is
 *  opened: a step or child workflow mapped over a dim hands it every spawn, and a note asking for a
 *  selection is not a view. Write the renderer over `invocations` instead, showing several in a view
 *  of their own (an aggregate, a row or card each, linking to each one alone) and one in full; load
 *  what they return together with `LoadEach`. For a root workflow, which is always handed its one
 *  run, read that spawn directly. */
export function singleInvocation<P extends SingleInvocationProps>(
  View: ComponentType<P>,
): FunctionComponent<InvocationViewProps & Omit<P, "invocation">> {
  type Rest = Omit<P, "invocation">;
  const Loaded = loadedView(View);
  function Single(props: InvocationViewProps & Rest) {
    const { invocations, ...rest } = props;
    if (invocations.length !== 1) return <SelectOne count={invocations.length} />;
    return <Loaded spawn={invocations[0] as Spawn} rest={rest as unknown as Rest} />;
  }
  Single.displayName = `singleInvocation(${View.displayName ?? View.name})`;
  return Single;
}

/** What a view of one invocation shows when given another count. */
function SelectOne({ count }: { count: number }) {
  const selected = count === 0 ? "No invocation is selected" : `${count} invocations are selected`;
  return <p className={layout.muted}>{selected}: select one to use this view.</p>;
}

export interface EachInvocationOptions {
  /** A one-line summary of a spawn's record, shown after its key in the section's heading (so
   *  every summary loads its record, even while its section is closed). */
  readonly Summary?: ComponentType<SingleInvocationProps>;
}

/**
 * Wrap a view of one record as an invocation renderer of any count: nothing for zero spawns, the
 * view with the loaded record for one, and for several a list of collapsible sections headed by
 * each spawn's key (and its `Summary`), each rendering the view on its own record once opened.
 * Every section loads for itself, so the list fills in progressively, and a record that fails to
 * load fails only its own section. The view's other props pass through to every section.
 */
export function eachInvocation<P extends SingleInvocationProps>(
  View: ComponentType<P>,
  { Summary }: EachInvocationOptions = {},
): FunctionComponent<InvocationViewProps & Omit<P, "invocation">> {
  type Rest = Omit<P, "invocation">;
  const Loaded = loadedView(View);
  const LoadedSummary = Summary && loadedView(Summary);
  function Each(props: InvocationViewProps & Rest) {
    const { invocations, ...rest } = props;
    if (invocations.length === 0) return null;
    if (invocations.length === 1) return <Loaded spawn={invocations[0] as Spawn} rest={rest as unknown as Rest} />;
    return (
      <Sections>
        {invocations.map((spawn) => (
          <LazySection
            key={invocationRefKey(spawn.ref)}
            label={spawnKeyLabel(spawn)}
            {...(LoadedSummary ? { summary: <LoadedSummary spawn={spawn} rest={{}} /> } : {})}
            body={() => <Loaded spawn={spawn} rest={rest as unknown as Rest} />}
          />
        ))}
      </Sections>
    );
  }
  Each.displayName = `eachInvocation(${View.displayName ?? View.name})`;
  return Each;
}
