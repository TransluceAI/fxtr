"""Boot smoke for the dialect fixture backend (the Python half of the
dashboard's round-trip harness — see `dialect_fixture_backend.py`).

The real cross-language assertions live in the spawned vitest
(`projects/fxtr_experiment/frontend/tests/dialect.test.ts`); this smoke
just proves the backend boots and serves its shapes from the Python
suite too, so a Python-side break fails here and not only through the
spawned process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_DEMO_DIR = Path(__file__).resolve().parents[1]


def _post(base: str, method: str, args: list[object] | None = None) -> object:
    req = urllib.request.Request(
        f"{base}/svc/dialect",
        data=json.dumps({"method": method, "args": args or [], "kwargs": {}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def test_fixture_backend_boots_and_serves() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(_DEMO_DIR / "dialect_fixture_backend.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=_DEMO_DIR,
    )
    try:
        base: str | None = None
        deadline = time.time() + 25
        assert proc.stdout is not None
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line.startswith("READY "):
                base = line.split()[1].strip()
                break
        assert base is not None, proc.stderr.read() if proc.stderr else "no READY"

        # Discovery lists the fixture service.
        with urllib.request.urlopen(f"{base}/discovery", timeout=10) as resp:
            discovery = json.loads(resp.read())
        assert "dialect-fixture" in json.dumps(discovery)

        # One representative decode + the echo direction (wire-form in,
        # wire-form out — the escaped JSON as the TS side sends it).
        summary = _post(base, "get_workflow_summary")
        assert '"kind": "task"' in json.dumps(summary)
        # The echo direction decodes a wire-form selection through the
        # real dialect codec (deep-equality semantics are the vitest's
        # job — a malformed encoding would 400 here).
        sel = {"token": "tok", "bound": [[]], "output": None}
        echoed = _post(base, "echo_sel", [sel])
        assert "tok" in json.dumps(echoed)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
