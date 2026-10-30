/**
 * Wire-decoded shapes of the Python-side report entities.
 *
 * The Python entity definitions live in `fxtr_report_builder`
 * (Python package). Their `contents` / `overview` / `detail`
 * fields are stored as ``FrozenFxtrCbor`` opaque trees; the
 * workstation's wire codec converts entity-ref leaves to
 * `EntityRefStub` instances at load time, so by the time a TS
 * renderer reads the body it sees the shapes typed here.
 *
 * Structural invariant (enforced at the producer — the TS
 * subprocess's `flatten.ts` and `evaluate.ts`): every position
 * in an MDX tree is either a primitive leaf or a
 * `[type, props, children]` node tuple. The recursive types
 * below capture that so the renderer can dispatch via control-
 * flow narrowing without runtime tuple predicates.
 */

/** Leaf values in an MDX tree (rendered as text or skipped). */
export type MdxLeaf = string | number | boolean | null;

/** Node in an MDX tree: `[type, props, children]`. Recursive. */
export type MdxNode = readonly [string, Record<string, unknown>, readonly MdxChild[]];

/** A child position in an MDX tree — either a leaf or a node. */
export type MdxChild = MdxLeaf | MdxNode;

/** The wire shape of an MDX-compiled body field — an array of
 *  document-level children (the top-level `__fragment` wrapper
 *  is stripped by `evaluate.ts`'s `extractFragmentChildren`). */
export type MdxTree = readonly MdxChild[];

export interface RootReportValue {
	readonly title: string;
	readonly summary: string;
	readonly contents: MdxTree;
}

export interface SubReportValue {
	readonly label: string;
	readonly overview: MdxTree;
	readonly detail: MdxTree;
}
