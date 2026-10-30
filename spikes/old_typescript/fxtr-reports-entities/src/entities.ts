/**
 * Report entity definitions: RootReport, SubReport, SimpleComment, LinkedSVGFigure.
 *
 * These are pure data definitions with no rendering or React dependencies.
 * All use the "fxtr-reports" namespace for type IDs.
 */

import { UUID, defineType } from "@fxtr/core";
import * as bicodec from "@fxtr/core/bicodec";

const NS = UUID("ab39723e-d351-4cff-9cbb-eab4b686018c");

// ---- RootReport ----

const _RootReport = bicodec.entity(defineType(NS, "RootReport"), {
	/** Title of the report. Used in reference links and breadcrumbs. */
	title: bicodec.string(),
	/** Short summary for preview cards. */
	summary: bicodec.string(),
	/** MDX component tree fragment — the main content of the report. */
	contents: bicodec.array(bicodec.unknown()),
});
export interface RootReport extends bicodec.InferEntity<typeof _RootReport> {}
export const RootReport: bicodec.EntityDefinition<RootReport> = _RootReport;

// ---- SubReport ----

const _SubReport = bicodec.entity(defineType(NS, "SubReport"), {
	/** Short label for reference links and breadcrumbs. */
	label: bicodec.string(),
	/** Component tree fragment shown in figure mode. */
	overview: bicodec.array(bicodec.unknown()),
	/** Additional component tree fragment shown below overview in main view. */
	detail: bicodec.array(bicodec.unknown()),
});
export interface SubReport extends bicodec.InferEntity<typeof _SubReport> {}
export const SubReport: bicodec.EntityDefinition<SubReport> = _SubReport;

// ---- SimpleComment ----

const _SimpleComment = bicodec.entity(defineType(NS, "SimpleComment"), {
	/** Author name. */
	author: bicodec.string(),
	/** Comment text. */
	comment: bicodec.string(),
	/** Weak reference to the entity this comment is on. */
	target: bicodec.fxtrId(),
});
export interface SimpleComment extends bicodec.InferEntity<typeof _SimpleComment> {}
export const SimpleComment: bicodec.EntityDefinition<SimpleComment> = _SimpleComment;

// ---- LinkedSVGFigure ----

const _LinkedSVGFigure = bicodec.entity(defineType(NS, "LinkedSVGFigure"), {
	/** Caption / reference link text. */
	label: bicodec.string(),
	/** Single SVG component tree node: ["svg", {...}, [...]]. */
	svg: bicodec.unknown(),
	/** Maps data-preview-target attribute values to entity IDs (weak refs). */
	links: bicodec.record(bicodec.fxtrId()),
});
export interface LinkedSVGFigure extends bicodec.InferEntity<typeof _LinkedSVGFigure> {}
export const LinkedSVGFigure: bicodec.EntityDefinition<LinkedSVGFigure> = _LinkedSVGFigure;
