"""Stage-by-stage timing harness for eryx sandbox creation.

Goal: make the cost of each step in the spawn pipeline visible so we
know what the optimization targets are. This deliberately stays close
to the eryx API (no fxtr2 RPC stack) — once we know where the time goes
at this layer we can decide where to add the RPC bridge back in.

Scenarios so far:

  bare         eryx.SandboxFactory()                   — empty factory
  with_wheels  eryx.SandboxFactory(packages=[...wheels])
  warm_spawn   1 factory build + N create_sandbox      — the dev-loop case
  save_load    factory.save → fresh process load → spawn
                                                       (run with --child)

Run from this directory:

    uv run python fetch_test_wheels.py        # one-time
    uv run python bench_harness.py            # all scenarios
    uv run python bench_harness.py warm_spawn # just one
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable, TypeVar

import eryx

# Reuse the canary-file repair from the production runner — same TMPDIR,
# same failure mode (empty/stale eryx-embedded/python-stdlib).
from fxtr_sandboxing.runner import _check_and_maybe_repair_eryx_embedded_dir

T = TypeVar("T")

HERE = Path(__file__).parent
WHEELS_DIR = HERE / "wheels_cache"
WHEELS_EXTRACTED_DIR = HERE / ".cache" / "wheels_extracted"
LOCAL_PKGS_DIR = HERE / "local_pkgs"
SAVED_FACTORY = HERE / ".cache" / "factory.bin"


def _wheels() -> list[Path]:
    if not WHEELS_DIR.is_dir():
        raise SystemExit(f"{WHEELS_DIR} does not exist — run `python fetch_test_wheels.py` first")
    paths = sorted(WHEELS_DIR.glob("*.whl"))
    if not paths:
        raise SystemExit(f"no wheels in {WHEELS_DIR} — run `python fetch_test_wheels.py`")
    return paths


def _ensure_wheels_extracted() -> Path:
    """Unzip every wheel in `wheels_cache/` into a single site-packages dir.

    One-time work, lives in `.cache/wheels_extracted/`. The point of the
    `bare_sandbox_with_volumes` scenario is to push the extraction work
    OUT of the per-spawn path; we treat the extracted dir as part of
    the warm-cache state, like the wheels themselves.
    """
    WHEELS_EXTRACTED_DIR.parent.mkdir(parents=True, exist_ok=True)
    if WHEELS_EXTRACTED_DIR.exists():
        return WHEELS_EXTRACTED_DIR
    tmp = WHEELS_EXTRACTED_DIR.with_suffix(".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    for whl in _wheels():
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(tmp)
    tmp.rename(WHEELS_EXTRACTED_DIR)
    return WHEELS_EXTRACTED_DIR


def _t(label: str, fn: Callable[[], T]) -> tuple[float, T]:
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    print(f"  {label:<40s} {dt * 1000:>8.1f} ms")
    return dt, out


def _summary(label: str, samples: list[float]) -> None:
    if not samples:
        return
    ms = [s * 1000 for s in samples]
    print(
        f"  {label:<40s} "
        f"min={min(ms):>7.1f} ms  median={statistics.median(ms):>7.1f} ms  "
        f"p95={sorted(ms)[max(0, int(len(ms) * 0.95) - 1)]:>7.1f} ms  "
        f"max={max(ms):>7.1f} ms  (n={len(ms)})"
    )


# ---------- scenarios ----------


def scenario_bare() -> None:
    print("\n[bare] empty SandboxFactory()")
    _t("eryx.SandboxFactory()", lambda: eryx.SandboxFactory())


def scenario_with_wheels() -> None:
    print("\n[with_wheels] SandboxFactory(packages=[...])")
    wheels = _wheels()
    print(f"  wheels: {[p.name for p in wheels]}")
    _t(
        "eryx.SandboxFactory(packages=[...], imports=[])",
        lambda: eryx.SandboxFactory(packages=wheels, imports=[]),
    )

    # Pre-importing is supposed to bake imports into the snapshot — we
    # want to know whether that's much more expensive than not.
    _t(
        "eryx.SandboxFactory(packages=[...], imports=[<all>])",
        lambda: eryx.SandboxFactory(
            packages=wheels,
            imports=["six", "attrs", "click"],
        ),
    )


def scenario_warm_spawn(n: int = 5) -> None:
    print(f"\n[warm_spawn] N={n} create_sandbox calls off one factory")
    wheels = _wheels()
    _, factory = _t(
        "build factory once",
        lambda: eryx.SandboxFactory(packages=wheels, imports=["six", "attrs", "click"]),
    )

    spawn_times: list[float] = []
    execute_times: list[float] = []
    for i in range(n):
        dt_spawn, sbx = _t(f"  create_sandbox #{i}", factory.create_sandbox)
        spawn_times.append(dt_spawn)
        dt_exec, _ = _t(
            f"  execute(import six,attrs,click) #{i}",
            lambda s=sbx: s.execute("import six, attrs, click; print('ok')"),
        )
        execute_times.append(dt_exec)

    print()
    _summary("create_sandbox()", spawn_times)
    _summary("execute(import three)", execute_times)


def scenario_warm_spawn_with_volume(n: int = 5) -> None:
    print(f"\n[warm_spawn_with_volume] N={n} spawns + local volume mount")
    wheels = _wheels()
    _, factory = _t(
        "build factory once",
        lambda: eryx.SandboxFactory(packages=wheels, imports=["six", "attrs", "click"]),
    )
    assert LOCAL_PKGS_DIR.is_dir(), LOCAL_PKGS_DIR

    runner_code = (
        "import sys\n"
        "sys.path.insert(0, '/local-pkgs')\n"
        "import local_pkg_b\n"
        "print(local_pkg_b.combined())\n"
    )

    spawn_times: list[float] = []
    execute_times: list[float] = []
    for i in range(n):
        dt_spawn, sbx = _t(
            f"  create_sandbox #{i}",
            lambda: factory.create_sandbox(
                volumes=[(str(LOCAL_PKGS_DIR), "/local-pkgs", True)],
            ),
        )
        spawn_times.append(dt_spawn)
        dt_exec, _ = _t(
            f"  execute(import local_pkg_b) #{i}",
            lambda s=sbx: s.execute(runner_code),
        )
        execute_times.append(dt_exec)

    print()
    _summary("create_sandbox(volumes=...)", spawn_times)
    _summary("execute(local import + call)", execute_times)


def scenario_bare_sandbox_with_volumes(n: int = 5) -> None:
    """Skip SandboxFactory entirely — bare Sandbox + everything as volumes.

    One-time setup (extract wheels, list local pkg dir) is hoisted out
    of the timed loop so the per-spawn numbers reflect what an already-
    warm host process would see. The interesting comparison is against
    `warm_spawn_with_volume`: same per-spawn shape, but no factory
    build cost up-front.
    """
    print(f"\n[bare_sandbox_with_volumes] N={n}")
    extracted = _ensure_wheels_extracted()
    print(f"  wheels extracted at: {extracted}")
    print(f"  local pkgs dir:      {LOCAL_PKGS_DIR}")

    runner_code = (
        "import sys\n"
        "sys.path.insert(0, '/wheels')\n"
        "sys.path.insert(0, '/local-pkgs')\n"
        "import six, attrs, click\n"
        "import local_pkg_b\n"
        "print(local_pkg_b.combined())\n"
    )

    spawn_times: list[float] = []
    execute_times: list[float] = []
    for i in range(n):
        dt_spawn, sbx = _t(
            f"  eryx.Sandbox(volumes=...) #{i}",
            lambda: eryx.Sandbox(
                volumes=[
                    (str(extracted), "/wheels", True),
                    (str(LOCAL_PKGS_DIR), "/local-pkgs", True),
                ],
            ),
        )
        spawn_times.append(dt_spawn)
        dt_exec, _ = _t(
            f"  execute(import wheels+local) #{i}",
            lambda s=sbx: s.execute(runner_code),
        )
        execute_times.append(dt_exec)

    print()
    _summary("eryx.Sandbox(volumes=...)", spawn_times)
    _summary("execute(import 3 wheels + local)", execute_times)


def scenario_empty_factory_with_volumes(n: int = 5) -> None:
    """`SandboxFactory()` (no packages, no imports) + everything via volumes.

    Tests whether the factory's value is "Python pre-initialized into a
    precompiled snapshot" (cheap import side) or "wheels baked in"
    (cheap and resolves to in-memory state). Compare against:

      - warm_spawn_with_volume: factory built with packages + imports
      - bare_sandbox_with_volumes: no factory at all
    """
    print(f"\n[empty_factory_with_volumes] N={n}")
    extracted = _ensure_wheels_extracted()
    _, factory = _t(
        "build EMPTY factory (no packages, no imports)",
        lambda: eryx.SandboxFactory(),
    )

    runner_code = (
        "import sys\n"
        "sys.path.insert(0, '/wheels')\n"
        "sys.path.insert(0, '/local-pkgs')\n"
        "import six, attrs, click\n"
        "import local_pkg_b\n"
        "print(local_pkg_b.combined())\n"
    )

    spawn_times: list[float] = []
    execute_times: list[float] = []
    for i in range(n):
        dt_spawn, sbx = _t(
            f"  create_sandbox(volumes=...) #{i}",
            lambda: factory.create_sandbox(
                volumes=[
                    (str(extracted), "/wheels", True),
                    (str(LOCAL_PKGS_DIR), "/local-pkgs", True),
                ],
            ),
        )
        spawn_times.append(dt_spawn)
        dt_exec, _ = _t(
            f"  execute(import wheels+local) #{i}",
            lambda s=sbx: s.execute(runner_code),
        )
        execute_times.append(dt_exec)

    print()
    _summary("create_sandbox(volumes=...)", spawn_times)
    _summary("execute(import 3 wheels + local)", execute_times)


def scenario_packages_no_preimport(n: int = 5) -> None:
    """`SandboxFactory(packages=[...], imports=[])` — baked wheels, NOT pre-imported.

    Isolates "pre-imported into snapshot" from "pure-Python files
    available on /site-packages". If first-import cost from /site-packages
    is similar to first-import cost from a /wheels volume, then
    pre-importing is what matters, not where the .py files live.
    """
    print(f"\n[packages_no_preimport] N={n}")
    wheels = _wheels()
    _, factory = _t(
        "build factory (packages baked, imports=[])",
        lambda: eryx.SandboxFactory(packages=wheels, imports=[]),
    )

    spawn_times: list[float] = []
    execute_times: list[float] = []
    for i in range(n):
        dt_spawn, sbx = _t(f"  create_sandbox #{i}", factory.create_sandbox)
        spawn_times.append(dt_spawn)
        dt_exec, _ = _t(
            f"  execute(import six,attrs,click) #{i}",
            lambda s=sbx: s.execute("import six, attrs, click; print('ok')"),
        )
        execute_times.append(dt_exec)

    print()
    _summary("create_sandbox()", spawn_times)
    _summary("execute(import three; not pre-imported)", execute_times)


def scenario_late_preload_sandbox() -> None:
    """Two `execute()` calls on the same `Sandbox`: does state persist?

    Eryx docs say: "Each call to `execute()` runs in complete isolation —
    Python state is not preserved between calls" for `Sandbox`. Verify
    empirically by importing wheels in call #1 and re-importing them in
    call #2; if call #2 is ~5 ms the state did persist, if ~250 ms each
    `execute` is fully fresh.
    """
    print("\n[late_preload_sandbox] same Sandbox, preload then real-call")
    extracted = _ensure_wheels_extracted()
    _, factory = _t("build EMPTY factory", lambda: eryx.SandboxFactory())
    _, sbx = _t(
        "create_sandbox(volumes=...)",
        lambda: factory.create_sandbox(
            volumes=[
                (str(extracted), "/wheels", True),
                (str(LOCAL_PKGS_DIR), "/local-pkgs", True),
            ],
        ),
    )

    preload = (
        "import sys\n"
        "sys.path.insert(0, '/wheels')\n"
        "sys.path.insert(0, '/local-pkgs')\n"
        "import six, attrs, click\n"
    )
    real_call = (
        "import sys\n"  # repeat in case sys.path was reset
        "sys.path.insert(0, '/wheels')\n"
        "sys.path.insert(0, '/local-pkgs')\n"
        "import six, attrs, click\n"
        "import local_pkg_b\n"
        "print(local_pkg_b.combined())\n"
    )
    _t("execute(preload)", lambda: sbx.execute(preload))
    _t("execute(real call, post-preload)", lambda: sbx.execute(real_call))
    _t("execute(real call again)", lambda: sbx.execute(real_call))


def scenario_late_preload_session() -> None:
    """Same idea via `eryx.Session`, which docs say *does* preserve state.

    If late-preload works at all, this is the API for it. Three timed
    `execute()` calls on one Session: a preload, then two real calls,
    so we can see whether subsequent calls match the warm-baked import
    cost (~5 ms) or the cold cost (~250 ms).
    """
    print("\n[late_preload_session] same Session, preload then real-calls")
    extracted = _ensure_wheels_extracted()

    _, session = _t(
        "create Session(volumes=...)",
        lambda: eryx.Session(
            volumes=[
                (str(extracted), "/wheels", True),
                (str(LOCAL_PKGS_DIR), "/local-pkgs", True),
            ],
        ),
    )

    preload = (
        "import sys\n"
        "sys.path.insert(0, '/wheels')\n"
        "sys.path.insert(0, '/local-pkgs')\n"
        "import six, attrs, click\n"
    )
    real_call = (
        "import six, attrs, click\n" "import local_pkg_b\n" "print(local_pkg_b.combined())\n"
    )
    _t("execute(preload)", lambda: session.execute(preload))
    _t("execute(real call #1, post-preload)", lambda: session.execute(real_call))
    _t("execute(real call #2)", lambda: session.execute(real_call))


def scenario_late_preload_session_clear_state() -> None:
    """Does `Session.clear_state()` keep imported modules around?

    If `clear_state` only drops user-defined variables but leaves
    `sys.modules` intact, this would be the "preload once, run many
    tasks each in a clean Python state but with imports already
    paid-for" pattern — basically a poor-man's per-task fork.
    """
    print("\n[late_preload_session_clear_state]")
    extracted = _ensure_wheels_extracted()

    _, session = _t(
        "create Session(volumes=...)",
        lambda: eryx.Session(
            volumes=[
                (str(extracted), "/wheels", True),
                (str(LOCAL_PKGS_DIR), "/local-pkgs", True),
            ],
        ),
    )

    preload = (
        "import sys\n"
        "sys.path.insert(0, '/wheels')\n"
        "sys.path.insert(0, '/local-pkgs')\n"
        "import six, attrs, click\n"
        "_PRELOAD_MARK = 'preloaded'\n"
    )
    _t("execute(preload + set var)", lambda: session.execute(preload))
    _t("clear_state()", session.clear_state)
    # User var should be gone; modules might or might not be.
    _t(
        "execute(check _PRELOAD_MARK is gone)",
        lambda: session.execute(
            "try:\n"
            "    print('var-survived' if _PRELOAD_MARK else 'falsy')\n"
            "except NameError:\n"
            "    print('var-cleared')\n"
        ),
    )
    _t(
        "execute(re-import + use)",
        lambda: session.execute(
            "import six, attrs, click\n" "import local_pkg_b\n" "print(local_pkg_b.combined())\n"
        ),
    )


def scenario_session_pick_up_edits() -> None:
    """If we drop local modules from sys.modules, do edits get picked up?

    The Session-with-clear_state pattern only works for stable wheels;
    a long-lived Session won't see edits to a volume-mounted local
    package unless we explicitly drop it from sys.modules between
    spawns and pay the cold-import cost.
    """
    print("\n[session_pick_up_edits] selective sys.modules invalidation")
    extracted = _ensure_wheels_extracted()

    _, session = _t(
        "create Session(volumes=...)",
        lambda: eryx.Session(
            volumes=[
                (str(extracted), "/wheels", True),
                (str(LOCAL_PKGS_DIR), "/local-pkgs", True),
            ],
        ),
    )
    _t(
        "execute(preload wheels + import local_pkg_b)",
        lambda: session.execute(
            "import sys\n"
            "sys.path.insert(0, '/wheels')\n"
            "sys.path.insert(0, '/local-pkgs')\n"
            "import six, attrs, click\n"
            "import local_pkg_b\n"
            "print('first:', local_pkg_b.combined())\n"
        ),
    )
    _t("clear_state()", session.clear_state)
    _t(
        "execute(import local_pkg_b — should be CACHED)",
        lambda: session.execute("import local_pkg_b\nprint('cached:', local_pkg_b.combined())\n"),
    )
    _t(
        "execute(drop local_pkg_b from sys.modules + reimport)",
        lambda: session.execute(
            "import sys\n"
            "for k in [k for k in sys.modules if k.startswith('local_pkg')]:\n"
            "    del sys.modules[k]\n"
            "import local_pkg_b\n"
            "print('reimported:', local_pkg_b.combined())\n"
        ),
    )
    _t(
        "execute(import local_pkg_b yet again — re-cached)",
        lambda: session.execute(
            "import local_pkg_b\nprint('re-cached:', local_pkg_b.combined())\n"
        ),
    )


def scenario_fresh_session_per_spawn(n: int = 5) -> None:
    """Fresh Session per spawn, preload + real-call sequence.

    Treats a 2-execute Session as the unit of isolation. Each "spawn"
    is: new Session → preload imports → run task code → discard. Tests
    whether the late-preload mechanism can stand in for a Sandbox while
    keeping per-spawn isolation hard (each Session is its own WASM
    instance).
    """
    print(f"\n[fresh_session_per_spawn] N={n}")
    extracted = _ensure_wheels_extracted()

    preload_code = (
        "import sys\n"
        "sys.path.insert(0, '/wheels')\n"
        "sys.path.insert(0, '/local-pkgs')\n"
        "import six, attrs, click\n"
    )
    real_code = (
        "import six, attrs, click\n" "import local_pkg_b\n" "print(local_pkg_b.combined())\n"
    )

    session_creates: list[float] = []
    preload_times: list[float] = []
    real_times: list[float] = []
    total_per_spawn: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        dt_create, session = _t(
            f"  Session(volumes=...) #{i}",
            lambda: eryx.Session(
                volumes=[
                    (str(extracted), "/wheels", True),
                    (str(LOCAL_PKGS_DIR), "/local-pkgs", True),
                ],
            ),
        )
        session_creates.append(dt_create)
        dt_preload, _ = _t(
            f"  execute(preload) #{i}",
            lambda s=session: s.execute(preload_code),
        )
        preload_times.append(dt_preload)
        dt_real, _ = _t(
            f"  execute(real call) #{i}",
            lambda s=session: s.execute(real_code),
        )
        real_times.append(dt_real)
        total_per_spawn.append(time.perf_counter() - t0)

    print()
    _summary("Session creation", session_creates)
    _summary("execute(preload)", preload_times)
    _summary("execute(real call)", real_times)
    _summary("TOTAL per spawn", total_per_spawn)


def scenario_session_pool_warmup(n: int = 4) -> None:
    """Warm up N Sessions in parallel, then time the per-spawn checkout.

    The pool pattern: preload happens in the background on a thread
    pool, so the user-visible "spawn" is just `session.execute(real_code)`
    on an already-warm Session pulled out of the pool.

    Measures:
      - sequential warmup (N sessions back-to-back, single-threaded)
      - parallel warmup (same N on a ThreadPoolExecutor with N workers)
      - per-spawn checkout cost (just the real-call execute on a warm
        session; this is the latency the user cares about once a pool
        slot is ready)
    """
    import concurrent.futures

    print(f"\n[session_pool_warmup] N={n}")
    extracted = _ensure_wheels_extracted()

    preload_code = (
        "import sys\n"
        "sys.path.insert(0, '/wheels')\n"
        "sys.path.insert(0, '/local-pkgs')\n"
        "import six, attrs, click\n"
    )
    real_code = (
        "import six, attrs, click\n" "import local_pkg_b\n" "print(local_pkg_b.combined())\n"
    )

    def _build_warm() -> eryx.Session:
        s = eryx.Session(
            volumes=[
                (str(extracted), "/wheels", True),
                (str(LOCAL_PKGS_DIR), "/local-pkgs", True),
            ],
        )
        s.execute(preload_code)
        return s

    # Warm one upfront so any first-touch JIT cost is paid.
    _t("(prime first Session)", _build_warm)

    # Sequential
    t0 = time.perf_counter()
    seq_sessions: list[eryx.Session] = []
    for i in range(n):
        seq_sessions.append(_build_warm())
    seq_total = time.perf_counter() - t0
    print(f"  sequential warmup of {n} Sessions: {seq_total * 1000:.1f} ms")

    # Parallel — eryx's Sandbox.execute releases the GIL via py.detach,
    # so multiple Sessions should overlap their WASM work on a thread
    # pool. Question is by how much.
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        par_sessions = list(pool.map(lambda _i: _build_warm(), range(n)))
    par_total = time.perf_counter() - t0
    print(
        f"  parallel warmup of {n} Sessions:  {par_total * 1000:.1f} ms  "
        f"(speedup {seq_total / par_total:.2f}×)"
    )

    # Per-spawn checkout cost: just run the real code on each pre-warmed
    # session. This is the latency the caller sees once a slot is ready.
    print()
    checkout_times: list[float] = []
    for i, s in enumerate(par_sessions):
        dt, _ = _t(f"  warm session #{i}.execute(real_code)", lambda s=s: s.execute(real_code))
        checkout_times.append(dt)
    print()
    _summary("checkout (real_code on warm session)", checkout_times)


def scenario_save_load() -> None:
    """Two-process timing: parent builds + saves; child loads + spawns."""
    print("\n[save_load] factory.save() / load() round-trip")
    wheels = _wheels()
    SAVED_FACTORY.parent.mkdir(parents=True, exist_ok=True)
    dt_build, factory = _t(
        "build factory once",
        lambda: eryx.SandboxFactory(packages=wheels, imports=["six", "attrs", "click"]),
    )
    _t("factory.save(...)", lambda: factory.save(SAVED_FACTORY))
    print(f"  saved: {SAVED_FACTORY} ({SAVED_FACTORY.stat().st_size:_} bytes)")

    # Re-launch ourselves with --child so the load happens in a *fresh*
    # process — that's the cold-start case we care about.
    import subprocess

    print("  launching child for cold load...")
    res = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child"],
        capture_output=True,
        text=True,
    )
    print(res.stdout)
    if res.returncode != 0:
        print("CHILD STDERR:", res.stderr, file=sys.stderr)
    print(f"  total parent build = {dt_build * 1000:.1f} ms")


def _child() -> None:
    """Cold-load measurement run by `scenario_save_load`."""
    print("[child] cold-load timing")
    if not SAVED_FACTORY.exists():
        raise SystemExit("no saved factory — run scenario_save_load from parent first")
    factory_site_packages = None  # rely on the snapshot for the wheel set

    def _load():
        return eryx.SandboxFactory.load(SAVED_FACTORY, site_packages=factory_site_packages)

    _, factory = _t("SandboxFactory.load(...)", _load)
    _, sbx = _t("create_sandbox", factory.create_sandbox)
    _t(
        "execute(import six,attrs,click)",
        lambda: sbx.execute("import six,attrs,click; print('ok')"),
    )


# ---------- entry ----------

SCENARIOS = {
    "bare": scenario_bare,
    "with_wheels": scenario_with_wheels,
    "warm_spawn": scenario_warm_spawn,
    "warm_spawn_with_volume": scenario_warm_spawn_with_volume,
    "bare_sandbox_with_volumes": scenario_bare_sandbox_with_volumes,
    "empty_factory_with_volumes": scenario_empty_factory_with_volumes,
    "packages_no_preimport": scenario_packages_no_preimport,
    "late_preload_sandbox": scenario_late_preload_sandbox,
    "late_preload_session": scenario_late_preload_session,
    "late_preload_session_clear_state": scenario_late_preload_session_clear_state,
    "session_pick_up_edits": scenario_session_pick_up_edits,
    "fresh_session_per_spawn": scenario_fresh_session_per_spawn,
    "session_pool_warmup": scenario_session_pool_warmup,
    "save_load": scenario_save_load,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", nargs="*", default=list(SCENARIOS.keys()))
    parser.add_argument("--child", action="store_true", help="(internal)")
    parser.add_argument("--json", action="store_true", help="emit raw timings as JSON too")
    args = parser.parse_args()

    if args.child:
        _child()
        return 0

    print(f"python: {sys.executable}")
    print(f"eryx:   {eryx.__version__} ({eryx.__file__})")
    _check_and_maybe_repair_eryx_embedded_dir()
    for name in args.scenarios:
        if name not in SCENARIOS:
            print(f"unknown scenario: {name}; pick from {list(SCENARIOS)}", file=sys.stderr)
            return 2
        SCENARIOS[name]()
    if args.json:
        print(json.dumps({"todo": "structured-output"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
