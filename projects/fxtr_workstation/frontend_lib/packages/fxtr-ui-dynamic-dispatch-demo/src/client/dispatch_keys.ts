/**
 * Dispatch-key declaration for the dynamic-dispatch demo.
 *
 * The renderer modules in `../renderers/<label>/index.tsx` export
 * a 2-level map keyed by `[subtype_key][dispatch_key_uuid]`. The
 * `dispatch_key_uuid` in those modules MUST match `CardKey.uuid`
 * below — both sides agree out-of-band on the uuid (a future
 * iteration could publish the keys as entities). If you change
 * the uuid here, update every renderer module's
 * `CARD_KEY_UUID` constant too.
 */

import { declareDispatchKey } from "@fxtr-ui/react-dispatch";
import { BuiltinCard } from "./cards/BuiltinCard";
import { CardErrorFallback } from "./cards/CardErrorFallback";
import { CardFallback } from "./cards/CardFallback";
import { NoMatchCard } from "./cards/NoMatchCard";

export const CardKey = declareDispatchKey({
	uuid: "00000000-0000-4000-8000-000000010001",
	description: "Card",
	fallbacks: {
		Loading: CardFallback,
		NoMatch: NoMatchCard,
		Error: CardErrorFallback,
		// Builtin only fires for entity stubs with `.` / `!`-prefixed
		// shortIds.
		Builtin: BuiltinCard,
	},
});
