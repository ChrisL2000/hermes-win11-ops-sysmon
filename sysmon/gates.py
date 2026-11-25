"""Preflight gates.

"Should a transition have requirements?" -- yes, and they are declarative, per
run-state, with an explicit policy (block / warn / off). A gate answers one
question about the *host*, not about the transition, so it can also be run
standalone from the API (``GET /api/gates``) as a "is this box healthy?" probe.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, asdict
from typing import Any

import psutil

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class GateResult:
    kind: str
    ok: bool
    detail: str
    policy: str = "warn"
    value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def blocks(self) -> bool:
        return (not self.ok) and self.policy == "block"


def _run(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.SubprocessError, OSError) as exc:
        return 1, str(exc)


def _reg_query_value(hive: str, key: str, name: str) -> str | None:
    rc, out = _run(["reg", "query", rf"{hive}\{key}", "/v", name])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 3 and parts[0].lower() == name.lower():
            return parts[-1]
    return None


def pending_reboot() -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if _reg_query_value("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending", "") is not None:
        reasons.append("CBS RebootPending")
    # The key itself (not a value) is the signal; probe it directly.
    rc, _ = _run(["reg", "query", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"])
    if rc == 0 and "CBS RebootPending" not in reasons:
        reasons.append("CBS RebootPending")
    rc, out = _run(["reg", "query", r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager", "/v", "PendingFileRenameOperations"])
    if rc == 0 and "PendingFileRenameOperations" in out:
        reasons.append("PendingFileRenameOperations")
    return bool(reasons), reasons


def crash_events(lookback_h: float, event_ids: list[int]) -> list[dict[str, str]]:
    ids = " or ".join(f"EventID={int(i)}" for i in event_ids)
    ms = int(max(0.1, lookback_h) * 3600 * 1000)
    query = (f"*[System[({ids}) and TimeCreated[timediff(@SystemTime) <= {ms}]]]")
    rc, out = _run(["wevtutil", "qe", "Application", f"/q:{query}", "/c:20", "/rd:true",
                    "/f:text"], timeout=25)
    if rc != 0:
        return []
    events: list[dict[str, str]] = []
    for block in re.split(r"\r?\n\r?\n", out.strip()):
        if "Event[" not in block:
            continue
        rec: dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                rec[k.strip()] = v.strip()
        events.append(rec)
    return events


def evaluate(gate: dict[str, Any], cfg_gates: dict[str, Any] | None = None) -> GateResult:
    cfg_gates = cfg_gates or {}
    kind = str(gate.get("kind", ""))
    policy = str(gate.get("policy", "warn")).lower()
    if policy == "off":
        return GateResult(kind, True, "gate disabled", policy, None)

    if kind == "uptime_min_h":
        need = float(gate.get("value", cfg_gates.get("uptime_min_h", 0.0)))
        uptime_h = (time.time() - psutil.boot_time()) / 3600.0
        ok = uptime_h >= need
        return GateResult(kind, ok, f"uptime {uptime_h:.2f}h (need >= {need}h)", policy, round(uptime_h, 2))

    if kind == "disk_free_min_gb":
        need = float(gate.get("value", cfg_gates.get("disk_free_min_gb", 10.0)))
        root = os.environ.get("SystemDrive", "C:") + "\\"
        du = psutil.disk_usage(root)
        free_gb = du.free / 1_073_741_824
        ok = free_gb >= need
        return GateResult(kind, ok, f"{root} free {free_gb:.1f} GB (need >= {need:.0f} GB)", policy, round(free_gb, 1))

    if kind == "no_pending_reboot":
        policy = policy if policy != "warn" else str(cfg_gates.get("pending_reboot", "warn"))
        owed, reasons = pending_reboot()
        return GateResult(kind, not owed, "no pending reboot" if not owed else "reboot owed: " + ", ".join(reasons),
                          policy, reasons)

    if kind == "no_crash_events":
        lookback = float(gate.get("hours", cfg_gates.get("crash_lookback_h", 24)))
        ids = list(gate.get("event_ids") or cfg_gates.get("crash_event_ids") or [1000, 1001])
        max_events = int(gate.get("max", 0))
        events = crash_events(lookback, ids)
        ok = len(events) <= max_events
        sample = [e.get("Description", "")[:80] for e in events[:3]]
        detail = f"{len(events)} app-crash event(s) in {lookback:.0f}h (max {max_events})"
        return GateResult(kind, ok, detail, policy, {"count": len(events), "sample": sample})

    if kind in ("process_running", "process_stopped"):
        names = [str(n).lower() for n in (gate.get("names") or ([gate["name"]] if gate.get("name") else []))]
        if not names:
            return GateResult(kind, False, "no process names given", policy, None)
        live = {p.info["name"].lower() for p in psutil.process_iter(["name"]) if p.info.get("name")}
        present = sorted(n for n in names if n in live)
        if kind == "process_running":
            ok = bool(present)
            detail = f"running: {', '.join(present) or 'none of ' + ', '.join(names)}"
        else:
            ok = not present
            detail = f"stopped: {', '.join(names) if ok else 'still running ' + ', '.join(present)}"
        return GateResult(kind, ok, detail, policy, present)

    return GateResult(kind, False, f"unknown gate kind '{kind}'", "block", None)


def evaluate_all(gates: list[dict[str, Any]], cfg_gates: dict[str, Any] | None = None) -> list[GateResult]:
    return [evaluate(g, cfg_gates) for g in (gates or [])]


def host_snapshot_cfg(cfg_gates: dict[str, Any]) -> list[dict[str, Any]]:
    """The default gate set used by ``GET /api/gates`` (host health, no target state)."""
    return [
        {"kind": "no_pending_reboot", "policy": "warn"},
        {"kind": "disk_free_min_gb", "value": cfg_gates.get("disk_free_min_gb", 10), "policy": "warn"},
        {"kind": "no_crash_events", "hours": cfg_gates.get("crash_lookback_h", 24), "policy": "warn"},
    ]
