hey Claude!  I'm working on thingwatcher and I want to refactor some stuff to prepare for the future. can you read the bullet "redesign custom renderer API" in @plans/phase2_overview.md and create a plan doc plans/phase2_step10p2_dynamic_renderer.md for how to do it? please ask me any questions about what I want, and familiarize yourself with the codebase using the overview docs. the high level idea is that I want to refactor the props and bindings for the dynamic renderers, to make them more like the API used by a separate prototype /Users/daniel/fxtr-worktrees/fxtr/v2/fxtr2/panel_spike/ts/src/renderers/by_entity_type/judgement-set.tsx /Users/daniel/fxtr-worktrees/fxtr/v2/fxtr2/panel_spike/ts/src/renderers/by_entity_type/judgement.tsx . key parts to port over: the dynamic renderers should export two separate renderers FullRenderer and RefRenderer , and those should be plumbed through dynamic rendering system. also the existing ThingLink should be renamed NodeRef and it should work like the other prototypes RefLink. and the existing ThingView should be unified with the other prototype's Child and be called ChildNodeView. you can read more of /Users/daniel/fxtr-worktrees/fxtr/v2/fxtr2/panel_spike to get more context. please ask me questions to clarify what the desired state is. this is prep work for eventually switching all of thingwatcher to use a similar rendering system as the panel spike, and I basically want it to feel like each type of renderer is a self-contained component that just has to be responsible for rendering its own data in a few contexts. also the inline renderer should be used whenever rendering a thinglink in the current system, and also used when rendering things in the ThingsList or roots view.

I also think this is a good opportunity to spend some time refining the props:

currently in thingwatcher:
    props are
        payload: unknown;
        dataChunks: unknown[];
        links: LinkInfo[];
        triggerable: boolean;
        trigger: (payload: unknown | null) => Promise<TriggerResult>;
        args: Record<string, unknown>;
    and the child components are
        interface ThingLinkProps {
            /** Thing key as base64 string */
            thingKey?: string;
            /** Thing key as JSON value (from keyJSON field) */
            keyJSON?: unknown;
            /** Custom label (default: "#shortId (thingType)") */
            label?: string;
        }
        interface ThingViewProps {
            /** Thing key as base64 string */
            thingKey?: string;
            /** Thing key as JSON value (from keyJSON field) */
            keyJSON?: unknown;
            /** Args to pass to the child renderer */
            args?: Record<string, unknown>;
        }

whereas for panel spike we have /Users/daniel/fxtr-worktrees/fxtr/v2/fxtr2/panel_spike/ts/src/renderers/types.ts
and
    export interface RefLinkProps {
        entityId: EntityId;
        /** True if rendering an owned child, false if an unowned reference */
        isOwned?: boolean;
        /** Direction to open panel relative to source. Defaults to 'right' (forward navigation). */
        direction?: OpenDirection;
    }
    export interface DraggableIDProps {
        entityId: EntityId;
    }
    export interface ChildProps {
        entityId: EntityId;
        /** Optional custom arguments forwarded to the entity renderer */
        customArgs?: Record<string, unknown>;
    }

and I think the most sensible thing to do would be for DraggableID to actually not take an entity key as a prop, and instead be passed in as a prop to the FullRenderer and bound to the ID of the thing being rendered. the NodeRef can be like
    export interface NodeRefProps {
        /** Thing key as base64 string */
        thingKey?: string;
        /** Thing key as JSON value (from keyJSON field) */
        keyJSON?: unknown;
        /** True if rendering an owned child, false if an unowned reference */
        isOwned?: boolean;
        /** Direction to open panel relative to source. Defaults to 'right' (forward navigation). */
        direction?: OpenDirection;
    }
and the ChildNodeView can be
    export interface ChildNodeViewProps {
        /** Thing key as base64 string */
        thingKey?: string;
        /** Thing key as JSON value (from keyJSON field) */
        keyJSON?: unknown;
        /** Args to pass to the child renderer */
        args?: Record<string, unknown>;
    }
and we can receive something like

/**
 * Props for full (card) renderers.
 * Full renderers display the complete entity content in a card format.
 */
export interface FullRendererProps {
    payload: unknown;
    dataChunks: unknown[];
    links: LinkInfo[];
    triggerable: boolean;
    trigger: (payload: unknown | null) => Promise<TriggerResult>;
    args: Record<string, unknown>;
    isTopLevel: boolean;
    DraggableIDMarker: (a component wrapper that takes a component as a child, and designates it as the draggable ID for this renderer);
}

/**
 * Props for block ref renderers.
 * Block ref renderers display a one-line block reference that expands to fill space.
 * Shows full ID, type info, and content preview.
 * Owned refs get a colored border, non-owned refs get underline styling.
 */
export interface RefRendererProps {
    payload: unknown;
    dataChunks: unknown[];
    links: LinkInfo[];
    triggerable: boolean;
    isOwned: boolean;
    DraggableIDMarker: (a component wrapper that takes a component as a child, and designates it as the draggable ID for this renderer);
}
