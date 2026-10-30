// Closes the lockfetch loop: mount the nix-built site-packages (the flake's
// `default` output — pass its store path as argv[0]) into pyodide under deno
// and prove the pyemscripten binary wheel imports and runs.
//
//   nix build "path:$PWD#default"
//   deno run --allow-read=<deno cache>,$(realpath result) check_lockfetch.ts $(realpath result)

import { loadPyodide } from "npm:pyodide@314.0.2";

const [sitePackages] = Deno.args;
if (!sitePackages) {
  console.error("usage: check_lockfetch.ts <site_packages_store_path>");
  Deno.exit(2);
}

const py = await loadPyodide();
py.FS.mkdirTree("/lockfetch");
py.FS.mount(py.FS.filesystems.NODEFS, { root: sitePackages }, "/lockfetch");
const result = await py.runPythonAsync(`
import sys
sys.path.insert(0, "/lockfetch")
import pydantic_core
v = pydantic_core.SchemaValidator({"type": "int"})
f"pydantic_core {pydantic_core.__version__} (binary wheel from nix store): {v.validate_python('41') + 1}"
`);
console.log(result);
