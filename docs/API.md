# API reference

Base URL: `http://127.0.0.1:8765` (loopback) and `http://<tailnet-ip>:8765`.
Interactive schema: `/api/docs`.

**Auth.** Requests from loopback (`127.0.0.1`, `::1`) need no credentials.
Everything else needs `Authorization: Bearer <token>` (or `X-Api-Key`), where the
token is the contents of `%LOCALAPPDATA%\hermes-sysmon\api_token.txt`. SSE
accepts `?token=` because EventSource cannot set headers. `/api/health` and
`/api/metrics` are open (they expose no secrets, and Prometheus cannot send a
header without extra config).

---

## Health & state

### `GET /api/health`
```json
{"ok": true, "agent": "hermes-win11-ops:sysmon-peeweeherman", "version": "0.1.0",
 "uptime_s": 412.7, "elevated": true, "runstate": "desktop", "busy": false, "time": 1795609746.8}
```

### `GET /api/state`
Everything the dashboard needs in one call.

```json
{
  "current": {"runstate": "desktop", "since": 1795609752.9, "source": "transition"},
  "transition": {
    "id": "d4b0c4e40849", "state": "done", "from": null, "to": "desktop",
    "to_label": "Desktop", "started_at": 1795609751.7, "ended_at": 1795609753.1,
    "duration_s": 1.2, "progress": 100.0, "eta_s": 0.0, "dry_run": false,
    "force_userland": false, "error": "",
    "steps": [
      {"id": "power", "label": "Balanced power plan", "action": "power.plan",
       "phase": "apply", "status": "ok", "detail": "activated 'Balanced' (...)",
       "verify_ok": true, "verify_detail": "active plan = Balanced (...)",
       "started_at": 1795609751.7, "ended_at": 1795609751.9, "duration_s": 0.2}
    ],
    "gates": [{"kind": "no_pending_reboot", "ok": true, "policy": "warn", "detail": "no pending reboot"}]
  },
  "busy": false,
  "history": [ /* last 20 transition summaries */ ],
  "drift": [ /* last 10 drift reports */ ],
  "summary": {"cpu_avg_60s": 4.8, "gpu_avg_60s": 2.0},
  "latest": {"t": 1795609743.0, "cpu": 5.3, "mem": 51.6, "gpu": 2.0, "disk_read_mbs": 0.13,
             "net_rx_mbs": 0.012, "procs": 390, "top": [{"pid": 50444, "name": "chrome.exe", "mem_mb": 5171.9, "cpu": 1.9}]},
  "head": {"slug": "...", "local_dir": "...", "share_dir": "...", "last_error": ""},
  "logs": {"dir": "...", "queued": 0, "shipped_http": 0, "share_failures": 0, "last_error": ""}
}
```

`transition.state` is one of `running`, `aborting`, `done`, `failed`, `aborted`.
`step.status` is one of `pending`, `running`, `ok`, `failed`, `skipped`, `aborted`.

---

## Run-states

### `GET /api/runstates`
```json
{"runstates": [{"id": "game", "label": "Game", "description": "...", "est_seconds": 55.0,
                "steps": 6, "restart_userland": false}],
 "errors": [], "current": "desktop"}
```

### `GET /api/runstates/{id}`
The full manifest, including steps and gates.

### `POST /api/runstates/reload`
Re-read `runstates/*.json`. Returns `{"ok": true, "ids": [...], "errors": []}`.

### `GET /api/gates?target={id}`
Preflight verdicts. Without `target`, evaluates the host-health default set
(pending reboot, free disk, crash events).

```json
{"gates": [{"kind": "no_crash_events", "ok": true, "policy": "warn",
            "detail": "0 app-crash event(s) in 24h (max 0)", "value": {"count": 0}}],
 "blocking": []}
```

---

## Transitions

### `POST /api/transition`
```json
{"target": "game", "dry_run": false, "force_userland_restart": null}
```

* `target` (required) — run-state id.
* `dry_run` — build the plan, evaluate gates, execute nothing (steps reported `skipped`).
* `force_userland_restart` — `true`/`false` overrides the manifest's `userland.mode`;
  `null`/omitted means "use the manifest".

Responses: `200` with the same shape as `GET /api/state`; `404` unknown target;
`409` a transition is already running; `400` missing target.

### `POST /api/abort`
Cooperative STOP. The engine finishes the step already in flight, marks the rest
`aborted`, keeps completed steps `ok`, and leaves `current` untouched.

```json
{"ok": true, "detail": "abort requested; stopping before the next step"}
```
`{"ok": false, "detail": "no transition in progress"}` when idle.

---

## Telemetry

### `GET /api/stream` (SSE)
One JSON frame per second:
```json
{"health": {"runstate": "game", "busy": true, "uptime_s": 900.0},
 "state": { ...same as /api/state... },
 "latest": { ...one sample... },
 "summary": {"cpu_avg_60s": 22.1}}
```
Remote callers: `/api/stream?token=<token>`.

### `GET /api/samples?seconds=300&limit=3600`
Ring buffer rows (`t`, `cpu`, `mem`, `gpu`, `disk_*`, `net_*`, `top`).

### `GET /api/metrics` (Prometheus text)
```
sysmon_cpu_percent{agent="..."} 5.3
sysmon_mem_percent{agent="..."} 51.6
sysmon_gpu_percent{agent="..."} 2.0
sysmon_disk_read_mbs{agent="..."} 0.13
sysmon_disk_write_mbs{agent="..."} 0.43
sysmon_net_rx_mbs{agent="..."} 0.012
sysmon_transition_progress{agent="...",to="game"} 40.0
sysmon_transition_active{agent="..."} 1
sysmon_uptime_seconds{agent="..."} 900.0
```

### `GET /api/logs?n=200`
Recent records in the same shape as the JSONL / Kibana documents:
`@timestamp`, `event.dataset`, `event.kind`, `log.level`, `message`, `host.name`,
`agent.id`, `sysmon`.

---

## Head / supervisor

### `GET /api/head`
This worker's `sysmon.head/v1` record (see `docs/ARCHITECTURE.md`).

### `POST /api/head/publish`
Force an immediate publish (local, share, supervisor). Returns `{"ok": bool, "detail": "..."}`.

### `POST /api/head`
Supervisor inbox: accept a peer's head so it shows up in `/api/heads`.
Rejects anything whose `schema` is not `sysmon.head/v1` or that lacks `agent.id`.

### `GET /api/heads`
```json
{"self": {"schema": "sysmon.head/v1", "...": "..."},
 "peers": [{"agent": {"id": "peer-1"}, "_received_at": 1795609900.1}],
 "links": {"grafana": "http://robertdobbs:3000", "kibana": "http://robertdobbs:5601"}}
```

### `GET /api/config`
Effective config with the supervisor token redacted and the API token replaced by
its fingerprint.

---

## CLI equivalents

```bat
.venv\Scripts\python.exe -m sysmon state
.venv\Scripts\python.exe -m sysmon runstates
.venv\Scripts\python.exe -m sysmon go game --dry-run
.venv\Scripts\python.exe -m sysmon go game --userland keep
.venv\Scripts\python.exe -m sysmon abort
.venv\Scripts\python.exe -m sysmon gates
.venv\Scripts\python.exe -m sysmon head
.venv\Scripts\python.exe -m sysmon open
```

Exit codes: `0` ok, `2` config error, `3` worker unreachable, `4` HTTP error
(the response body is printed).

---

## Remote example (from another tailnet node)

```bash
TOKEN=$(ssh peeweeherman 'type %LOCALAPPDATA%\hermes-sysmon\api_token.txt')
curl -s -H "Authorization: Bearer $TOKEN" http://100.x.y.z:8765/api/state
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"target":"game"}' http://100.x.y.z:8765/api/transition
```
