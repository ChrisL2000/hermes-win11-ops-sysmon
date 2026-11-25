# Architecture

```
                    +---------------------------+
    supervisor ----> |  head (JSON on SMB share) |  <-- written every 30 s
   (poll / dial)     +---------------------------+
                                  ^
                                  | publish
   +--------------------------------------------------------------+
   |  sysmon worker  (elevated, user session, logon task)         |
   |                                                              |
   |  sampler --1 s--> ring buffer --SSE--> dashboard / Grafana   |
   |                     |                                        |
   |  engine <-- manifest registry (runstates/*.json)             |
   |    |  plan -> steps -> executor -> read-back verify          |
   |    |  cancel token (STOP)                                    |
   |    +--> powercfg / sc / reg / Popen / wevtutil               |
   |                                                              |
   |  logship: JSONL -> SMB mirror -> Logstash (NDJSON)           |
   +--------------------------------------------------------------+
              ^                              ^
              | HTTP + bearer token          | Prometheus scrape
       remote tooling                Prometheus on robertdobbs
```

## Components

| module | responsibility | failure behaviour |
|---|---|---|
| `config.py` | defaults, JSON overlay, data dirs, token | bad JSON -> warn + defaults, never crash |
| `sampler.py` | CPU/mem/disk/net 1 s, GPU 2 s, process table 5 s | a denied counter degrades one sample |
| `runstates.py` | manifest load/validate/index | bad manifest is reported, others still load |
| `gates.py` | host preflight (uptime, disk, reboot, crash log) | returns a verdict, does not throw |
| `steps.py` | one Windows primitive per action + verifier | returns `{ok, detail}` |
| `engine.py` | plan, execute, progress/ETA, abort, persistence | failures recorded per step |
| `api.py` | REST/SSE/metrics/dashboard | auth enforced off loopback |
| `head.py` | identity record + heartbeat + share/supervisor publish | share outage logged, worker unaffected |
| `logship.py` | local JSONL, SMB mirror, optional HTTP | ships are best-effort, local file is truth |

## Transition lifecycle

1. `POST /api/transition {"target": "game"}` (or GUI GO).
2. Registry lookup -> `_build_plan()`:
   * `on_leave` steps of the **current** state (weighted at half),
   * optional userland-restart pseudo-step (only when requested),
   * the target's steps,
   * gates are evaluated now; any `block` gate fails the transition before
     the first step.
3. Worker thread executes steps in order. Before each step it checks the
   cancel flag; after each step it runs the read-back verifier.
4. Progress = completed weight / total weight. ETA is the remaining estimate
   corrected by observed speed (clamped 0.25x-4x so the number stays honest).
5. Terminal states: `done` (current state updated), `failed` (critical step),
   `aborted` (STOP; current state unchanged). Partial truth is persisted to
   `%LOCALAPPDATA%\hermes-sysmon\engine_state.json` after every transition.

## Head protocol (`sysmon.head/v1`)

```json
{
  "schema": "sysmon.head/v1",
  "agent": {"id": "...", "name": "...", "slug": "...", "role": "worker", "labels": {}},
  "host":  {"hostname": "...", "os": {"caption": "...", "build": "26300"}, "tailnet_ip": "100.x.y.z", "uptime_h": 12.3},
  "api":   {"endpoints": [{"url": "http://127.0.0.1:8765", "scope": "loopback"},
                          {"url": "http://100.x.y.z:8765", "scope": "tailnet"}],
            "token_fingerprint": "9f926baa472119e8", "auth": "bearer"},
  "state": {"runstate": "game", "runstate_label": "Game", "since": 1795609752.9, "transition": null},
  "links": {"grafana": "http://...", "kibana": "http://..."},
  "heartbeat_at": 1795609853.4, "heartbeat_iso": "2026-11-25T12:30:53", "started_at": 1795609736.2, "pid": 54316
}
```

Rules the supervisor can rely on:

* the file name is the stable `agent.slug`; the content is replaced atomically
  (tmp + `os.replace`), so a reader never sees a half-written head;
* `heartbeat_at` older than ~3x `heartbeat_s` means the worker is gone or wedged;
* the **token itself is never in the head** -- only its SHA-256 prefix, so a
  supervisor can prove it holds the right token without the share being a
  credential store;
* a peer head can be pushed to a supervisor with `POST /api/head`; it then
  appears in `GET /api/heads` (that is the "links to other agents" surface).

## Why this shape

* One process, no Windows service: services run in session 0 and cannot launch
  user apps onto the interactive desktop; a logon task with highest privileges
  gets both admin and the user's session.
* Manifest-first, because a run-state is a *policy*, not a script: it must be
  reviewable in a diff and reusable across hosts with different software.
* Read-back verify because the ops-tuning work showed return codes lie
  (a "successful" `powercfg` on a wrong GUID, a service that starts then dies).
* Best-effort ships so an ELK or SMB outage cannot stall a transition.
