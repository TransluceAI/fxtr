// One judgment. Its header is laid out as the conversation views lay out a transcript's
// (entities.tsx): its kind, a status only when it failed (it has no answer), then a dim line with
// a chip that opens its judge's conversation and how its attempts went. Under it, the answer is one tree, in the
// order the output schema lists its fields, as Docent shows a judge's output: each field's key, then its value. A value (a yes or no, a number,
// one of an enum's values) is blue; prose follows its key, each citation in it a quote-mark chip
// that opens the cited entity at the lines it names; a field that is one citation is a chip that
// names those lines; a map or a list is a group that opens and closes, its entries set in under a
// rule. The judge conversation chip in the header opens how `judge` made the judgment in a pane of its own:
// its whole conversation with the model and the arguments `judge` was given. The panel's `judgment_part` param
// picks that pane or the output schema's (judgment_pane_params.ts, judgment_panes.tsx). The hover
// card and the inline link sum the answer up by its values.

import shared from "../behaviors.module.css";
import styles from "./judging.module.css";
import { useMemo, type ReactNode } from "react";
import {
  isDataMap, unwrap, useEntity, useEntityLinks,
  type DataMap, type DataValue, type EntityID, type EntityPanelProps, type EntityViewProps,
} from "fxtr-view";
import { countLabel } from "../entities";
import { AttemptOutcome } from "./attempt_outcome";
import { PartChip } from "./judge_arguments";
import { UnsentBlocksWarning } from "./unsent_blocks";
import { JUDGE_CONVERSATION_PARAMS, JUDGMENT_PART } from "./judgment_pane_params";
import { JudgeConversationView, SchemaView, attemptsLine } from "./judgment_panes";
import { outputSchemaOf, schemaAt, typeText } from "./output_schema";
import type { MessageSelector } from "../selector";
import { entityNameOf, judgmentOf, type AnswerField, type CitationData, type JudgmentData } from "./types";

export { outputSchemaOf, schemaAt, typeText } from "./output_schema";

/** What a summary of the answer (the hover card, the inline link) shows for a value that is not
 *  there: an empty field, or null. The answer's tree shows the value as it is. */
const MISSING = "—";

/** Whether two paths into the answer are the same. */
function samePath(a: readonly (string | number)[], b: readonly (string | number)[]): boolean {
  return a.length === b.length && a.every((part, index) => part === b[index]);
}

/** The params a transcript or an agent run opens at for an address, as behaviors writes
 *  them (judging/agent_run_evidence.py): a line of a block (`B3L4`, or `T1B3L4` in an agent run's
 *  second transcript) or a run of lines (`B3L4-6`). Undefined for an address of another shape,
 *  which includes a block with no lines (`B3`) and a run written last line first. */
export function selectorOf(address: string): { selector: MessageSelector } | undefined {
  const match = /^(?:T(\d+))?B(\d+)L(\d+)(?:-L?(\d+))?$/.exec(address);
  if (match === null) return undefined;
  const first = Number(match[3]);
  const last = match[4] === undefined ? first : Number(match[4]);
  if (last < first) return undefined;
  const block: MessageSelector = match[1] === undefined ? { message: Number(match[2]) } : { transcript: Number(match[1]), message: Number(match[2]) };
  return { selector: { ...block, lines: [first, last] } };
}

/** The label of the block that the lines of an address are in: `T1B3` for `T1B3L4-6`. An address
 *  that names no lines is returned as it is. */
export function blockOf(address: string): string {
  return address.replace(/L\d+(?:-L?\d+)?$/, "");
}

/** A citation as one chip, opening the cited entity in a pane of its own, at the cited lines when
 *  that is a transcript's (`selectorOf`, which its conversation view reads as a selector, scrolls
 *  to and highlights). With `quote`, as in prose, the chip is a quotation mark that names what it
 *  cites on hover; otherwise it names it (`transcript:B3L4`, or the entity's name alone). The chip
 *  is lit while what it opens is open. */
export function CitationChip({ id, name, address, quote = false }: { id: EntityID; name: string | null; address: string | null; quote?: boolean }) {
  const links = useEntityLinks();
  const params = address === null ? undefined : selectorOf(address);
  const label = [name, address].filter((part) => part !== null).join(":");
  return (
    <button type="button" className={styles[quote ? "quote" : "cite"]} aria-pressed={links.isOpen(id, params)} title={`Open ${label}`}
      {...(quote ? { "aria-label": `Open ${label}` } : {})} onClick={() => links.open(id, params)}>
      {quote ? <QuoteMark /> : label}
    </button>
  );
}

/** A quotation mark, the face of a citation chip in prose. */
function QuoteMark() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M10 7H5.5A1.5 1.5 0 0 0 4 8.5V13a1.5 1.5 0 0 0 1.5 1.5H8c0 1.6-.9 2.6-2.6 3l.5 1.6C9 18.4 11 16.3 11 12.6V8a1 1 0 0 0-1-1zm9.5 0H15a1 1 0 0 0-1 1v5a1.5 1.5 0 0 0 1.5 1.5H18c0 1.6-.9 2.6-2.6 3l.5 1.6c3.1-.7 5.1-2.8 5.1-6.5V8.5A1.5 1.5 0 0 0 19.5 7z" />
    </svg>
  );
}

/** The index in `text` of its code point number `offset`. A citation's offsets count code points,
 *  as Python indexes a string, and a JavaScript string is indexed by UTF-16 unit: a character
 *  outside the Basic Multilingual Plane, such as an emoji, is one code point and two units, so
 *  the two differ after one. An offset past the end gives the text's length. */
export function indexOfCodePoint(text: string, offset: number): number {
  let index = 0;
  for (let count = 0; count < offset && index < text.length; count++) index += text.codePointAt(index)! > 0xffff ? 2 : 1;
  return index;
}

/** A string of the answer as prose, inline, its line breaks kept, each citation in it a chip that
 *  opens what it cites (a quotation mark with `quote`). */
export function CitedText({ text, citations, judgment, quote = false }: { text: string; citations: readonly CitationData[]; judgment: JudgmentData; quote?: boolean }) {
  const ordered = [...citations].sort((a, b) => a.start_in_field - b.start_in_field);
  const pieces: ReactNode[] = [];
  let at = 0;
  ordered.forEach((citation, index) => {
    pieces.push(text.slice(at, indexOfCodePoint(text, citation.start_in_field)));
    pieces.push(<CitationChip key={index} id={citation.entity_id} name={entityNameOf(judgment, citation.entity_id)} address={citation.address_in_entity} quote={quote} />);
    at = indexOfCodePoint(text, citation.end_in_field);
  });
  pieces.push(text.slice(at));
  return <span className={shared["prose"]}>{pieces}</span>;
}

/** A string of the answer with its citations taken out, in one line: for an excerpt, where a
 *  citation cannot be a chip. */
function withoutCitations(text: string, citations: readonly CitationData[]): string {
  const ordered = [...citations].sort((a, b) => a.start_in_field - b.start_in_field);
  let at = 0;
  let kept = "";
  for (const citation of ordered) {
    kept += text.slice(at, indexOfCodePoint(text, citation.start_in_field));
    at = indexOfCodePoint(text, citation.end_in_field);
  }
  kept += text.slice(at);
  return kept.replace(/\s+/g, " ").replace(/ ([.,;:!?)])/g, "$1").trim();
}

/** The values a field could have had, as its schema says: `true/false`, an enum's values, a
 *  number's bounds. Null when the schema does not say. */
export function rangeOf(schema: DataMap | null, path: readonly (string | number)[]): string | null {
  const at = schemaAt(schema, path);
  if (at === undefined) return null;
  const choices = at["enum"];
  if (Array.isArray(choices)) return (choices as readonly DataValue[]).map(String).join("/");
  if (at["type"] === "boolean") return "true/false";
  const low = at["minimum"];
  const high = at["maximum"];
  if (typeof low === "number" && typeof high === "number") return `${low}-${high}`;
  if (typeof low === "number") return `${low} or more`;
  if (typeof high === "number") return `${high} or less`;
  return null;
}

/** The entries of a map of the answer in the order its schema lists them (as the judge was
 *  asked), then any the schema does not name. A stored answer does not keep its own order. */
export function schemaOrdered(value: DataMap, schema: DataMap | null, path: readonly (string | number)[]): [string, DataValue][] {
  const properties = schemaAt(schema, path)?.["properties"];
  const listed = isDataMap(properties) ? Object.keys(properties) : [];
  const entries = Object.entries(value) as [string, DataValue][];
  const rank = (key: string) => (listed.includes(key) ? listed.indexOf(key) : listed.length);
  return entries.map((entry, index) => ({ entry, index })).sort((a, b) => rank(a.entry[0]) - rank(b.entry[0]) || a.index - b.index).map(({ entry }) => entry);
}

/** A value of the answer, in blue, with the values it could have been after it when `range` is
 *  given. A value that is not there (null) is a dash. */
export function Scalar({ value, range = null }: { value: DataValue; range?: string | null }) {
  if (value === null) return <span className={styles["missing"]}>{MISSING}</span>;
  return (
    <>
      <span className={styles["scalar"]}>{valueText(value)}</span>
      {range !== null && <span className={styles["range"]}> [{range}]</span>}
    </>
  );
}

/** A value as text: a list's items as JSON, a dash for null. */
function valueText(value: DataValue): string {
  if (value === null) return MISSING;
  if (Array.isArray(value)) return (value as readonly DataValue[]).map((item) => JSON.stringify(item)).join(", ");
  return String(value);
}

/** What a field that points at one place holds: the one citation that is its whole text, or
 *  null when it is empty. Undefined for any other field: a pointer is a string that is exactly
 *  one citation, or an empty string in a field whose schema gives it a format (it would hold a
 *  citation). Such a field is one of the answer's values, not its prose. */
export function pointerOf(judgment: JudgmentData, schema: DataMap | null, path: readonly (string | number)[], value: DataValue): CitationData | null | undefined {
  if (typeof value !== "string") return undefined;
  if (value === "") return typeof schemaAt(schema, path)?.["format"] === "string" ? null : undefined;
  return judgment.citations.find((citation) =>
    samePath(citation.path_to_output_field, path) && citation.start_in_field === 0 && indexOfCodePoint(value, citation.end_in_field) === value.length);
}

/** One field of the answer at the end of its path: `lethal_means.score`, `findings[0].match`. */
interface FlatField {
  readonly key: string;
  readonly path: readonly (string | number)[];
  readonly value: DataValue;
}

/** The answer's fields, each by its full path, in the order its schema lists them. A map is read
 *  into, and so is a list of maps; anything else is one field. */
export function flatFields(value: DataMap, schema: DataMap | null, path: readonly (string | number)[] = []): FlatField[] {
  return schemaOrdered(value, schema, path).flatMap(([name, entry]): FlatField[] => {
    const at = [...path, name];
    if (isDataMap(entry)) return flatFields(entry, schema, at);
    if (Array.isArray(entry) && entry.length > 0 && entry.every(isDataMap))
      return (entry as readonly DataMap[]).flatMap((item, index) => flatFields(item, schema, [...at, index]));
    return [{ key: keyOf(at), path: at, value: entry }];
  });
}

/** A path as one key: names joined by dots, positions in brackets (`findings[0].match`). */
export function keyOf(path: readonly (string | number)[]): string {
  return path.map((part, index) => (typeof part === "number" ? `[${part}]` : index === 0 ? part : `.${part}`)).join("");
}

/** A field's name, underlined when its schema describes it: hovering or focusing it shows a card
 *  with its full key, its description, what it holds, and its parent object's description. */
function FieldKey({ label, path, schema }: { label: string; path: readonly (string | number)[]; schema: DataMap | null }) {
  const own = schemaAt(schema, path);
  const parentPath = path.slice(0, path.length - 1);
  // A field of an item of a list has the list's object as its parent.
  const objectPath = typeof parentPath.at(-1) === "number" ? parentPath.slice(0, -1) : parentPath;
  const parent = objectPath.length === 0 ? undefined : schemaAt(schema, objectPath);
  const description = typeof own?.["description"] === "string" ? own["description"] : null;
  const holds = own === undefined ? null : typeText(own);
  const parentDescription = typeof parent?.["description"] === "string" ? parent["description"] : null;
  if (description === null && holds === null && parentDescription === null) return <span className={styles["fieldKey"]}>{label}</span>;
  const key = keyOf(path);
  return (
    <span className={styles["fieldKey"]} data-described="" tabIndex={0}>
      {label}
      <span className={styles["hoverCard"]} role="tooltip">
        <span className={styles["hoverName"]}>{key}</span>
        {description !== null && <span>{description}</span>}
        {holds !== null && <span className={styles["hoverType"]}>{holds}</span>}
        {parentDescription !== null && <span className={styles["hoverParent"]}><code>{keyOf(objectPath)}</code>: {parentDescription}</span>}
      </span>
    </span>
  );
}

/** A value of the answer that is neither a map nor a list, as the tree shows it after its key:
 *  prose with its citations as quote chips, a field that is one citation as a chip naming what
 *  it cites, any other value in blue, and null or an empty string as it is. */
function LeafValue({ path, value, judgment, schema }: { path: readonly (string | number)[]; value: DataValue; judgment: JudgmentData; schema: DataMap | null }) {
  const pointer = pointerOf(judgment, schema, path, value);
  if (pointer !== undefined && pointer !== null) return <CitationChip id={pointer.entity_id} name={entityNameOf(judgment, pointer.entity_id)} address={pointer.address_in_entity} />;
  if (value === null) return <span className={styles["literal"]}>null</span>;
  if (value === "") return <span className={styles["literal"]}>""</span>;
  if (typeof value === "string" && rangeOf(schema, path) === null) {
    const citations = judgment.citations.filter((citation) => samePath(citation.path_to_output_field, path));
    return <CitedText text={value} citations={citations} judgment={judgment} quote />;
  }
  return <Scalar value={value} />;
}

/** A chevron that points right while its group is closed and down while it is open. */
function Chevron() {
  return (
    <svg className={styles["chevron"]} width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M6 3l5 5-5 5" />
    </svg>
  );
}

/** One entry of the answer's tree: its key, then its value. A map (its fields in the schema's
 *  order) or a list is a group headed by its key and its size, open to start with near the top,
 *  its entries set in under a rule. */
function AnswerEntry({ label, path, value, judgment, schema }: {
  label: string;
  path: readonly (string | number)[];
  value: DataValue;
  judgment: JudgmentData;
  schema: DataMap | null;
}) {
  const key = <span className={styles["key"]}><FieldKey label={label} path={path} schema={schema} />:</span>;
  const map = isDataMap(value);
  if (!map && !Array.isArray(value)) {
    return <div className={styles["entry"]}>{key} <LeafValue path={path} value={value} judgment={judgment} schema={schema} /></div>;
  }
  const entries: [string, string | number, DataValue][] = map
    ? schemaOrdered(value, schema, path).map(([name, item]) => [name, name, item])
    : (value as readonly DataValue[]).map((item, index) => [`[${index}]`, index, item]);
  if (entries.length === 0) return <div className={styles["entry"]}>{key} <span className={styles["literal"]}>{map ? "{}" : "[]"}</span></div>;
  return (
    <details className={styles["group"]} open={path.length < 2}>
      <summary>
        <Chevron />{key} <span className={styles["size"]}>{map ? `{${countLabel(entries.length, "field")}}` : `[${countLabel(entries.length, "item")}]`}</span>
      </summary>
      <div className={styles["children"]}>
        {entries.map(([name, part, item]) => <AnswerEntry key={name} label={name} path={[...path, part]} value={item} judgment={judgment} schema={schema} />)}
      </div>
    </details>
  );
}

/** Whether a field's schema says it holds a number or a yes or no (perhaps or null). */
function holdsScalar(schema: DataMap | null, path: readonly (string | number)[]): boolean {
  const type = schemaAt(schema, path)?.["type"];
  const types: readonly DataValue[] = Array.isArray(type) ? type : [type ?? null];
  return types.some((name) => name === "integer" || name === "number" || name === "boolean");
}

/** The answer's scalar fields, each with the values it could have been: what a judgment comes to,
 *  at a glance, in the order the schema lists them. A field holding a nested yes-or-no answer is
 *  read as its `match`. The fields of any other nested map are read too, each keyed by its path
 *  joined with dots (`suicidal_ideation.score`), so an answer to several questions at once has a
 *  field for each question's values. A field that points at one place is one of them too. */
export function answerFields(judgment: JudgmentData): AnswerField[] {
  if (judgment.output === null) return [];
  const schema = outputSchemaOf(judgment);
  const fieldsOf = (map: DataMap, at: readonly string[]): AnswerField[] => schemaOrdered(map, schema, at).flatMap(([name, value]) => {
    const nested = isDataMap(value) && "match" in value;
    if (isDataMap(value) && !nested) return fieldsOf(value, [...at, name]);
    const key = [...at, name].join(".");
    const path = nested ? [...at, name, "match"] : [...at, name];
    const answer = nested ? (value["match"] ?? null) : value;
    const range = rangeOf(schema, path);
    // A field left empty (null) is still one of the answer's scalars when its schema says it
    // would hold a number or a yes or no, so a table of answers keeps its column.
    const scalar = typeof answer === "boolean" || typeof answer === "number" || (typeof answer === "string" && range !== null)
      || (answer === null && holdsScalar(schema, path));
    // A field that points at one place is a value too: the address it points at, or nothing.
    const pointer = pointerOf(judgment, schema, path, answer);
    if (pointer !== undefined) {
      const address = pointer === null ? null : (pointer.address_in_entity ?? entityNameOf(judgment, pointer.entity_id));
      return [{ key, path, value: address, range: null, citation: pointer }];
    }
    return scalar ? [{ key, path, value: answer, range }] : [];
  });
  return fieldsOf(judgment.output, []);
}

/** The answer's values in a row, each field's key then its value: what a judgment's hover card
 *  shows. A nested field is keyed by its path joined with dots (`lethal_means.score`). Each
 *  value's range follows it when there are at most three. */
export function AnswerLine({ judgment }: { judgment: JudgmentData }) {
  const fields = answerFields(judgment);
  const ranges = fields.length <= 3;
  return (
    <div className={styles["verdict"]}>
      {fields.map((field) => (
        <span className={styles["answer"]} key={field.key}>
          <span className={styles["key"]}>{field.key}:</span>
          <Scalar value={field.value} range={ranges ? field.range : null} />
        </span>
      ))}
    </div>
  );
}

/** The first field of the answer that is prose, without its citations, cut to an excerpt. Null
 *  when the answer has no prose. */
function answerExcerpt(judgment: JudgmentData): string | null {
  if (judgment.output === null) return null;
  const schema = outputSchemaOf(judgment);
  for (const field of flatFields(judgment.output, schema)) {
    const { value, path } = field;
    if (typeof value !== "string" || value === "" || rangeOf(schema, path) !== null || pointerOf(judgment, schema, path, value) !== undefined) continue;
    const text = withoutCitations(value, judgment.citations.filter((citation) => samePath(citation.path_to_output_field, path)));
    return text.length > 200 ? `${text.slice(0, 200)}…` : text;
  }
  return null;
}

/** A judgment's header: its kind; `Failed` when it has no answer; a chip that opens the conversation
 *  pane, then how its attempts went; and the blocks of a judged transcript or agent run that the
 *  judge was not sent. */
function JudgmentHeader({ judgment, id }: { judgment: JudgmentData; id: EntityID | undefined }) {
  return (
    <header className={shared["header"]}>
      <span className={shared["kind"]}>Judgment</span>
      {judgment.output === null && <span className={shared["error"]}>Failed</span>}
      <div className={shared["muted"]}>
        <PartChip id={id} params={JUDGE_CONVERSATION_PARAMS}>judge conversation</PartChip> {attemptsLine(judgment)}
      </div>
      <UnsentBlocksWarning judgment={judgment} />
    </header>
  );
}

/** The answer as one tree, its fields in the order the output schema lists them. */
function AnswerTree({ judgment, output }: { judgment: JudgmentData; output: DataMap }) {
  const schema = useMemo(() => outputSchemaOf(judgment), [judgment]);
  return (
    <section className={`${shared["exchange"]} ${styles["tree"]}`} aria-label="Answer">
      {schemaOrdered(output, schema, []).map(([name, value]) => (
        <AnswerEntry key={name} label={name} path={[name]} value={value} judgment={judgment} schema={schema} />
      ))}
    </section>
  );
}

/** The judgment as its panel draws it: its header, then its answer as a tree; or, when it failed,
 *  what came of the last attempt. */
export function JudgmentDetail({ judgment, id }: { judgment: JudgmentData; id?: EntityID }) {
  const last = judgment.attempts.at(-1);
  return (
    <>
      <JudgmentHeader judgment={judgment} id={id} />
      {judgment.output !== null ? <AnswerTree judgment={judgment} output={judgment.output} />
        : last === undefined ? <p className={shared["muted"]}>No attempt recorded.</p>
        : (
          <section className={shared["exchange"]} aria-label="Last attempt">
            <h3 className={shared["turn-number"]}>Last attempt</h3>
            <AttemptOutcome attempt={last} />
          </section>
        )}
    </>
  );
}

/** A judgment's panel: the whole judgment, or the part its `judgment_part` param names. A judgment
 *  stored in a shape these views do not read says so, and its data tree shows it. */
export function JudgmentPanel({ id, params }: EntityPanelProps) {
  const data = unwrap(useEntity(id));
  const judgment = useMemo(() => judgmentOf(data), [data]);
  const part = params?.[JUDGMENT_PART];
  if (judgment === null) return <UnreadableJudgment />;
  return (
    <section className={shared["panel"]} aria-label="Judgment">
      {part === "judge_conversation" ? <JudgeConversationView judgment={judgment} id={id} />
        : part === "output_schema" ? <SchemaView judgment={judgment} />
        : <JudgmentDetail judgment={judgment} id={id} />}
    </section>
  );
}

/** What a judgment in a shape these views do not read shows in their place. */
function UnreadableJudgment() {
  return (
    <section className={shared["panel"]} aria-label="Judgment">
      <header className={shared["header"]}>
        <span className={shared["kind"]}>Judgment</span>
        <span className={shared["muted"]}>Earlier format</span>
      </header>
      <p className={shared["muted"]}>
        This judgment was stored by an earlier version of behaviors, in a format this viewer does not read.
        Choose the data view to see what it holds.
      </p>
    </section>
  );
}

/** A judgment where a shell links to it by its inline view, as the shell writes a link of its own:
 *  its kind, then its values, in the link's own font, so the link's pill cuts the line off where
 *  it runs out (`Judgment · match false · overall good`). The values' ranges are in its tooltip.
 *  An answer with no values shows the start of its prose. */
export function JudgmentInlineLink({ id }: EntityViewProps) {
  const judgment = judgmentOf(unwrap(useEntity(id)));
  if (judgment === null) return <span>Judgment · earlier format</span>;
  if (judgment.output === null) return <span>Judgment · failed</span>;
  const fields = answerFields(judgment);
  if (fields.length === 0) return <span>Judgment · {answerExcerpt(judgment)}</span>;
  const spelled = fields.map((field) => `${field.key}: ${valueText(field.value)}${field.range === null ? "" : ` [${field.range}]`}`).join("; ");
  return <span title={spelled}>{["Judgment", ...fields.map((field) => `${field.key} ${valueText(field.value)}`)].join(" · ")}</span>;
}

/** A judgment in the card a shell shows when a link to it is hovered, as a transcript's is: its
 *  kind, its values, how its attempts went (and that it failed, when it did), and the start of its
 *  first field of prose. */
export function JudgmentPreview({ id }: EntityViewProps) {
  const judgment = judgmentOf(unwrap(useEntity(id)));
  if (judgment === null) {
    return (
      <div className={shared["preview"]}>
        <span className={shared["kind"]}>Judgment</span>
        <div className={shared["muted"]}>stored in an earlier format</div>
      </div>
    );
  }
  const excerpt = answerExcerpt(judgment);
  return (
    <div className={shared["preview"]}>
      <span className={shared["kind"]}>Judgment</span>
      {answerFields(judgment).length > 0 && <AnswerLine judgment={judgment} />}
      <div className={shared[judgment.output === null ? "error" : "muted"]}>
        {[...(judgment.output === null ? ["Failed"] : []), attemptsLine(judgment)].join(" · ")}
      </div>
      {excerpt !== null && <p className={shared["prose"]}>{excerpt}</p>}
    </div>
  );
}
