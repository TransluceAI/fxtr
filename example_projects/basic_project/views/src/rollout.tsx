// Renderers for the rollout experiment's types: ModelConfig, Scenario, Rubric, Transcript. Each type
// has a panel, an inline link, and a hover preview. A renderer casts its own id to the typed id once,
// at `unwrap(useEntity(id as EntityID<...>))`, which suspends onto the host's loading fallback until the data
// is there; everything it references is then typed (the transcript preview loads its model's name
// through `useEntity(data.model)` with no further cast). Panels, inline links, and previews
// start with the kind of thing they show (`Kind`: the type's short name).

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
import type { ModelConfigData, RubricData, ScenarioData, TranscriptData } from "./types";
import styles from "./views.module.css";

// ── ModelConfig ──

export function ModelConfigPanel({ id }: EntityViewProps) {
  const m = unwrap(useEntity(id as EntityID<ModelConfigData>));
  return (
    <div className={layout.panel}>
      <div className={layout.kindLine}><Kind>ModelConfig</Kind></div>
      <h2 className={layout.title}>{m.name}</h2>
      <FieldList fields={[["temperature", m.temperature]]} />
    </div>
  );
}
export function ModelConfigInlineLink({ id }: EntityViewProps) {
  const m = unwrap(useEntity(id as EntityID<ModelConfigData>));
  return <span className={layout.link}><Kind>ModelConfig</Kind> {m.name}</span>;
}
export function ModelConfigPreview({ id }: EntityViewProps) {
  const m = unwrap(useEntity(id as EntityID<ModelConfigData>));
  return (
    <div className={layout.preview}>
      <b>{m.name}</b> · temperature {m.temperature}
    </div>
  );
}

// ── Scenario ──

export function ScenarioPanel({ id }: EntityViewProps) {
  const s = unwrap(useEntity(id as EntityID<ScenarioData>));
  return (
    <div className={layout.panel}>
      <div className={layout.kindLine}><Kind>Scenario</Kind></div>
      <h2 className={layout.title}>{s.title}</h2>
      <p className={layout.prose}>{s.prompt}</p>
    </div>
  );
}
export function ScenarioInlineLink({ id }: EntityViewProps) {
  const s = unwrap(useEntity(id as EntityID<ScenarioData>));
  return <span className={layout.link}><Kind>Scenario</Kind> {s.title}</span>;
}
export function ScenarioPreview({ id }: EntityViewProps) {
  const s = unwrap(useEntity(id as EntityID<ScenarioData>));
  return (
    <div className={layout.preview}>
      <b>{s.title}</b>
      <p className={layout.prose}>{excerpt(s.prompt)}</p>
    </div>
  );
}

// ── Rubric ──

export function RubricPanel({ id }: EntityViewProps) {
  const r = unwrap(useEntity(id as EntityID<RubricData>));
  return (
    <div className={layout.panel}>
      <div className={layout.kindLine}><Kind>Rubric</Kind></div>
      <h2 className={layout.title}>{r.name}</h2>
      <ul className={styles.criteria}>
        {r.criteria.map((c, i) => (
          <li key={i}>{c}</li>
        ))}
      </ul>
    </div>
  );
}
export function RubricInlineLink({ id }: EntityViewProps) {
  const r = unwrap(useEntity(id as EntityID<RubricData>));
  return <span className={layout.link}><Kind>Rubric</Kind> {r.name}</span>;
}
export function RubricPreview({ id }: EntityViewProps) {
  const r = unwrap(useEntity(id as EntityID<RubricData>));
  return (
    <div className={layout.preview}>
      <b>{r.name}</b> · {r.criteria.length} criteria
    </div>
  );
}

// ── Transcript ──

/** The model's name, loaded through the typed id a transcript carries. */
export function ModelName({ id }: { id: EntityID<ModelConfigData> }) {
  const result = useEntity(id);
  return <>{result.status === "loaded" ? result.value.name : "…"}</>;
}

export function TranscriptPanel({ id }: EntityViewProps) {
  const t = unwrap(useEntity(id as EntityID<TranscriptData>));
  return (
    <div className={layout.panel}>
      <div className={layout.kindLine}><Kind>Transcript</Kind></div>
      <div className={layout.header}>
        <EntityLink id={t.model} /> on <EntityLink id={t.scenario} /> · {turnsLabel(t.messages.length)}
      </div>
      <MessageList messages={t.messages} />
    </div>
  );
}
export function TranscriptInlineLink({ id }: EntityViewProps) {
  const t = unwrap(useEntity(id as EntityID<TranscriptData>));
  return <span className={layout.link}><Kind>Transcript</Kind> {turnsLabel(t.messages.length)}</span>;
}
export function TranscriptPreview({ id }: EntityViewProps) {
  const t = unwrap(useEntity(id as EntityID<TranscriptData>));
  return (
    <div className={layout.preview}>
      <div className={layout.header}>
        <Kind>Transcript</Kind> <ModelName id={t.model} /> · {turnsLabel(t.messages.length)}
      </div>
      <FirstTurn messages={t.messages} />
    </div>
  );
}
