import { FxtrViewError } from "fxtr-view";

/** A dispatch could not be served: a duplicate registration, an unknown renderer id, no
 *  `RegistryProvider` above. */
export class DispatchFault extends FxtrViewError {}
