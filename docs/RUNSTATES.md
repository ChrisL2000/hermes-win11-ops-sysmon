# Run-state manifests

One JSON file per state in `runstates/`. Files are loaded at startup and on
`POST /api/runstates/reload`; a broken file is reported in `errors` and the rest
still load.

## Schema

```jsonc
{
  "id": "game",                     // required, unique, used in the API
  "label": "Game",                  // shown in the picker
  "icon": "gamepad",                // free-form hint for the GUI
  "description": "one line",
  "est_seconds": 55,                // estimate used for ETA maths
  "x_notes": "ignored by the engine, kept for humans",

  "userland": {                     // blanket process restart -- off by default
    "mode": "keep",                 // keep | restart
    "keep": ["discord.exe"],        // extra processes to spare on restart
    "grace_s": 8
  },

  "gates": [                        // evaluated before the first step
    {"kind": "no_pending_reboot", "policy": "warn"},
    {"kind": "no_crash_events", "hours": 6, "max": 2, "policy": "warn"},
    {"kind": "uptime_min_h", "value": 0.1, "policy": "warn"},
    {"kind": "disk_free_min_gb", "value": 40, "policy": "warn"}
  ],

  "steps": [ /* ordered, see below */ ],

  "on_leave": [ /* steps to run when leaving THIS state */ ]
}
```

Gate kinds: `uptime_min_h`, `disk_free_min_gb`, `no_pending_reboot`,
`no_crash_events` (`hours`, `max`, `event_ids`), `process_running`,
`process_stopped`. Policy: `block` (fails the transition before step 1),
`warn` (recorded, continues), `off`.

## Step schema

```jsonc
{
  "id": "power",                    // unique within the manifest
  "label": "High performance power plan",
  "action": "power.plan",           // from the table below
  "params": {"plan": "High performance"},
  "est_s": 2,                       // ETA contribution
  "weight": 1.0,                    // progress contribution
  "critical": true,                 // false = failure does not stop the run
  "elevated": true,                 // false = allowed without admin
  "verify": {"kind": "power_plan", "plan": "High performance"}   // optional
}
```

Actions: `power.plan`, `power.setting`, `process.start`, `process.stop`,
`process.ensure_running`, `process.ensure_stopped`, `service.set`,
`registry.set`, `env.set`, `script.run`, `cmd.run`, `noop`.

Verify kinds: `power_plan`, `power_setting` (`subgroup`, `setting`, `ac_value`),
`process_running`, `process_stopped`, `service_state`, `registry_value`, `cmd`.
When `verify` is omitted the engine picks a sensible read-back from the action
(for `power.plan`, process steps and `service.set`); anything else falls back to
"executor result only", which is reported as such.

## Troubleshooting

* **"elevation required"** -- the worker was started without admin. Install the
  logon task (`scripts\install.ps1`) rather than running `sysmon.cmd` from a
  normal shell.
* **A step fails on read-back but the action looked fine** -- that is the point;
  the detail string carries both the executor output and the read-back value.
* **A run-state hangs** -- STOP, then look at `GET /api/state` -> `steps[]`
  with `duration_s`; the offending step is the one that was `running`.

## Safe Mode (deliberately manual)

A real Safe Mode boot is reboot-class and cannot be undone without a second
reboot, so it is **not** an automated step:

```bat
bcdedit /set {current} safeboot minimal   :: then reboot; Safe Mode
bcdedit /deletevalue {current} safeboot   :: then reboot; back to normal
```

If you want it automated later, add it as a manifest of its own with
`"requires_reboot": true` steps and a dedicated "unsafe-reboot" run-state, and
make the supervisor the only caller -- not the GUI picker.

## Adding a host-specific state

1. Copy the closest manifest, change `id`/`label`.
2. Point `process.start` at real executables on that box
   (`Get-Command`, or the `DisplayName` in the uninstall registry).
3. `POST /api/runstates/reload`, then `POST /api/transition {"target": "...",
   "dry_run": true}` and read the gate verdicts before a live run.
