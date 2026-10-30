// Deno driver for the pyodide sandbox backend — HOST-OWNED AND TRUSTED.
//
// Everything that runs after `loadPyodide` (the pyodide JS glue, the wasm,
// the bootstrap, guest python) is flake-supplied and untrusted; this file is
// the last trusted code to execute, so permission narrowing happens here.
//
// Usage (constructed by fxtr_sandboxing's pyodide runner, not by hand):
//   deno run <permission flags> --config <deno.json> main.ts \
//     <task_socket> <entry_point> <site_packages_dir> <bundle_dir> \
//     [transport_socket]
//
// Sequence:
//   1. Dial the host's per-spawn task UDS (fail fast if missing — the host
//      races accept against our exit).
//   2. Grant-then-drop: when no transport socket is configured, revoke the
//      net permission entirely — no flake-supplied code can ever open a
//      connection, even if a permission-check bug existed. (With a transport
//      socket the net grant stays, scoped by the host's CLI flags to the two
//      socket paths.)
//   3. Import pyodide.mjs from the runtime bundle BY FILE PATH (no npm
//      resolution, no network), boot it, mount the site-packages dir
//      read-only, register the byte-level `fxtr_deno_bridge` JS module.
//   4. Run the bundle's guest_bootstrap.py via runPythonAsync (top-level
//      await — pyodide has no asyncio.run without JSPI).
//   5. EOF backstop: once the main conn drops, the guest is expected to
//      unwind promptly; a grace timer hard-exits if python never returns.
//      (An unref'd timer — it can't fire while wasm blocks the event loop,
//      so a synchronously-spinning guest is the host kill ladder's job.)

const [taskSocket, entryPoint, sitePackagesDir, bundleDir, transportSocket] =
  Deno.args;
if (!taskSocket || !entryPoint || !sitePackagesDir || !bundleDir) {
  console.error(
    "usage: main.ts <task_socket> <entry_point module:function> " +
      "<site_packages_dir> <bundle_dir> [transport_socket]",
  );
  Deno.exit(2);
}

// Grace between main-conn EOF and hard exit, if the guest doesn't unwind.
const EOF_EXIT_GRACE_MS = 10_000;

// Byte-level connection handle exposed to Python. EOF is signaled as an
// empty Uint8Array (pyodide maps JS null to a JsNull proxy, not None; a
// zero-byte read can't otherwise occur). conn.read never yields 0 bytes.
interface BridgeHandle {
  recv(maxBytes: number): Promise<Uint8Array>;
  send(data: Uint8Array): Promise<void>;
  send_eof(): Promise<void>;
  close(): void;
}

function makeHandle(
  conn: Deno.UnixConn,
  onEof?: () => void,
): BridgeHandle {
  let closed = false;
  return {
    recv: async (maxBytes: number): Promise<Uint8Array> => {
      const buf = new Uint8Array(Math.min(maxBytes, 262144));
      let n: number | null = null;
      try {
        n = await conn.read(buf);
      } catch (_e) {
        n = null; // treat a torn connection as EOF; python sees EndOfStream
      }
      if (n === null) {
        onEof?.();
        return new Uint8Array(0);
      }
      return buf.subarray(0, n);
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

// 1. Dial the host before booting pyodide.
const mainConn = await Deno.connect({ transport: "unix", path: taskSocket });

// EOF backstop: once the host side of the main conn drops, the guest's RPC
// loop should see EndOfStream and unwind main() promptly. If it doesn't
// (async-idle guest ignoring EOF), exit after a grace period. The timer is
// unref'd so it never keeps a cleanly-exiting process alive.
let sawMainEof = false;
const armEofBackstop = () => {
  if (sawMainEof) return;
  sawMainEof = true;
  const timer = setTimeout(() => {
    console.error(
      `[fxtr-pyodide] guest did not exit within ${EOF_EXIT_GRACE_MS}ms of main-conn EOF; exiting`,
    );
    Deno.exit(3);
  }, EOF_EXIT_GRACE_MS);
  Deno.unrefTimer(timer);
};
const mainHandle = makeHandle(mainConn, armEofBackstop);

// 2. Grant-then-drop, before any flake-supplied code runs.
if (!transportSocket) {
  await Deno.permissions.revoke({ name: "net" });
}

// 3. Boot pyodide from the bundle (file-path import; no npm, no network).
const { loadPyodide } = await import(
  new URL(`file://${bundleDir}/pyodide/pyodide.mjs`).href
);
const py = await loadPyodide({ indexURL: `${bundleDir}/pyodide` });
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
    if (!transportSocket) {
      throw new Error("no transport socket configured for this sandbox");
    }
    return makeHandle(
      await Deno.connect({ transport: "unix", path: transportSocket }),
    );
  },
});

py.globals.set("_ENTRY_POINT", entryPoint);

// 4. Run the bundle's bootstrap (flake-supplied, untrusted — same trust
// domain as the guest python it starts).
const bootstrap = await Deno.readTextFile(`${bundleDir}/guest_bootstrap.py`);

console.error(
  `[fxtr-pyodide] pyodide ready in ${(t1 - t0).toFixed(0)}ms; starting guest`,
);

try {
  await py.runPythonAsync(bootstrap);
} finally {
  mainHandle.close();
}
console.error(
  `[fxtr-pyodide] guest exited cleanly after ${(performance.now() - t0).toFixed(0)}ms`,
);
