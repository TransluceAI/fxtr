// Deno driver for the pyodide sandbox spike.
//
// Usage:
//   deno run <permission flags> main.ts \
//     <socket_path> <entry_point> <site_packages_dir> [transport_socket_path]
//
// Boots pyodide, mounts <site_packages_dir> read-only(ish) into the
// emscripten FS at /guest-site-packages, dials the host's per-spawn UDS at
// <socket_path>, exposes the connection to Python as a tiny byte-level JS
// module (`fxtr_deno_bridge`), and runs guest_bootstrap.py — which builds a
// SandboxServiceContext over the bridge and hands off to
// fxtr_sandbox_rpc_adapter.main(ctx, entry_point=<entry_point>).
//
// When <transport_socket_path> is given, the bridge also exposes
// `connect_transport()`, dialing one fresh connection to the host's
// multiplexed transport UDS per call (the pyodide analog of the container
// runtime's /run/fxtr/transport.sock). Token routing-header framing lives on
// the Python side (fxtr_sandbox_types.transport_routing).
//
// Intended permission profile (enforced by the host launcher, not here):
//   --no-prompt
//   --allow-read=<deno cache>,<this dir>,<site_packages_dir>,<socket paths>
//   --allow-write=<socket paths>
//   --allow-net=unix:<socket_path>[,unix:<transport_socket_path>]

import { loadPyodide } from "pyodide";

const [socketPath, entryPoint, sitePackagesDir, transportSocketPath] =
  Deno.args;
if (!socketPath || !entryPoint || !sitePackagesDir) {
  console.error(
    "usage: main.ts <socket_path> <entry_point module:function> " +
      "<site_packages_dir> [transport_socket_path]",
  );
  Deno.exit(2);
}

// Byte-level connection handle exposed to Python. EOF is signaled as an
// empty Uint8Array: pyodide 314 converts JS null to a JsNull proxy (not
// Python None), so a null sentinel is awkward to test for on the Python
// side. conn.read never yields 0 bytes except at EOF.
interface BridgeHandle {
  recv(maxBytes: number): Promise<Uint8Array>;
  send(data: Uint8Array): Promise<void>;
  send_eof(): Promise<void>;
  close(): void;
}

function makeHandle(conn: Deno.UnixConn): BridgeHandle {
  let closed = false;
  return {
    recv: async (maxBytes: number): Promise<Uint8Array> => {
      const buf = new Uint8Array(Math.min(maxBytes, 262144));
      const n = await conn.read(buf);
      return n === null ? new Uint8Array(0) : buf.subarray(0, n);
    },
    send: async (data: Uint8Array): Promise<void> => {
      let off = 0;
      while (off < data.length) {
        off += await conn.write(data.subarray(off));
      }
    },
    send_eof: async (): Promise<void> => {
      await conn.closeWrite();
    },
    close: (): void => {
      if (!closed) {
        closed = true;
        try {
          conn.close();
        } catch (_e) {
          // already closed by the runtime
        }
      }
    },
  };
}

const t0 = performance.now();

// Connect to the host before booting pyodide, so a missing/broken socket
// fails fast (and the host's accept-or-exit race sees our exit).
const mainHandle = makeHandle(
  await Deno.connect({ transport: "unix", path: socketPath }),
);

const py = await loadPyodide();
const t1 = performance.now();

py.FS.mkdirTree("/guest-site-packages");
py.FS.mount(
  py.FS.filesystems.NODEFS,
  { root: sitePackagesDir },
  "/guest-site-packages",
);

py.registerJsModule("fxtr_deno_bridge", {
  main: mainHandle,
  connect_transport: async (): Promise<BridgeHandle> => {
    if (!transportSocketPath) {
      throw new Error("no transport socket configured for this sandbox");
    }
    return makeHandle(
      await Deno.connect({ transport: "unix", path: transportSocketPath }),
    );
  },
});

py.globals.set("_ENTRY_POINT", entryPoint);

const bootstrap = await Deno.readTextFile(
  new URL("./guest_bootstrap.py", import.meta.url),
);

console.error(
  `[deno-py] pyodide ready in ${(t1 - t0).toFixed(0)}ms; running guest bootstrap`,
);

try {
  await py.runPythonAsync(bootstrap);
} finally {
  mainHandle.close();
}
console.error(
  `[deno-py] guest main returned after ${(performance.now() - t0).toFixed(0)}ms; exiting`,
);
