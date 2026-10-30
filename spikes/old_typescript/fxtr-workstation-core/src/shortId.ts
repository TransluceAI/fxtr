import type { FxtrID } from "@fxtr/core";

export function shortId(id: FxtrID): string {
	return id.idString().slice(-8);
}
