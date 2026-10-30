# Stage 8 post 1 — container-test runtime sweep + posture softening

Stage 8 (`stage8_podman.md`) standardized on podman and framed other
runtimes as "an escape hatch, not a feature". Two corrections to that,
settled in discussion 2026-06-06:

1. **The container E2E tests become a sweep over all supported
   runtimes** — currently **podman** and **Apple Container**
   (`container`) — instead of running once against whatever
   `get_runtime()` returns. The two backends exercise genuinely
   different implementation surfaces (tarless skopeo-into-API-socket +
   sshd-sidecar socket plumbing vs. tar-fallback delivery + direct
   socket-file mounts), so a single-runtime run leaves one surface
   silently untested. Runtimes not installed are skipped; installed
   ones run.
2. **Posture rewording**: Apple Container is a *supported non-default
   backend*, not a mere escape hatch. podman is the recommended flow;
   the alternative stays tested so it can't rot silently. (If
   maintaining the sweep ever becomes prohibitive, that's the signal
   to deliberately cut the feature — what we don't want is an untested
   feature that's nominally still there.) The "collapse the escape
   hatch post-stage-9" followup gets reframed as conditional, not a
   default plan.

No production code changes — `get_runtime()` reads
`FXTR_CONTAINER_RUNTIME` at call time, per spawn
(`nix_container_runner._run_nix_container_internal`), so a
`monkeypatch.setenv` fixture fully controls the backend per test.

Decisions from discussion:

- **Wall-time**: accepted — both-installed roughly doubles container
  E2E time. No restriction knob; users wanting one backend deselect
  via the pytest invocation (`-k podman` etc. — parameterized ids make
  this easy).
- **Installed-but-not-running** (podman machine stopped, Apple
  Container service not started): tests **fail**, not skip. The
  `shutil.which` PATH check is the only skip condition, as today.
- **Externally set `FXTR_CONTAINER_RUNTIME`**: ignored by the swept
  tests (the fixture overrides it), with a once-per-session warning so
  `FXTR_CONTAINER_RUNTIME=docker pytest …` doesn't silently mean
  something different than before.

## Chunk 1 — the sweep fixture + test parameterization (done 2026-06-07)

- `tests/conftest.py`: add
  - `SWEEP_CONTAINER_RUNTIMES = ("podman", "container")` — the
    supported-runtime list, one place.
  - `container_runtime` fixture, `params=SWEEP_CONTAINER_RUNTIMES`
    (ids = the runtime names): `pytest.skip` if
    `shutil.which(rt)` is `None`, else
    `monkeypatch.setenv("FXTR_CONTAINER_RUNTIME", rt)` and return
    `rt`.
  - a `pytest_configure` (or first-use) warning when
    `FXTR_CONTAINER_RUNTIME` is already set in the environment:
    container tests sweep over the supported runtimes and override
    it.
- Swept tests (each takes the `container_runtime` fixture; their
  module-level `needs_container` markers go away for these):
  - `test_nix_container_spawner.py::test_spawn_nix_container_service_greet`
  - `test_nix_container_transport.py` E2E tests
  - `test_nested_spawn_container_inner.py`
- **Not** swept:
  - `test_spawn_kind_mismatch_raises` — fails at the nix `kind` check
    before any runtime is touched; drop its `needs_container` marker
    too (it never shells out to a container runtime).
  - `test_spawn_nix_container_without_git_repo_raises` — no runtime,
    already unmarked.
  - `test_nix_container_delivery.py` — unit-level, already covers
    both delivery branches with mocked subprocesses.
- Sanity notes (no action expected): `monkeypatch.setenv` is
  process-local and xdist workers run tests sequentially, so the
  env-var override is race-free under `-n auto`; the nix image build
  is content-addressed and shared, so the second backend pays only
  delivery + run; the podman machine VM and Apple Container VM
  coexist.

## Chunk 2 — posture rewording ("escape hatch" → supported non-default) (done 2026-06-06)

Soften wording so readers don't conclude non-podman backends are
unsupported/abandonable. podman remains "the recommended/default
runtime"; Apple Container becomes "a supported alternative backend
(tar delivery + direct socket mounts)"; docker remains a
compatibility escape hatch (untested — it's not in the sweep).

- `fxtr_sandboxing/_container_common.py` — `get_runtime()` docstring.
- `fxtr_sandboxing/nix_container_runner.py` — module docstring
  (delivery-branch description).
- `fxtr_sandboxing/CODE_OVERVIEW.md` — the `nix_container_runner` /
  `_container_common` sections + the Development section (which also
  documents the sweep, see chunk 3).
- `stage8_podman.md` — add a short dated addendum noting the posture
  update + pointer here (don't rewrite the historical decision log).
- `followups.md` — reframe "Collapse the runtime escape hatch + tar
  fallback (post-stage-9)" as conditional: collapse only as a
  deliberate decision if the sweep's maintenance cost stops earning
  its keep, not as the default post-stage-9 step. Mention the sweep
  as the thing keeping the backend honest.
- `overview.md` / `stage9_nix_runner.md` — touch the "escape hatch"
  phrasings only where they'd confuse (historical plan text can
  stay).

## Chunk 3 — docs for the sweep (done 2026-06-06)

- `fxtr_sandboxing/CODE_OVERVIEW.md` Development section: container
  tests sweep over podman + Apple Container; per-runtime skip when
  not on PATH; installed-but-not-started fails (start the machine /
  service); `FXTR_CONTAINER_RUNTIME` is ignored by tests (warning),
  but still selects the backend for non-test use; deselect a backend
  with `-k`.
- Update CODE_OVERVIEW.md (final check that all touched packages'
  overviews are current).

## End check (verified 2026-06-07)

- With both runtimes installed: the swept tests collect with
  `[podman]` + `[container]` ids and pass under both (8 passed —
  6 swept + 2 unswept guards — ~2 min on warm caches).
- With one missing from PATH: its half skips with a clear reason; the
  other half runs. (Skip path not exercised on this host — both
  runtimes installed; the `shutil.which` gate is trivial.)
- `FXTR_CONTAINER_RUNTIME=docker uv run pytest tests/test_nix_container_spawner.py`
  warns and still sweeps podman + container only. (Verified: all 6
  swept tests emitted the override warning.)
- `grep -ri "escape hatch"` over `fxtr_sandboxing` live code + docs
  returns only the docker-compatibility sense (or nothing).

## Implementation notes (2026-06-07)

- **Host-env incident during validation, unrelated to the change**:
  the first full sweep run failed all 6 E2E tests at the *nix image
  build* — the virby builder VM's outbound network was dead
  (`cache.nixos.org` connect timeouts from inside the VM; instant
  from the host; persisted across a `virbyd` restart). Cause:
  **Tailscale on the host** breaking vfkit/vmnet NAT — toggling
  Tailscale off fixed it immediately. It had coexisted fine before,
  so the suspect is a Tailscale *mode* change (exit node enabled /
  DNS override / update) rather than Tailscale per se. The podman
  machine is immune (libkrun/gvproxy does user-space networking, no
  vmnet). If it recurs: check `tailscale status` for an active exit
  node before blaming the runtimes.
