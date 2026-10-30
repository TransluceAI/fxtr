# Stage 2p4 (part 2) — Host-death container teardown

## Goal

Make every long-lived container fxtr starts **tear itself down when the owning
host process goes away** (crash, SIGKILL, the spawner job dying), instead of
leaking running containers that never get cleaned up. Uses podman's native
health-check action (`--health-on-failure=stop`) + `--rm` — no host-side
reaper, no heartbeat daemon we write, no podman socket handed to a container.

Companion to `plan.md` (the warm builder) in this folder: the warm builder made
the builder/deliverer live for a whole env instead of per-spawn, which widened
the window in which a host death leaves them running — this closes it.

## Problem

Four container kinds, three of which run `sleep infinity` as PID 1 and so
**never exit on their own** — if the host dies before its explicit `rm -f`,
they run forever:

| Container | Run | PID 1 | Leaks on host death? |
|---|---|---|---|
| Builder (`fxtr_nix_runner`) | `run -d` | `sleep infinity` | **yes** |
| Deliverer (`fxtr_nix_runner`) | `run -d` | `sleep infinity` | **yes** |
| Sidecar (`fxtr_sandboxing`, remote pod) | `run -d --pod` | `sleep infinity` | **yes** |
| Sandbox (`nix_container_runner`) | `run --rm` (attached) | guest shim | mostly self-cleans |

The three cleanup CLIs (`fxtr_nix clear`, `clear-spawn-debris`, `clear-images`)
are **manual** and **blanket** (they'd kill a concurrent live session's
containers), so they can't run automatically.

**Why it's hard:** on macOS the host (Python) and the containers (Linux VM) are
different kernels — no cgroup / PID-namespace / parent-death signal crosses the
boundary, and `--rm` only fires when the *container* exits, not when the *host
client* dies. So we need an application-level liveness signal that crosses
host→VM, and podman to act on it.

## Spike findings (podman 5.8.2, applehv machine) — the design's foundation

1. **`--health-on-failure=stop` + `--rm` stops *and removes*** a container ~2s
   after it goes unhealthy (`--health-interval=2s --health-retries=1`). So
   podman does the teardown for us.
2. **A dying `podman exec -i` client propagates promptly across the VM
   boundary**: SIGKILL'ing the client made the in-container process get stdin
   EOF and terminate, with nothing left running. This is what makes the
   sandbox self-clean chain (below) work, and confirms connection death is a
   real, prompt signal in the VM.
3. **Builder image `nixos/nix:2.34.7`**: has `bash` + `timeout`; **no `nc`, no
   `flock`**. `bash`'s `/dev/tcp` covers a TCP liveness check.
   **`host.containers.internal` is reachable** from a normal networked
   container.
4. **`flock` is released on holder death inside the VM** (kernel-guaranteed,
   even on SIGKILL) — a robust liveness signal — and **alpine has `flock`**
   (the sidecar is alpine).

## Design — one mechanism (`--health-on-failure=stop`), signal per container

The signal differs because the containers differ in what host-linked resource
they can observe (networked vs `--network none` pod).

### Builder + deliverer — TCP to a host liveness beacon

They have a normal network, and `host.containers.internal` is reachable
(finding 3). Add a **liveness beacon** to `fxtr_nix_runner`'s `PodmanBackend`: a
listening TCP socket on `0.0.0.0:0` held for the process's life, with a daemon
thread that accepts + closes connections. Its socket dies with the process
(incl. SIGKILL), so a dial fails exactly when the owner is gone. (A daemon
thread — not an async task — so the backend needs no task-group lifecycle;
`sock.getsockname()[1]` gives the port.)

Each builder/deliverer `run` gains (port = the beacon's):

```
--health-cmd 'bash -c "echo > /dev/tcp/host.containers.internal/<PORT>"'
--health-interval=5s --health-timeout=2s --health-retries=2
--health-on-failure=stop --rm
```

Host dies → beacon socket closed → dial refused → unhealthy after ~2 checks →
podman stops + `--rm` removes. (`bash` present per finding 3; no `nc` needed.)

### Sidecar — flock over the shared volume, released by the ssh session

The pod is `--network none`, so `host.containers.internal` is unreachable
inside it — but the ssh tunnel already crosses host→pod, and finding 2 shows the
ssh session dies promptly when the host dies (the host ssh client's
ProxyCommand is `podman exec -i sidecar sshd -i`; client death → in-VM sshd
EOF → session ends). Turn that into a kernel-robust signal with an `flock`
(finding 4) on the shared volume, held by the ssh **remote command**:

- Remote command (in `open_sidecar_tunnel`), today `… echo READY; exec sleep
  infinity`, becomes `… echo READY; exec flock -x /shared/.hostlive sleep
  infinity` — holds an exclusive lock for the session's life; the lock releases
  (kernel) when the session dies.
- Sidecar `run` gains a health check that reports **unhealthy when the lock is
  free** (i.e. the session — and thus the host — is gone):

  ```
  --health-cmd 'flock -n /shared/.hostlive -c true && exit 1 || exit 0'
  --health-interval=5s --health-timeout=2s --health-retries=2
  --health-on-failure=stop --rm
  ```

  (`flock -n` succeeds ⇒ lock free ⇒ host gone ⇒ exit 1 = unhealthy.) Both the
  holder and the checker run *inside the sidecar* (same VM kernel), so flock
  coordinates correctly. Needs `--rm` added to the sidecar run (today `-d`, no
  `--rm`).

No host beacon is needed for the sidecar — its signal is entirely in-VM.

### Sandbox — no health check; rides the guest's exit

The sandbox is `--network none` and we must **not** depend on the user image
having any tooling. It doesn't need a health check: its lifeline is the
ssh-forwarded task socket, and finding 2's chain means host death → forwarded
socket + guest RPC connection drop → the guest (`fxtr_service_runner`, *our*
code) exits → `--rm` removes the sandbox. The only in-container dependency is
our own shim exiting on connection loss — no user tooling. (Local-podman path:
the guest's socket connection breaks the same way when the host dies; `--rm`
cleans up.)

## Interactions

- **Warm-builder recycle synergy (from `plan.md`).** If a builder self-stops
  (health failure — e.g. a transient beacon blip while the host is actually
  alive), `--rm` removes it and the registry's cached `_Builder` goes stale; the
  next `builder_for` exec hits rc 125 → `BuilderTransportError` → `invalidate` →
  rebuild. So a *false* teardown self-heals on next use — the blip-robustness
  worry for the beacon path is largely absorbed by the recycle we already built.
- **Normal teardown unchanged.** Explicit `_stop_builder` / tunnel-`finally`
  `rm -f` still run on clean exit; `--rm` is only load-bearing on host death.
- **Multi-host safety.** Each host's beacon is its own socket; the sidecar flock
  is per-pod. Nothing here can tear down another host's / session's containers
  (unlike a blanket reap), so it's safe when several sessions share one machine.

## Accepted residuals (not handled — cheap, per the "keep empty pods" decision)

- After a host death the **empty pod + per-spawn volume** remain (nothing
  removes them without host-side code or a podman socket in a container). They
  consume no CPU/RAM; `clear-spawn-debris` mops them up. Explicitly accepted.
- **Sandbox with a hung guest**: if user code wedges so the guest never notices
  the socket drop, the sandbox container could linger. Possible follow-up: a
  guest-side watchdog **thread** (our code, no image tooling) that `os._exit`s
  on task-socket EOF. Out of scope here; the acute leak (the `sleep infinity`
  trio) is fully covered.
- **Host hang (not death)**: the beacon socket stays open / the flock stays
  held, so no teardown. By design — we target death, not hangs.
- ~10s detection latency (interval 5s × 2 retries). Fine for cleanup.

## Testing

- **Live (podman), the decisive one:** spawn a nix container through the harness,
  confirm builder + sidecar (+ sandbox) are running, `SIGKILL` the owning
  process, then assert after ~15s that the builder / deliverer / sidecar
  containers are **gone** (health-stopped + `--rm`), leaving at most an empty
  pod + volume. Do it for the remote (sidecar) path; a builder-only variant
  (kill the process holding a warm builder) covers the beacon path.
- **Beacon unit test** (`fxtr_nix_runner`): the beacon reports a port, accepts +
  closes a connection while alive, and the socket is closed once the beacon is
  stopped (connect refused).
- **Health-cmd string tests**: assert the exact run args are assembled with the
  beacon port / flock path (pure arg-construction, no podman), mirroring the
  existing podman-backend arg tests.
- Reuse the `perf`/`collect_perf` harness only if useful; this is mostly a
  process-death integration test.

## Implementation steps

- [x] `fxtr_nix_runner`: a `LivenessBeacon` (listening socket + accept/close
      daemon thread + `.port`), owned by `PodmanBackend`, started lazily on
      first builder; beacon + `_beacon_health_args` unit tests.
- [x] Add the health args + `--rm` to the builder and deliverer `run` in
      `_start_builder` / `_start_deliverer`.
- [x] Update `fxtr_nix_runner/CODE_OVERVIEW.md` (beacon + builder/deliverer
      self-teardown).
- [x] `fxtr_sandboxing`: the sidecar ssh remote command holds the `flock` over
      a `cat` on the ssh-forwarded stdin (**dead-man's switch** — a plain flock
      wasn't enough, see note below); flock health args + `--rm` on the sidecar
      `run`; ssh `stdin=PIPE` held open.
- [x] Confirmed the sandbox self-cleans on host death (guest exits when the
      forwarded task socket drops); hung-guest residual documented below.
- [x] Live process-death integration test (`test_host_death_cleanup.py`).
- [x] Update `fxtr_sandboxing/CODE_OVERVIEW.md`.

## Implementation note — the orphaned-ssh-client trap

The first cut used a flock held by `exec sleep infinity` under the ssh session,
expecting the session to end when the host died. **It didn't work:** the E2E
spike showed the ssh *client* orphan-survives a SIGKILL of its Python parent on
macOS (reparented, no SIGHUP, no EPIPE since it wasn't writing), so the session —
and the flock — stayed alive and the sidecar leaked. The fix is the **stdin
dead-man's switch**: ssh runs with `stdin=PIPE` (Python holds the write end) and
the remote command is `flock -x /shared/.hostlive -c 'cat >/dev/null'`. When the
host dies its end of the pipe closes, ssh forwards EOF, `cat` exits, and the
kernel releases the flock — which the health check turns into a teardown. The
builder/deliverer beacon path had no such issue (the beacon *socket* closes with
the process, OS-guaranteed). Verified end-to-end: builder+deliverer gone ~10s
after SIGKILL, sidecar ~15-20s; only an empty pod + volume remain.

## Open questions

- Beacon binding: one per `PodmanBackend` instance is simplest; confirm nothing
  objects to a long-lived `0.0.0.0` listener (macOS local bind — spike showed it
  reachable and fine).
- Exact `--health-cmd` quoting/interpreter form (string CMD-SHELL vs list CMD)
  for `bash -c "…/dev/tcp…"` in the nixos/nix image — verify `/bin/sh` + `bash`
  on the health PATH during implementation (finding 3 says both exist).
