// The dispatch registry: renderer packages' lists gathered into one object, and the lookup over
// it. The renderers of one slot and subject type, the specific ones then the slot's wildcards
// (`ANY_SUBJECT_TYPE`), are the alternatives a host lets the user switch between, in list order,
// the first being the default. `renderersFor` is the one question dispatch asks of a registry; `chosenRenderer` is
// how a host reads a remembered choice back against those alternatives, and `shownRenderer` which
// of them a choice shows.

import { ANY_SUBJECT_TYPE, type AnyRenderer, type Renderer, type ViewSlot } from "fxtr-view";
import { DispatchFault } from "./errors";

/** What a shell dispatches through. Build one with `dispatchRegistry`, which validates it. */
export interface DispatchRegistry {
  readonly renderers: readonly AnyRenderer[];
}

/** Build a registry. Throws `DispatchFault` for two renderers of one slot sharing an id where a
 *  host would offer both: under one subject type, or a wildcard against anything of the slot. */
export function dispatchRegistry(renderers: readonly AnyRenderer[]): DispatchRegistry {
  renderers.forEach((item, i) => {
    for (const other of renderers.slice(0, i)) {
      const offeredTogether =
        other.subjectType === item.subjectType || other.subjectType === ANY_SUBJECT_TYPE || item.subjectType === ANY_SUBJECT_TYPE;
      if (other.slot.symbol === item.slot.symbol && other.id === item.id && offeredTogether) {
        throw new DispatchFault(
          `duplicate renderer id "${item.id}" in ${item.slot.debugName} (${other.subjectType} and ${item.subjectType})`,
        );
      }
    }
  });
  return Object.freeze({ renderers: Object.freeze([...renderers]) });
}

/** The renderers of `slot` for a subject type: the ones registered for it in order, then the
 *  slot's wildcards in order. The first is the default; empty when there is none. This is also what
 *  a host offers the user to switch between. A registry is a few dozen renderers, so this is a
 *  scan, not an index. Throws `DispatchFault` for `ANY_SUBJECT_TYPE`, which is no subject's type. */
export function renderersFor<Props extends object>(
  registry: DispatchRegistry,
  slot: ViewSlot<Props>,
  subjectType: string,
): readonly Renderer<Props>[] {
  if (subjectType === ANY_SUBJECT_TYPE) throw new DispatchFault(`renderersFor asked for the wildcard in ${slot.debugName}`);
  const ofSlot = registry.renderers.filter((item) => item.slot.symbol === slot.symbol);
  return [...ofSlot.filter((item) => item.subjectType === subjectType), ...ofSlot.filter((item) => item.subjectType === ANY_SUBJECT_TYPE)];
}

/** A remembered renderer choice read back against the renderers now offered (`renderersFor` /
 *  `useRenderers`): the id when one of them still has it, else null, meaning the default (the
 *  first renderer, or a host's own view). A choice can outlive its renderer (a subject type
 *  changed under a kept choice, a registry edited between sessions), and every host falls back
 *  the same way; dispatching an id no renderer has is a fault. */
export function chosenRenderer(renderers: readonly AnyRenderer[], remembered: string | null): string | null {
  return remembered !== null && renderers.some((candidate) => candidate.id === remembered) ? remembered : null;
}

/** The renderer dispatch shows for a choice among `renderers`: the one with the chosen id, or the
 *  first for no choice. Undefined when the id is not among them, or there are none. */
export function shownRenderer<Props extends object>(
  renderers: readonly Renderer<Props>[],
  chosen: string | null,
): Renderer<Props> | undefined {
  return chosen === null ? renderers[0] : renderers.find((candidate) => candidate.id === chosen);
}
