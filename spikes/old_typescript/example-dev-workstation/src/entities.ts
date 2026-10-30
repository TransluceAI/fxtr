/**
 * Dev demo entity definitions.
 *
 * - DemoNote: a small labeled document (title, body).
 * - DemoSection: a grouping entity with a title, summary, and a list of
 *   referenced note ids. Has a registered FullPanel handler that
 *   renders each item via `<EntityLink as="block">`.
 * - DemoArchive: a meta-grouping with a name and a list of sections.
 *   Intentionally has NO registered renderers on the client so it
 *   exercises the JsonTree FullPanel fallback — with tagged IDs
 *   rendered through `<EntityLink as="inline">` wrapping the
 *   InlineLink fallback.
 * - DemoListNode: a self-referential linked-list node (`next` points
 *   at another DemoListNode, nullable to terminate the chain). The
 *   server seeds a 20-node chain and pins its head as
 *   `demo/chain-head` — useful for exercising breadcrumb history:
 *   walking 20 clicks grows the chain, then clicking older entries
 *   navigates back / forward.
 */

import { type EntityRef, UUID, defineType } from "@fxtr/core";
import * as bicodec from "@fxtr/core/bicodec";

const NS = UUID("b5e4a2d1-9c3f-4a7e-8d10-2e6f4a1c9b3d");

const _DemoNote = bicodec.entity(defineType(NS, "DemoNote"), {
	title: bicodec.string(),
	body: bicodec.string(),
});
export interface DemoNote extends bicodec.InferEntity<typeof _DemoNote> {}
export const DemoNote: bicodec.EntityDefinition<DemoNote> = _DemoNote;

const _DemoSection = bicodec.entity(defineType(NS, "DemoSection"), {
	title: bicodec.string(),
	summary: bicodec.string(),
	items: bicodec.array(bicodec.entityRef(DemoNote)),
});
export interface DemoSection extends bicodec.InferEntity<typeof _DemoSection> {}
export const DemoSection: bicodec.EntityDefinition<DemoSection> = _DemoSection;

const _DemoArchive = bicodec.entity(defineType(NS, "DemoArchive"), {
	name: bicodec.string(),
	sections: bicodec.array(bicodec.entityRef(DemoSection)),
});
export interface DemoArchive extends bicodec.InferEntity<typeof _DemoArchive> {}
export const DemoArchive: bicodec.EntityDefinition<DemoArchive> = _DemoArchive;

// Self-referential `next` needs bicodec.lazy so the codec graph can
// close over itself without a forward reference. TypeScript side: we
// describe the field shape explicitly rather than let `InferEntity`
// loop.
interface DemoListNodeFields {
	label: string;
	next: EntityRef<bicodec.EntityInstance<DemoListNodeFields>> | null;
}

const _DemoListNode: bicodec.EntityDefinition<bicodec.EntityInstance<DemoListNodeFields>> =
	bicodec.entity(defineType(NS, "DemoListNode"), {
		label: bicodec.string(),
		next: bicodec.nullable(bicodec.lazy(() => bicodec.entityRef(DemoListNode))),
	});
export interface DemoListNode extends bicodec.EntityInstance<DemoListNodeFields> {}
export const DemoListNode: bicodec.EntityDefinition<DemoListNode> = _DemoListNode;
