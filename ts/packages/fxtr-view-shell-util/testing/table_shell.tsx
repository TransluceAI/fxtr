// A complete shell over in-memory tables, for tests and static fixtures: entities by cid and,
// when given, invocation records by `invocationRefKey`, served synchronously (an id or ref not in
// its table loads as an error; a shell without an invocation table serves none). Links are
// `PassthroughEntityLinkImpl`, which has no behavior; `useEntityLinks` is the `links` given, if any.

import { useMemo, type ReactNode } from "react";
import {
  EntityInlineLink,
  ViewShellProvider,
  invocationRefKey,
  type EntityData,
  type EntityLinkImplProps,
  type EntityLinks,
  type InvocationRecord,
  type LoadResult,
  type ViewShell,
} from "fxtr-view";
import type { DispatchRegistry } from "../src/registry";
import { RegistryProvider, RenderEntity } from "../src/render";

/** An `EntityLinkImpl` with no behavior: the entity's inline link view in a plain `<span>`. */
export function PassthroughEntityLinkImpl({ id }: EntityLinkImplProps) {
  return (
    <span className="fxtr-view-link">
      <RenderEntity slot={EntityInlineLink} id={id} />
    </span>
  );
}

export interface TableShellProps {
  readonly registry: DispatchRegistry;
  /** cid -> data. */
  readonly entities?: ReadonlyMap<string, EntityData>;
  /** invocationRefKey -> record. */
  readonly invocations?: ReadonlyMap<string, InvocationRecord>;
  /** What `useEntityLinks` returns: none for a shell that opens nothing. */
  readonly links?: EntityLinks;
  readonly children?: ReactNode;
}

export function TableShellProvider({ registry, entities, invocations, links, children }: TableShellProps) {
  const shell = useMemo<ViewShell>(
    () => ({
      useEntity: (id): LoadResult<EntityData> => {
        const value = entities?.get(id.cid);
        return value ? { status: "loaded", value } : { status: "error", error: new Error(`unknown ${id.cid}`) };
      },
      ...(invocations === undefined
        ? {}
        : {
            useInvocation: (ref): LoadResult<InvocationRecord> => {
              const value = invocations.get(invocationRefKey(ref));
              return value
                ? { status: "loaded", value }
                : { status: "error", error: new Error(`unknown invocation ${invocationRefKey(ref)}`) };
            },
          }),
      EntityLinkImpl: PassthroughEntityLinkImpl,
      ...(links === undefined ? {} : { useEntityLinks: () => links }),
    }),
    [entities, invocations, links],
  );
  return (
    <RegistryProvider registry={registry}>
      <ViewShellProvider shell={shell}>{children}</ViewShellProvider>
    </RegistryProvider>
  );
}
