/**
 * Shared entity definitions for the example scripts.
 */

import { UUID, defineType } from "@fxtr/core";
import * as bicodec from "@fxtr/core/bicodec";

const NS = UUID("545552bf-ecb9-4f85-a45f-9ce853c21e94");

const _Author = bicodec.entity(defineType(NS, "Author"), {
	name: bicodec.string(),
	email: bicodec.string(),
});
export interface Author extends bicodec.InferEntity<typeof _Author> {}
export const Author: bicodec.EntityDefinition<Author> = _Author;

const _Tag = bicodec.entity(defineType(NS, "Tag"), {
	label: bicodec.string(),
});
export interface Tag extends bicodec.InferEntity<typeof _Tag> {}
export const Tag: bicodec.EntityDefinition<Tag> = _Tag;

const _Article = bicodec.entity(defineType(NS, "Article"), {
	title: bicodec.string(),
	body: bicodec.string(),
	author: bicodec.childRef(Author),
	tags: bicodec.array(bicodec.childRef(Tag)),
});
export interface Article extends bicodec.InferEntity<typeof _Article> {}
export const Article: bicodec.EntityDefinition<Article> = _Article;
