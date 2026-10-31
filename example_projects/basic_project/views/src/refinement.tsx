// Renderers for the bio refinement experiment's types: Bio, RewriteInstructions,
// SimulatedTranscript. Same shape as rollout.tsx.

import { EntityLink, unwrap, useEntity, type EntityID, type EntityViewProps } from "fxtr-view";
import {
  excerpt,
  FieldList,
  FirstTurn,
  Kind,
  layout,
  MessageList,
  turnsLabel,
} from "fxtr-view-kit";
import { ModelName } from "./rollout";
import type { BioData, RewriteInstructionsData, SimulatedTranscriptData } from "./types";

// ── Bio ──

export function BioPanel({ id }: EntityViewProps) {
  const b = unwrap(useEntity(id as EntityID<BioData>));
  return (
    <div className={layout.panel}>
      <div className={layout.kindLine}><Kind>Bio</Kind></div>
      <h2 className={layout.title}>{b.name}</h2>
      <p className={layout.prose}>{b.persona}</p>
      <FieldList fields={[["detail", b.detail]]} />
    </div>
  );
}
export function BioInlineLink({ id }: EntityViewProps) {
  const b = unwrap(useEntity(id as EntityID<BioData>));
  return <span className={layout.link}><Kind>Bio</Kind> {b.name}</span>;
}
export function BioPreview({ id }: EntityViewProps) {
  const b = unwrap(useEntity(id as EntityID<BioData>));
  return (
    <div className={layout.preview}>
      <b>{b.name}</b> · detail {b.detail}
      <p className={layout.prose}>{excerpt(b.persona)}</p>
    </div>
  );
}

// ── RewriteInstructions ──

export function RewriteInstructionsPanel({ id }: EntityViewProps) {
  const r = unwrap(useEntity(id as EntityID<RewriteInstructionsData>));
  return (
    <div className={layout.panel}>
      <div className={layout.kindLine}><Kind>RewriteInstructions</Kind></div>
      <h2 className={layout.title}>rewrite: {r.style}</h2>
      <FieldList fields={[["target detail", r.target_detail]]} />
    </div>
  );
}
export function RewriteInstructionsInlineLink({ id }: EntityViewProps) {
  const r = unwrap(useEntity(id as EntityID<RewriteInstructionsData>));
  return <span className={layout.link}><Kind>RewriteInstructions</Kind> {r.style}</span>;
}
export function RewriteInstructionsPreview({ id }: EntityViewProps) {
  const r = unwrap(useEntity(id as EntityID<RewriteInstructionsData>));
  return (
    <div className={layout.preview}>
      <b>{r.style}</b> · target detail {r.target_detail}
    </div>
  );
}

// ── SimulatedTranscript ──

export function SimulatedTranscriptPanel({ id }: EntityViewProps) {
  const t = unwrap(useEntity(id as EntityID<SimulatedTranscriptData>));
  return (
    <div className={layout.panel}>
      <div className={layout.kindLine}><Kind>SimulatedTranscript</Kind></div>
      <div className={layout.header}>
        <EntityLink id={t.model} /> as <EntityLink id={t.bio} /> · {turnsLabel(t.messages.length)}
      </div>
      <MessageList messages={t.messages} />
    </div>
  );
}
export function SimulatedTranscriptInlineLink({ id }: EntityViewProps) {
  const t = unwrap(useEntity(id as EntityID<SimulatedTranscriptData>));
  return <span className={layout.link}><Kind>SimulatedTranscript</Kind> {turnsLabel(t.messages.length)}</span>;
}
export function SimulatedTranscriptPreview({ id }: EntityViewProps) {
  const t = unwrap(useEntity(id as EntityID<SimulatedTranscriptData>));
  return (
    <div className={layout.preview}>
      <div className={layout.header}>
        <Kind>SimulatedTranscript</Kind> <ModelName id={t.model} /> · {turnsLabel(t.messages.length)}
      </div>
      <FirstTurn messages={t.messages} />
    </div>
  );
}
