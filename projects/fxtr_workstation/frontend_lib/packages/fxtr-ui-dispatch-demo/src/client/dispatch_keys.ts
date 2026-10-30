/**
 * Dispatch-key declarations for the dispatch demo.
 *
 * We use a single `CardKey` that accepts both entities and
 * services — the registry then has two `entityEntry` /
 * `serviceEntry` pairings for the same key, with kind-specific
 * lookup maps.
 *
 * The UUID is hardcoded per `declareDispatchKey`'s contract.
 * In a real app it'd live next to the key it identifies; for
 * the demo it lives here so the dispatch-registry module can
 * import it.
 *
 * Fallback components (Loading / NoMatch / Error / optional
 * Builtin) live on the key itself; the dispatcher reads them
 * directly. See
 * `projects/fxtr_workstation/plans/phase0/part3_frontend_ui/stage10aside1_dispatch_key_fallback_refactor.md`
 * for the rationale.
 */

import { declareDispatchKey } from "@fxtr-ui/react-dispatch";
import { BuiltinCard } from "./cards/BuiltinCard";
import { CardErrorFallback } from "./cards/CardErrorFallback";
import { CardFallback } from "./cards/CardFallback";
import { NoMatchCard } from "./cards/NoMatchCard";

// No explicit Props — `CardKey` doesn't take extra props
// beyond `stub`, so the `NoProps` default applies.
export const CardKey = declareDispatchKey({
	uuid: "a8f3c2e7-1b4d-4e2a-9c5f-d6e7a8b9c0d1",
	description: "Card",
	fallbacks: {
		Loading: CardFallback,
		NoMatch: NoMatchCard,
		Error: CardErrorFallback,
		// Builtin only fires for entity stubs with `.` / `!`-prefixed
		// shortIds; service stubs ignore it.
		Builtin: BuiltinCard,
	},
});
