"""Transition engine: cancellable, verifiable, observable.

Design rules that came out of the ops-tuning work:
* the manifest is the source of truth; the engine is a thin applier;
* every step is followed by a read-back (never trust a return code);
* a transition can be stopped between steps and leaves the host at whatever
  state it actually reached -- the engine records that partial state instead of
  pretending it either fully applied or fully failed;
* one transition at a time, one thread, one event log.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psutil

from . import gates as gates_mod
from . import steps as steps_mod
from .runstates import RunState, Step

IDLE = "idle"
RUNNING = "running"
ABORTING = "aborting"
DONE = "done"
FAILED = "failed"
ABORTED = "aborted"

USERLAND_PROTECTED = {
    "system", "registry", "memory compression", "secure system", "idle", "smss.exe",
    "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "sihost.exe",
    "ctfmon.exe", "taskhostw.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "searchhost.exe", "runtimebroker.exe", "textinputhost.exe", "python.exe", "pythonw.exe",
    "conhost.exe", "cmd.exe", "powershell.exe", "windowsterminal.exe", "openconsole.exe",
    "nvidia share.exe", "nvcontainer.exe", "nvsphelper64.exe",
}


@dataclass
class StepRecord:
    id: str
    label: str
    action: str
    phase: str
    status: str = "pending"      # pending | running | ok | failed | skipped | aborted
    detail: str = ""
    started_at: float | None = None
    ended_at: float | None = None
    verify_ok: bool | None = None
    verify_detail: str = ""
    weight: float = 1.0
    est_s: float = 3.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "action": self.action, "phase": self.phase,
            "status": self.status, "detail": self.detail,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "verify_ok": self.verify_ok, "verify_detail": self.verify_detail,
            "weight": self.weight, "est_s": self.est_s,
            "duration_s": round((self.ended_at or time.time()) - self.started_at, 2) if self.started_at else None,
        }


class Engine:
    def __init__(self, cfg, registry, on_event: Callable[[str, dict], None] | None = None):
        self.cfg = cfg
        self.registry = registry
        self.on_event = on_event or (lambda kind, data: None)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._records: list[StepRecord] = []
        self.state_path = Path(cfg.data_dir) / "engine_state.json"

        self.current: dict[str, Any] = {"runstate": None, "since": None, "source": "boot"}
        self.transition: dict[str, Any] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=int(cfg.engine.get("record_history", 200)))
        self.drift: list[dict[str, Any]] = []
        self._load_state()

    # ------------------------------------------------------------- public
    @property
    def busy(self) -> bool:
        return bool(self.transition and self.transition["state"] in (RUNNING, ABORTING))

    def start(self, target_id: str, options: dict | None = None) -> dict[str, Any]:
        options = options or {}
        state = self.registry.get(target_id)
        if state is None:
            raise KeyError(f"unknown run-state '{target_id}'")
        with self._lock:
            if self.busy:
                raise RuntimeError(f"transition already running (to '{self.transition['to']}')")
            previous = self.current.get("runstate")
            prev_manifest = self.registry.get(previous) if previous else None
            plan = self._build_plan(state, prev_manifest, options)
            self._cancel.clear()
            self._records = plan["records"]
            self.transition = {
                "id": uuid.uuid4().hex[:12],
                "state": RUNNING,
                "from": previous,
                "to": state.id,
                "to_label": state.label,
                "started_at": time.time(),
                "ended_at": None,
                "progress": 0.0,
                "eta_s": round(plan["est_s"], 1),
                "dry_run": bool(options.get("dry_run")),
                "force_userland": bool(options.get("force_userland_restart")),
                "steps": [r.as_dict() for r in plan["records"]],
                "gates": plan["gates"],
                "error": "",
                "abort_requested_at": None,
                "est_s": plan["est_s"],
            }
            self._thread = threading.Thread(target=self._run, args=(state, prev_manifest, options, plan),
                                            name="sysmon-transition", daemon=True)
            self._thread.start()
            self._persist()
        self._emit("transition.start", {"to": state.id, "from": previous})
        return self.status()

    def abort(self) -> dict[str, Any]:
        with self._lock:
            if not self.busy:
                return {"ok": False, "detail": "no transition in progress"}
            self._cancel.set()
            self.transition["state"] = ABORTING
            self.transition["abort_requested_at"] = time.time()
        self._emit("transition.abort", {"to": self.transition["to"]})
        return {"ok": True, "detail": "abort requested; stopping before the next step"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current": dict(self.current),
                "transition": dict(self.transition) if self.transition else None,
                "busy": self.busy,
                "history": list(self.history)[-20:],
                "drift": list(self.drift)[-10:],
            }

    def set_current(self, runstate_id: str | None, source: str = "manual") -> None:
        with self._lock:
            self.current = {"runstate": runstate_id, "since": time.time(), "source": source}
            self._persist()

    # ------------------------------------------------------------ storage
    def _load_state(self) -> None:
        """Current run-state and recent history survive a worker restart."""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        cur = data.get("current") or {}
        if cur.get("runstate"):
            self.current = {"runstate": cur.get("runstate"), "since": cur.get("since"),
                            "source": cur.get("source", "restored")}
        for item in data.get("history") or []:
            self.history.append(item)
        last = data.get("last_transition")
        if last and last.get("state") in (RUNNING, ABORTING):
            # A worker restart mid-transition: record it as interrupted, do not
            # pretend it finished and do not resume it automatically.
            last = {**last, "state": FAILED, "error": "interrupted by worker restart"}
        self.transition = last

    def _persist(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "current": self.current,
                "history": list(self.history)[-100:],
                "last_transition": self.transition,
                "saved_at": time.time(),
            }
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # -------------------------------------------------------------- plan
    def _build_plan(self, target: RunState, prev: RunState | None, options: dict) -> dict[str, Any]:
        records: list[StepRecord] = []
        leave_steps: list[Step] = []
        if prev is not None and prev.id != target.id:
            leave_steps = prev.on_leave
        for s in leave_steps:
            records.append(StepRecord(s.id, f"[leave {prev.id}] {s.label}", s.action, "on_leave",
                                      weight=max(0.25, s.weight * 0.5), est_s=s.est_s))
        restart = options.get("force_userland_restart")
        restart = target.restart_userland if restart is None else bool(restart)
        if restart:
            records.append(StepRecord("_userland_restart", "Restart userland processes (shell kept)",
                                      "internal.userland", "userland", weight=1.0, est_s=20.0))
        for s in target.steps:
            records.append(StepRecord(s.id, s.label, s.action, "apply",
                                      weight=max(0.01, s.weight), est_s=s.est_s))
        est = sum(r.est_s for r in records) or target.est_seconds
        gate_specs = list(target.gates or [])
        gate_results = [g.as_dict() for g in gates_mod.evaluate_all(gate_specs, self.cfg.gates)]
        return {"records": records, "est_s": est, "gates": gate_results}

    # --------------------------------------------------------------- run
    def _run(self, target: RunState, prev: RunState | None, options: dict, plan: dict) -> None:
        dry = bool(options.get("dry_run"))
        started = time.time()
        blocked = [g for g in plan["gates"] if not g["ok"] and g["policy"] == "block"]
        try:
            if blocked:
                raise RuntimeError("blocked by gate(s): " + "; ".join(f"{g['kind']}: {g['detail']}" for g in blocked))
            if dry:
                self._dry_run(target, plan)
            else:
                self._execute(target, prev, options, plan)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator, never swallowed
            with self._lock:
                if self.transition:
                    self.transition["state"] = FAILED
                    self.transition["error"] = f"{exc}"
            self._emit("transition.error", {"error": str(exc), "trace": traceback.format_exc()[-1500:]})
        finally:
            elapsed = time.time() - started
            with self._lock:
                if not self.transition:
                    return
                t = self.transition
                if t["state"] == RUNNING:  # fell out without setting a terminal state
                    t["state"] = DONE
                if t["state"] == DONE:
                    self.current = {"runstate": target.id, "since": time.time(), "source": "transition"}
                    t["progress"] = 100.0
                t["eta_s"] = 0.0
                t["ended_at"] = time.time()
                t["duration_s"] = round(elapsed, 1)
                t["steps"] = [r.as_dict() for r in plan["records"]]
                self.history.append({k: t[k] for k in
                                     ("id", "state", "from", "to", "started_at", "ended_at",
                                      "duration_s", "progress", "error", "dry_run")})
                summary = {"state": t["state"], "to": target.id, "duration_s": round(elapsed, 1),
                           "progress": t["progress"]}
                self._persist()
            self._emit("transition.end", summary)

    def _execute(self, target: RunState, prev: RunState | None, options: dict, plan: dict) -> None:
        recs: list[StepRecord] = plan["records"]
        total_w = sum(r.weight for r in recs) or 1.0
        done_w = 0.0
        step_lookup: dict[tuple[str, str], Step] = {}
        if prev is not None:
            for s in prev.on_leave:
                step_lookup[("on_leave", s.id)] = s
        for s in target.steps:
            step_lookup[("apply", s.id)] = s

        for idx, rec in enumerate(recs):
            if self._cancel.is_set():
                rec.status = "aborted"
                for rest in recs[idx + 1:]:
                    rest.status = "aborted"
                with self._lock:
                    if self.transition:
                        self.transition["state"] = ABORTED
                        self.transition["error"] = f"aborted before step '{rec.label}'"
                        self.transition["progress"] = round(100.0 * done_w / total_w, 1)
                self._sync_steps(recs)
                self._emit("transition.aborted", {"at": rec.id, "progress": round(100.0 * done_w / total_w, 1)})
                return

            rec.status = "running"
            rec.started_at = time.time()
            self._emit("step.start", {"id": rec.id, "label": rec.label, "phase": rec.phase})
            if rec.action == "internal.userland":
                res = self._restart_userland(target)
            else:
                spec = step_lookup.get((rec.phase, rec.id))
                if spec is None:
                    res = {"ok": False, "detail": "step spec missing from manifest"}
                else:
                    res = self._run_step(spec)
            rec.ended_at = time.time()
            rec.detail = str(res.get("detail", ""))
            rec.verify_ok = res.get("verify_ok")
            rec.verify_detail = str(res.get("verify_detail", ""))
            rec.status = "ok" if res.get("ok") else "failed"
            if res.get("skipped"):
                rec.status = "skipped"
            done_w += rec.weight
            self._sync_steps(recs)
            self._update_progress(done_w, total_w, plan["est_s"], started_at=self.transition["started_at"])
            self._emit("step.end", {"id": rec.id, "status": rec.status, "detail": rec.detail,
                                    "verify": rec.verify_detail})
            if rec.status == "failed":
                spec = step_lookup.get((rec.phase, rec.id))
                critical = spec.critical if spec is not None else True
                if critical:
                    with self._lock:
                        if self.transition:
                            self.transition["state"] = FAILED
                            self.transition["error"] = f"step '{rec.label}' failed: {rec.detail}"
                    return
        with self._lock:
            if self.transition:
                self.transition["state"] = DONE

    def _run_step(self, spec: Step) -> dict[str, Any]:
        fn = steps_mod.EXECUTORS.get(spec.action)
        if fn is None:
            return {"ok": False, "detail": f"no executor for action '{spec.action}'"}
        ctx = {"default_timeout_s": self.cfg.engine.get("default_step_timeout_s", 120),
               "cancel": self._cancel, "config": self.cfg}
        if spec.elevated and not steps_mod.is_admin():
            return {"ok": False, "detail": "elevation required (run the worker elevated)"}
        try:
            res = fn(spec.params, ctx)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"executor raised: {exc}"}
        if not res.get("ok"):
            return res
        v = steps_mod.verify(spec.verify, spec)
        res["verify_ok"] = bool(v.get("ok"))
        res["verify_detail"] = str(v.get("detail", ""))
        if not v.get("ok"):
            res["ok"] = False
            res["detail"] = f"{res.get('detail', '')} / read-back mismatch: {v.get('detail', '')}"
        return res

    def _restart_userland(self, target: RunState) -> dict[str, Any]:
        keep = {str(k).lower() for k in (target.userland.get("keep") or [])}
        keep |= USERLAND_PROTECTED
        me = os_getpid()
        my_session = _session_id(me)
        victims = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                name = (p.info.get("name") or "").lower()
                if p.pid == me or name in keep:
                    continue
                if _session_id(p.pid) != my_session:
                    continue
                victims.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        grace = float(target.userland.get("grace_s", 8))
        for p in victims:
            try:
                p.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(victims, timeout=grace)
        return {"ok": True, "detail": f"restarted userland: terminated {len(victims)} process(es), shell kept"}

    def _dry_run(self, target: RunState, plan: dict) -> None:
        for rec in plan["records"]:
            rec.status = "skipped"
            rec.detail = "dry-run: not executed"
            rec.started_at = rec.ended_at = time.time()
        with self._lock:
            if self.transition:
                self.transition["progress"] = 100.0
        self._sync_steps(plan["records"])

    def _sync_steps(self, recs: list[StepRecord]) -> None:
        with self._lock:
            if self.transition:
                self.transition["steps"] = [r.as_dict() for r in recs]

    def _update_progress(self, done_w: float, total_w: float, est_s: float, started_at: float) -> None:
        elapsed = max(0.001, time.time() - started_at)
        frac = min(1.0, done_w / total_w)
        expected_frac = min(1.0, elapsed / max(1.0, est_s))
        speed = max(0.25, min(4.0, frac / expected_frac if expected_frac > 0.01 else 1.0))
        remaining = max(0.0, (est_s - elapsed) / speed)
        with self._lock:
            if self.transition:
                self.transition["progress"] = round(100.0 * frac, 1)
                self.transition["eta_s"] = round(remaining, 1)

    def _emit(self, kind: str, data: dict) -> None:
        try:
            self.on_event(kind, data)
        except Exception:  # noqa: BLE001 - event sinks must never break a transition
            pass


def os_getpid() -> int:
    import os
    return os.getpid()


def _session_id(pid: int) -> int | None:
    return _win_session(pid)


def _win_session(pid: int) -> int | None:
    try:
        import ctypes
        from ctypes import wintypes
        sid = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)):
            return int(sid.value)
    except (AttributeError, OSError):
        return None
    return None
