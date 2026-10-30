# Panel Spike: Basics

This phase sets up the foundation: data layer, basic dockview, and stub panels.

## Component Hierarchy (Phase 1)

```
<App>
  <RenderManager data={...}>   ← owns dockview, takes data as prop
    ├── InPortalContainer      ← hidden div with InPortals
    │   └── InPortal[dummy]    ← proof-of-concept: renders "Hello World"
    │
    └── DockviewReact
        ├── DebugPanel         ← placeholder for now
        ├── EntrypointPanel    ← shows links to top-level entities
        └── DummyEntityPanel   ← proof-of-concept: renders OutPortal
  </RenderManager>
</App>
```

## File Structure

```
ts/src/
├── data/
│   ├── schema.ts           # TypeScript types for all entity types
│   ├── example-data.ts     # The static test dataset
│   └── ownership.ts        # Compute ownership graph, helper functions
│
├── render-manager/
│   └── RenderManager.tsx   # Owns dockview + InPortals
│
├── panels/
│   ├── DebugPanel.tsx      # Placeholder for now
│   ├── EntrypointPanel.tsx # Shows top-level entity links
│   └── DummyEntityPanel.tsx # Proof-of-concept OutPortal test
│
└── App.tsx
```

---

## Data Layer

### Entity Types (schema.ts)

```typescript
type EntityId = string;

type EntityType =
  | 'transcript'
  | 'turn'
  | 'judgement'
  | 'judgement_set'
  | 'collection'
  | 'misconfigured';

interface BaseEntity {
  id: EntityId;
  type: EntityType;
}

interface Transcript extends BaseEntity {
  type: 'transcript';
  topic: string;
  turns: EntityId[];  // owned
}

interface Turn extends BaseEntity {
  type: 'turn';
  role: 'user' | 'assistant';
  message: string;
  previousTurn: EntityId | null;  // ref
}

interface Judgement extends BaseEntity {
  type: 'judgement';
  rubric: EntityId;     // ref
  turn: EntityId;       // ref
  explanation: string;
  score: number;
}

interface JudgementSet extends BaseEntity {
  type: 'judgement_set';
  judgements: EntityId[];  // owned
}

interface Collection extends BaseEntity {
  type: 'collection';
  items: Record<string, EntityId>;  // owned
}

// NOTE: This type is intentionally misconfigured for testing edge cases.
// The renderer will be set up to treat ownedTurns as refs and
// refJudgements as owned, to test the mismatch detection.
interface MisconfiguredObject extends BaseEntity {
  type: 'misconfigured';
  refJudgements: EntityId[];  // actually refs, but renderer will think owned
  ownedTurns: EntityId[];     // actually owned, but renderer will think refs
}

type Entity = Transcript | Turn | Judgement | JudgementSet | Collection | MisconfiguredObject;
```

### Ownership Declaration

Each entity type declares which fields are "owned" vs "refs":

```typescript
const ownershipDeclarations: Record<EntityType, { owned: string[]; refs: string[] }> = {
  transcript: { owned: ['turns'], refs: [] },
  turn: { owned: [], refs: ['previousTurn'] },
  judgement: { owned: [], refs: ['rubric', 'turn'] },
  judgement_set: { owned: ['judgements'], refs: [] },
  collection: { owned: ['items'], refs: [] },
  misconfigured: { owned: ['ownedTurns'], refs: ['refJudgements'] },
};
```

### Ownership Graph (ownership.ts)

```typescript
interface OwnershipInfo {
  owners: Map<EntityId, EntityId[]>;      // who owns each entity
  referencedBy: Map<EntityId, EntityId[]>; // who refs each entity (without owning)
  owns: Map<EntityId, EntityId[]>;         // what each entity owns
  refs: Map<EntityId, EntityId[]>;         // what each entity refs
}

function computeOwnership(
  entities: Map<EntityId, Entity>,
  declarations: typeof ownershipDeclarations
): OwnershipInfo { ... }
```

### Data Bundle (passed as prop)

```typescript
interface DataBundle {
  entities: Map<EntityId, Entity>;
  ownership: OwnershipInfo;
}

// Helper functions (in ownership.ts)
function getData(bundle: DataBundle, id: EntityId): Entity | undefined;
function getOwners(bundle: DataBundle, id: EntityId): EntityId[];
function getReferencedBy(bundle: DataBundle, id: EntityId): EntityId[];
function getTopLevelEntities(bundle: DataBundle): EntityId[];  // entities with no owners
```

### Example Data Structure

```
collection_simple
├── transcript_1
│   ├── turn_1a (→ null)
│   ├── turn_1b (→ turn_1a)
│   └── turn_1c (→ turn_1b)
├── transcript_2
│   ├── turn_2a (→ null)
│   └── turn_2b (→ turn_2a)
├── judgement_set_1
│   ├── judgement_1a (→ rubric_1, → turn_1b)
│   └── judgement_1b (→ rubric_1, → turn_1c)
└── rubric_1

collection_overlap_a
├── transcript_shared (ALSO in collection_overlap_b)  ← multiple owners
│   ├── turn_sa (→ null)
│   └── turn_sb (→ turn_sa)
├── judgement_set_overlap_a
│   ├── judgement_shared (ALSO in judgement_set_overlap_b)  ← multiple owners
│   └── judgement_oa (→ rubric_shared, → turn_sa)
├── rubric_shared (ALSO in collection_overlap_b)
└── misconfigured_1

collection_overlap_b
├── transcript_shared (ALSO in collection_overlap_a)
├── judgement_set_overlap_b
│   ├── judgement_shared (ALSO in judgement_set_overlap_a)
│   └── judgement_ob (→ rubric_shared, → turn_sb)
└── rubric_shared (ALSO in collection_overlap_a)
```

---

## Panels

Panel components receive data via dockview params (set by RenderManager).

### DebugPanel

For now, just a placeholder:

```typescript
function DebugPanel() {
  // TODO: In entity_rendering phase, this will read from RenderManager context
  // and display: active views, view locations, inline children, etc.

  return (
    <div style={{ padding: 16 }}>
      <h3>Debug Panel</h3>
      <p style={{ color: '#888' }}>
        Render manager state will appear here once entity rendering is implemented.
      </p>
    </div>
  );
}
```

### EntrypointPanel

Shows links to top-level entities (collections):

```typescript
interface EntrypointPanelParams {
  data: DataBundle;
}

function EntrypointPanel({ params }: IDockviewPanelProps<EntrypointPanelParams>) {
  const { data } = params;
  const topLevel = getTopLevelEntities(data);

  // TODO: In entity_rendering phase, clicking these will open entity panels

  return (
    <div style={{ padding: 16 }}>
      <h3>Entrypoint</h3>
      <p>Top-level entities:</p>
      <ul>
        {topLevel.map(id => {
          const entity = getData(data, id);
          return (
            <li key={id}>
              <code>{id}</code> ({entity?.type})
              {/* TODO: make clickable to open entity panel */}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
```

### DummyEntityPanel (proof-of-concept)

Tests that InPortal/OutPortal works correctly within dockview:

```typescript
interface DummyEntityPanelParams {
  portalNode: HtmlPortalNode;
}

function DummyEntityPanel({ params }: IDockviewPanelProps<DummyEntityPanelParams>) {
  const { portalNode } = params;

  return (
    <div style={{ padding: 16 }}>
      <h3>Dummy Entity Panel</h3>
      <p style={{ color: '#888', marginBottom: 8 }}>
        Content below comes from an InPortal:
      </p>
      <OutPortal node={portalNode} />
    </div>
  );
}
```

---

## RenderManager

Owns dockview directly and manages InPortals. For this phase, includes one
proof-of-concept portal to verify the pattern works.

```typescript
interface RenderManagerProps {
  data: DataBundle;
}

function RenderManager({ data }: RenderManagerProps) {
  const dockviewApiRef = useRef<DockviewApi | null>(null);

  // Proof-of-concept: one InPortal with dummy content
  const [dummyPortalNode] = useState(() => createHtmlPortalNode());

  const onReady = useCallback((event: DockviewReadyEvent) => {
    dockviewApiRef.current = event.api;

    // Create initial panels
    event.api.addPanel({
      id: 'debug',
      component: 'debug',
      title: 'Debug'
    });
    event.api.addPanel({
      id: 'entrypoint',
      component: 'entrypoint',
      title: 'Entrypoint',
      params: { data },
      position: { referencePanel: 'debug', direction: 'right' }
    });
    event.api.addPanel({
      id: 'dummy-entity',
      component: 'dummy-entity',
      title: 'Dummy Entity',
      params: { portalNode: dummyPortalNode },
      position: { referencePanel: 'entrypoint', direction: 'below' }
    });
  }, [data, dummyPortalNode]);

  // TODO: In entity_rendering phase, this will:
  // - Maintain view state (Map<ViewId, ViewInstance>)
  // - Provide RenderContext for child/ref rendering
  // - Dynamically create/destroy InPortals for active views
  // - Handle openEntity, focusView, etc.

  return (
    <>
      {/* Hidden container for InPortals */}
      <div style={{ display: 'none' }}>
        <InPortal node={dummyPortalNode}>
          <div style={{ padding: 8, background: '#4a4a4a', borderRadius: 4 }}>
            Hello World (from InPortal)
          </div>
        </InPortal>
      </div>

      {/* Dockview */}
      <DockviewReact
        className="dockview-theme-abyss"
        onReady={onReady}
        components={{
          'debug': DebugPanel,
          'entrypoint': EntrypointPanel,
          'dummy-entity': DummyEntityPanel,
        }}
      />
    </>
  );
}
```

---

## Implementation Checklist

- [ ] Create `ts/src/` directory structure
- [ ] `data/schema.ts` - all entity types + ownership declarations
- [ ] `data/example-data.ts` - full test dataset as described above
- [ ] `data/ownership.ts` - compute ownership graph + helper functions
- [ ] `panels/DebugPanel.tsx` - placeholder panel
- [ ] `panels/EntrypointPanel.tsx` - shows top-level entity links
- [ ] `panels/DummyEntityPanel.tsx` - proof-of-concept OutPortal
- [ ] `render-manager/RenderManager.tsx` - owns dockview + InPortals
- [ ] `App.tsx` - wire everything together
- [ ] Update `main.tsx` to use new App
- [ ] Verify "Hello World" appears in DummyEntityPanel via portal
