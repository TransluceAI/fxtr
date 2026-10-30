# Minimal guest package for the eryx-import smoke test. `run` is a no-op
# `fxtr.service` entry point so `mkWasmService` can read an `entryPoint`
# off this workspace's pyproject — the smoke test only builds the
# `.sitePackages` + `import fxtr2`, it never actually invokes `run`.
def run(_config):  # pragma: no cover - never invoked by the smoke test
    raise NotImplementedError("sandbox_smoke is a build-only smoke fixture")
