// View slots. A slot names one place in the UI a renderer goes ("the panel", "the inline link",
// "the invocation panel") and carries, as a phantom type parameter, the props contract of every
// component registered under it. fxtr-view declares every slot (`builtin_slots.ts`); identity is a
// plain symbol created once there. The brand is an assertion: components registered under the slot
// honor `Props`; nothing checks it at runtime. A renderer shows a *subject*: an entity, or an invocation. Which
// renderer fills a slot is chosen by the *subject type*, the kind of thing being rendered: an
// entity's type name, or a step / workflow's registered name. The slot itself does not care which.

import type { EntityID } from "./ids";
import type { Spawn } from "./invocation";

declare const PROPS_BRAND: unique symbol;

/** A place in the UI a renderer goes, branded by the props its components take. */
export interface ViewSlot<Props extends object> {
  readonly symbol: symbol;
  readonly debugName: string;
  /** Phantom, invariant in `Props`; never assigned. */
  readonly [PROPS_BRAND]?: (props: Props) => Props;
}

/** Declare a slot. `debugName` is for messages and devtools only; identity is the fresh symbol. */
export function declareViewSlot<Props extends object>(debugName: string): ViewSlot<Props> {
  return Object.freeze({ symbol: Symbol(debugName), debugName });
}

/** The props every entity view receives: the id of the entity it shows. A slot whose `Props`
 *  extends this is an *entity slot*; a host finds its subject type (the type name) by loading the
 *  entity. */
export interface EntityViewProps {
  readonly id: EntityID;
}

/** The props every invocation view receives: the invocations it shows, each with its key within
 *  the run the host took them from: the spawns the host's selection picks out (one, several, or
 *  every spawn of the run), and the view shows every count it is given. A slot whose `Props`
 *  extends this is an *invocation slot*; its subject type is the step / workflow name, which the
 *  host knows from its own data. */
export interface InvocationViewProps {
  readonly invocations: readonly Spawn[];
}
