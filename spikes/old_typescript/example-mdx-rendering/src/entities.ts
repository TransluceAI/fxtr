/**
 * Fxtr entity definitions for the MDX-to-JSON spike.
 */

import { UUID, defineType } from "@fxtr/core";
import * as bicodec from "@fxtr/core/bicodec";

const NS = UUID("97e2837d-5a2a-421e-958d-382c5be24df2");

// ---- ComponentTree: the top-level entity for a rendered MDX document ----

const _ComponentTree = bicodec.entity(defineType(NS, "ComponentTree"), {
	document: bicodec.array(bicodec.unknown()),
});
export interface ComponentTree extends bicodec.InferEntity<typeof _ComponentTree> {}
export const ComponentTree: bicodec.EntityDefinition<ComponentTree> = _ComponentTree;

// ---- Stats: example data entity stored by the FxtrStats component ----

const _Stats = bicodec.entity(defineType(NS, "Stats"), {
	users: bicodec.number(),
	revenue: bicodec.string(),
	growth: bicodec.string(),
});
export interface Stats extends bicodec.InferEntity<typeof _Stats> {}
export const Stats: bicodec.EntityDefinition<Stats> = _Stats;
