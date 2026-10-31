// The renderers every shell starts from: those of fxtr's core types (the Array table and inline
// link) and the cbor-tree views as wildcard renderers of the built-in entity slots, so every type
// has a panel, a link, and a preview. The invocation slots have no wildcard:
// the hosts show their own view for a step or workflow with no renderers. Project renderers are
// not here; a shell concatenates the lists of the projects it loads with `defaultRenderers` (the
// frontend's `views/registry.ts` is the example). Nothing else imports the core renderer packages.

import type { AnyRenderer } from "fxtr-view";
import { arrayTableRenderers } from "fxtr-view-array-table";
import { cborTreeRenderers } from "fxtr-view-cbor-tree";

export const defaultRenderers: readonly AnyRenderer[] = [...arrayTableRenderers, ...cborTreeRenderers];
