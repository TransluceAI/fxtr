/**
 * Rich typed views for example entity types.
 * Demonstrates cascading useFxtrLoad with Suspense.
 */

import type { Entity, EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { Suspense } from "react";
import { Article, Author, Tag } from "../entities.js";
import type { Article as ArticleType, Author as AuthorType, Tag as TagType } from "../entities.js";

/**
 * Dispatches to the right view based on what entity type loads.
 * Uses the union codec to load any of the known example types.
 */
export function RichEntityView({ entityRef }: { entityRef: EntityRef<Entity> }) {
	const entity = useFxtrLoad(entityRef);

	// Dispatch based on which definition created the entity.
	// The union codec gives us one of Article, Author, or Tag instances.
	if ("title" in entity && "body" in entity) {
		return <ArticleView article={entity as ArticleType} />;
	}
	if ("name" in entity && "email" in entity) {
		return <AuthorView author={entity as AuthorType} />;
	}
	if ("label" in entity) {
		return <TagView tag={entity as TagType} />;
	}

	return <pre>Unknown entity shape: {JSON.stringify(entity, null, 2)}</pre>;
}

function ArticleView({ article }: { article: ArticleType }) {
	return (
		<div>
			<h2>{article.title}</h2>
			<p>{article.body}</p>
			<Suspense fallback={<span className="loading">Loading author...</span>}>
				<AuthorBadge authorRef={article.author} />
			</Suspense>
			<Suspense fallback={<span className="loading">Loading tags...</span>}>
				<TagList tagRefs={article.tags} />
			</Suspense>
		</div>
	);
}

function AuthorView({ author }: { author: AuthorType }) {
	return (
		<div>
			<h2>Author</h2>
			<p>
				{author.name} &lt;{author.email}&gt;
			</p>
		</div>
	);
}

function TagView({ tag }: { tag: TagType }) {
	return (
		<div>
			<h2>Tag</h2>
			<span className="tag">{tag.label}</span>
		</div>
	);
}

function AuthorBadge({ authorRef }: { authorRef: EntityRef<AuthorType> }) {
	const author = useFxtrLoad(authorRef);
	return (
		<p className="author">
			By {author.name} &lt;{author.email}&gt;
		</p>
	);
}

function TagList({ tagRefs }: { tagRefs: EntityRef<TagType>[] }) {
	return (
		<div>
			{tagRefs.map((ref) => (
				<Suspense key={ref.entityId.key()} fallback={<span className="tag loading">...</span>}>
					<TagBadge tagRef={ref} />
				</Suspense>
			))}
		</div>
	);
}

function TagBadge({ tagRef }: { tagRef: EntityRef<TagType> }) {
	const tag = useFxtrLoad(tagRef);
	return <span className="tag">{tag.label}</span>;
}

/** Union codec that handles all example entity types. */
export { Article, Author, Tag };
