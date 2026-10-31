// A shell for this package's tests whose every entity and invocation load is pending, beside the
// registry as given.

import { ViewShellProvider, type ViewShell } from "fxtr-view";
import type { ReactNode } from "react";
import { RegistryProvider, type DispatchRegistry } from "../src/index";
import { PassthroughEntityLinkImpl } from "../testing";

const pending = () => ({ status: "loading" as const, promise: new Promise(() => {}) });

const PENDING_SHELL: ViewShell = {
  useEntity: pending,
  useInvocation: pending,
  EntityLinkImpl: PassthroughEntityLinkImpl,
};

export function PendingShellProvider({ registry, children }: { registry: DispatchRegistry; children?: ReactNode }) {
  return (
    <RegistryProvider registry={registry}>
      <ViewShellProvider shell={PENDING_SHELL}>{children}</ViewShellProvider>
    </RegistryProvider>
  );
}
