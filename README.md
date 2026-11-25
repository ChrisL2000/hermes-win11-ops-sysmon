# hermes-win11-ops : sysmon

A Windows 11 **run-state worker** that a supervisory node can discover and drive,
and that otherwise runs autonomously: it samples the host in real time, moves the
machine between named run-states (desktop / work / game / game+stream / discord /
create / moves / safe), verifies every step by read-back, publishes a *head* for
a supervisor to connect to, and ships logs to the ELK stack on `robertdobbs`.

Status: **v0.1 vertical slice, running on PeeWeeHerman** (Win11 Pro 10.0.26300,
RTX 4070 Ti). Real transitions, real aborts, real read-backs. See
[Verification](#verification) for what was actually exercised.

---

## 1. What it does

* **Worker daemon** (`python -m sysmon serve`) on `127.0.0.1:8765` + the tailnet IP.
* **Run-state engine**: declarative JSON manifests -> ordered, cancellable,
  read-back-verified steps. One transition at a time; STOP halts between steps
  and records the partial state instead of pretending success.
* **Small window / dashboard**: run-state picker, GO, progress meter, ETA, live
  line graph (CPU / GPU / RAM + transition progress), top processes, log tail,
  and a red **STOP** that aborts in flight. Served at `/`, opened as a
  chromeless Edge window by `sysmon-ui.cmd`.
* **API**: REST + SSE + Prometheus text. Loopback is trusted; anything off
  loopback needs the bearer token from `%LOCALAPPDATA%\hermes-sysmon\api_token.txt`.
* **Head**: `sysmon.head/v1` JSON with identity, endpoints, token *fingerprint*,
  current run-state and dashboard links -- written locally, mirrored to the
  bots share when it is reachable, optionally POSTed to a supervisor.
* **Gates**: per-state preflight (uptime, free disk, pending reboot, crash
  events from the Windows event log) with `block` / `warn` / `off` policies.
* **Drift watch**: every 2 minutes the worker re-verifies the critical steps of
  the current run-state; drift is logged (and remediated only if
  `engine.auto_remediate` is on).
* **Log shipping**: JSONL locally, mirror to the SMB share, optional NDJSON POST
  to Logstash (ECS-ish fields for Kibana).

## 2. Quick start

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
schtasks /Run /TN hermes-sysmon
sysmon-ui.cmd
```

`install.ps1` builds `.venv`, installs the package, seeds
`%LOCALAPPDATA%\hermes-sysmon\config.json`, validates the manifests, and
registers a **logon task that runs the worker elevated inside your own session**
(needed for power/service/registry knobs *and* for launching user apps).

Ad-hoc use:

```bat
sysmon.cmd                      :: run in this console
sysmon-state.cmd                :: one-shot status + gates
.venv\Scripts\python.exe -m sysmon go game
.venv\Scripts\python.exe -m sysmon abort
```

## 3. Run-states

| id | what it does | notes |
|---|---|---|
| `desktop` | Balanced plan, no capture stack | default everyday state |
| `work` | Balanced, games/OBS down, Discord up | |
| `game` | High performance, CPU min 100%, browsers/TIDAL/OBS down | Steam up |
| `game-stream` | High performance + OBS + Discord + Steam | leaving it stops OBS |
| `discord` | Balanced, games/OBS down, Discord + TIDAL up | |
| `create` | High performance, Krita up, chat quiet | |
| `moves` | High performance for video work, big-disk gate | |
| `safe` | Everything heavy down, Power saver | live "safe harbour", not a safeboot |
| `selftest` | Five paced waits, changes nothing | demo the meter + STOP |

Add or edit a state by dropping a JSON file into `runstates/` and calling
`POST /api/runstates/reload` (or restarting). Schema: [docs/RUNSTATES.md](docs/RUNSTATES.md).

## 4. API (short form)

```
GET  /api/health                 no auth, loopback
GET  /api/state                  current + transition + steps + samples
GET  /api/runstates              list manifests
POST /api/transition             {"target":"game","dry_run":false,"force_userland_restart":false}
POST /api/abort                  stop in flight, leave host at reached state
GET  /api/stream                 SSE: state + latest sample every second
GET  /api/samples?seconds=300    ring buffer
GET  /api/gates?target=game      preflight
GET  /api/logs?n=200             recent JSONL records
GET  /api/metrics                Prometheus text
GET  /api/head                   this worker's head record
POST /api/head                   supervisor inbox: register a peer head
GET  /api/heads                  self + peers + dashboard links
GET  /api/docs                   OpenAPI UI
```

Full reference: [docs/API.md](docs/API.md).

## 5. Observability

* **Logs** -> JSONL in `%LOCALAPPDATA%\hermes-sysmon\logs`, mirrored to
  `\\us-hm-proxmox01\bots\logs\<host>`, optionally POSTed to Logstash.
  Wiring for `robertdobbs` (Elastic 8.19.24, Kibana on `robertdobbs:5601`):
  [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).
* **Metrics** -> `GET /api/metrics` is Prometheus-shaped; scraped by the existing
  Prometheus on `robertdobbs`, charted in Grafana (`robertdobbs:3000`).
* **Head** -> `\\us-hm-proxmox01\bots\heads\<agent>.json` is the rendezvous
  directory a supervisor scans. **The share does not exist yet** -- see
  [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md#2-create-the-bots-share).

## 6. Decisions taken (and why)

* **Python, not C++.** Everything the run-states need is Windows API through
  `ctypes` / `powercfg` / `sc` / `reg` / `wevtutil`; `psutil` + `nvidia-smi`
  cover the sampling. No compiler is installed on this box and none is needed.
  If you later want a single-file binary, `pyinstaller`/`nuitka` on this package
  is enough -- the design isolates all OS calls in `sysmon/steps.py`.
* **Manifest as source of truth, thin applier.** Matches the existing
  `hermes-agent-win11-26300-ops-tuning` convention; a run-state is auditable,
  diffable and reversible.
* **Read-back or it did not happen.** Every step runs a verifier
  (`powercfg /query`, `reg query`, `sc query`, process table, ...). A return
  code alone is never accepted.
* **Userland restart is selectable, not forced.** `userland.mode` per manifest
  (`keep` default) plus `force_userland_restart` per request. A blanket restart
  loses unsaved work, and the real work (stop OBS, start Discord, ...) is
  expressed as explicit steps. The shell (`explorer.exe`) and session-critical
  processes are never touched.
* **STOP means "stop where you are".** Abort is cooperative between steps; the
  engine marks the reachable steps `aborted`, keeps completed ones `ok`, and
  refuses to claim the target state.
* **Elevation via logon task, not UAC prompts.** A scheduled task with
  `RunLevel Highest` + `LogonType Interactive` gives admin *and* the user's
  session, so app launches land on the right desktop and nothing blocks on a
  consent dialog.
* **Gates before steps.** Cheap, host-level questions (uptime, free disk,
  pending reboot, crash events) with explicit `block`/`warn`/`off` policy.

## 7. Not built yet

1. Performance tuning beyond power plans (GPU power limits via NVIDIA Profile
   Inspector, MSI mode, timer resolution, process priority/affinity).
2. `bcdedit safeboot` (reboot-class Safe Mode) -- documented, deliberately not
   automated; it needs a second reboot to undo.
3. A GUI stop-priority: STOP aborts between steps, it cannot interrupt a
   `process.stop` grace window mid-wait.
4. Supervisor-side UI (a wall of peer heads + links); the API and the head
   protocol exist (`/api/heads`), the page does not.
5. Packaging as a single `.exe`, and code-signing.

## 8. Verification

Exercised on this host (not just unit-tested):

* `pytest` -> 36 passed (engine, gates, manifests, API, persistence).
* Live transition `desktop`: power plan read back as Balanced, processor min
  state applied and read back `AC index = 5`, OBS stop verified, Explorer
  verified, `current = desktop`.
* Live `selftest` + `POST /api/abort`: stopped at "Pace 3 of 5", progress 40%,
  steps 1-2 `ok`, 3-5 `aborted`, target state **not** claimed.
* Head published locally with the tailnet endpoint and token fingerprint; share
  mirror fails cleanly with `WinError 1326` because `\\us-hm-proxmox01\bots`
  does not exist yet.
* GUI: served and structurally verified (HTML/JS), JS syntax-checked with
  `node --check`. Not yet eyeballed in a real window on this build.

## 9. Layout

```
sysmon/            config, sampler, runstates, gates, steps, engine, api, head, logship, gui/
runstates/         one JSON manifest per run-state
scripts/           install.ps1 (+ -Uninstall, -NoTask)
docs/              ARCHITECTURE, RUNSTATES, API, OBSERVABILITY
tests/             pytest suite
```
