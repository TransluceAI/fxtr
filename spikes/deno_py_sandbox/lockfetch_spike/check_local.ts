// Verifies the local-package arm: localpkg (built via the pyproject-nix
// pythonSet, hatch force-include honored) imports alongside the
// emscripten-picked pydantic-core binary wheel.
import { loadPyodide } from "npm:pyodide@314.0.2";
const [sitePackages] = Deno.args;
const py = await loadPyodide();
py.FS.mkdirTree("/lockfetch");
py.FS.mount(py.FS.filesystems.NODEFS, { root: sitePackages }, "/lockfetch");
const result = await py.runPythonAsync(`
import sys
sys.path.insert(0, "/lockfetch")
import localpkg, pydantic_core
v = pydantic_core.SchemaValidator({"type": "int"})
f"{localpkg.probe()} | pydantic_core: {v.validate_python('41') + 1}"
`);
console.log(result);
