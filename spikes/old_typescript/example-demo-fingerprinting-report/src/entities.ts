/**
 * Fingerprint report entity definitions.
 *
 * - CategoryAggregate: a category within a dimension, with prevalence and transcript links
 * - RubricText: the rubric description for a dimension/category
 * - Transcript: raw conversation data (messages, metadata, sim bio link)
 * - FingerprintedTranscript: links a Transcript to its grader results
 * - FingerprintGraderResult: one dimension's grading of one transcript
 * - SimulatorBio: a user simulator's persona and configuration
 */

import { UUID, defineType } from "@fxtr/core";
import * as bicodec from "@fxtr/core/bicodec";

const NS = UUID("f7a1c3e2-8b4d-4f6a-9e2c-1d5b7a3f8e0c");

// ---- CategoryAggregate ----

const _CategoryAggregate = bicodec.entity(defineType(NS, "CategoryAggregate"), {
	label: bicodec.string(),
	dimensionName: bicodec.string(),
	prevalence: bicodec.number(),
	transcriptCount: bicodec.number(),
	rubricExcerpt: bicodec.string(),
	rubric: bicodec.fxtrId(),
	transcripts: bicodec.array(bicodec.fxtrId()),
});
export interface CategoryAggregate extends bicodec.InferEntity<typeof _CategoryAggregate> {}
export const CategoryAggregate: bicodec.EntityDefinition<CategoryAggregate> = _CategoryAggregate;

// ---- RubricText ----

const _RubricText = bicodec.entity(defineType(NS, "RubricText"), {
	label: bicodec.string(),
	categoryName: bicodec.string(),
	text: bicodec.string(),
});
export interface RubricText extends bicodec.InferEntity<typeof _RubricText> {}
export const RubricText: bicodec.EntityDefinition<RubricText> = _RubricText;

// ---- Transcript (raw conversation) ----

const _Transcript = bicodec.entity(defineType(NS, "Transcript"), {
	label: bicodec.string(),
	transcriptId: bicodec.string(),
	simKey: bicodec.string(),
	familyName: bicodec.string(),
	modelName: bicodec.string(),
	messages: bicodec.array(
		bicodec.object({
			role: bicodec.string(),
			content: bicodec.string(),
		}),
	),
	simulatorBio: bicodec.fxtrId(),
});
export interface Transcript extends bicodec.InferEntity<typeof _Transcript> {}
export const Transcript: bicodec.EntityDefinition<Transcript> = _Transcript;

// ---- FingerprintedTranscript (transcript + grader results) ----

const _FingerprintedTranscript = bicodec.entity(defineType(NS, "FingerprintedTranscript"), {
	label: bicodec.string(),
	transcriptId: bicodec.string(),
	simKey: bicodec.string(),
	familyName: bicodec.string(),
	modelName: bicodec.string(),
	transcript: bicodec.fxtrId(),
	simulatorBio: bicodec.fxtrId(),
	graderResults: bicodec.array(bicodec.fxtrId()),
});
export interface FingerprintedTranscript
	extends bicodec.InferEntity<typeof _FingerprintedTranscript> {}
export const FingerprintedTranscript: bicodec.EntityDefinition<FingerprintedTranscript> =
	_FingerprintedTranscript;

// ---- FingerprintGraderResult ----

const _FingerprintGraderResult = bicodec.entity(defineType(NS, "FingerprintGraderResult"), {
	label: bicodec.string(),
	dimensionName: bicodec.string(),
	reasoning: bicodec.string(),
	activeCategories: bicodec.array(bicodec.string()),
	allCategories: bicodec.array(bicodec.string()),
	rubric: bicodec.fxtrId(),
	transcript: bicodec.fxtrId(),
});
export interface FingerprintGraderResult
	extends bicodec.InferEntity<typeof _FingerprintGraderResult> {}
export const FingerprintGraderResult: bicodec.EntityDefinition<FingerprintGraderResult> =
	_FingerprintGraderResult;

// ---- SimulatorBio ----

const _SimulatorBio = bicodec.entity(defineType(NS, "SimulatorBio"), {
	label: bicodec.string(),
	bio: bicodec.string(),
	description: bicodec.string(),
	tags: bicodec.array(bicodec.string()),
	initialMessage: bicodec.string(),
});
export interface SimulatorBio extends bicodec.InferEntity<typeof _SimulatorBio> {}
export const SimulatorBio: bicodec.EntityDefinition<SimulatorBio> = _SimulatorBio;
