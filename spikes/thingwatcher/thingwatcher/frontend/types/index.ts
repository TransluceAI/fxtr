// GraphQL response types

export interface Tick {
  index: number;
  timestamp: number;
  level: string;
  type: string;
  desc: unknown;
}

export interface NewTickPayload {
  tick: Tick;
  triggerableKeys: string[];
}

/** Where a thing's data came from */
export type ThingSourceKind = 'SNAPSHOT' | 'STORE';

/** Information about a source of a thing's data */
export interface ThingSource {
  kind: ThingSourceKind;
  storeName?: string;  // Present when kind=STORE
  overridden: boolean; // True if this source's state differs from the resolved state
}

export interface Thing {
  shortId: number;
  key: string;
  keyJSON?: unknown;
  thingType: string;
  thingstateId: string;
  links: Link[];
  backlinks?: Link[];  // Optional since not all queries fetch it
  data?: unknown;
  dataChunks?: DataChunk[];
  sources?: ThingSource[];  // Optional since not all queries fetch it
}

export interface Link {
  linkType: string;
  linkKey: string;
  linkKeyJSON?: unknown;
  fromThing: {
    shortId: number;
    key: string;
    keyJSON?: unknown;
    thingType: string;
  };
  toThing: {
    shortId: number;
    key: string;
    keyJSON?: unknown;
    thingType: string;
  };
}

export interface DataChunk {
  id: string;
  data: unknown;
}

export interface ChangeInfo {
  addedKeys: Set<string>;
  modifiedKeys: Set<string>;
  disappearingKeys: Set<string>;
}

export interface TriggerResult {
  ok: boolean;
  triggerId?: number;
  error?: string;
}

export interface TriggerRecord {
  id: number;
  triggerTick: number;
  thing: { shortId: number; key: string };
  payload: unknown;
}

export interface Root {
  name: string;
  thing: {
    shortId: number;
    key: string;
    keyJSON?: unknown;
    thingType: string;
  };
}

/** Reference from a thing type to a specific renderer within a module */
export interface RendererReference {
  moduleId: string;
  rendererKey: string;
}

/** Renderer module containing TSX source code (fetched by module ID) */
export interface RendererModule {
  id: string;
  main: string;
  imports: Record<string, string>;
}

export interface ThingTypeConfig {
  thingType: string;
  color: string;
  linkColors: { linkType: string; color: string }[];
  renderer?: RendererReference | null;
}

// App state types

export interface AppState {
  selectedThingKey: string | null;  // b64 key - canonical identifier
  levelFilter: string[];
}
