"""Step executors + read-back verifiers.

Every executor returns ``{"ok": bool, "detail": str, "data": {...}}`` and is
followed by a read-back verify -- the engine never trusts a return code alone
(windows-host-ops rule). Executors are deliberately dumb: one Windows primitive
per action, so a manifest stays auditable.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable

import psutil

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


# ---------------------------------------------------------------- helpers
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def run(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except (subprocess.SubprocessError, OSError) as exc:
        return 1, str(exc)


def _result(ok: bool, detail: str, **data: Any) -> dict[str, Any]:
    return {"ok": ok, "detail": detail, "data": data}


def _live_names() -> dict[str, list[int]]:
    names: dict[str, list[int]] = {}
    for p in psutil.process_iter(["name", "pid"]):
        try:
            n = (p.info.get("name") or "").lower()
            names.setdefault(n, []).append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return names


def power_schemes() -> dict[str, str]:
    """friendly-name (lower) -> GUID."""
    rc, out = run(["powercfg", "/list"])
    schemes: dict[str, str] = {}
    if rc != 0:
        return schemes
    for line in out.splitlines():
        m = re.search(r"Power Scheme GUID:\s*([0-9a-fA-F-]{36})\s*\(([^)]*)\)", line)
        if m:
            schemes[m.group(2).strip().lower()] = m.group(1).lower()
    return schemes


def active_power_scheme() -> tuple[str, str]:
    rc, out = run(["powercfg", "/getactivescheme"])
    if rc != 0:
        return "", ""
    m = re.search(r"([0-9a-fA-F-]{36})\s*\(([^)]*)\)", out)
    return (m.group(1).lower(), m.group(2).strip()) if m else ("", "")


def resolve_power_plan(token: str) -> str | None:
    token_l = (token or "").strip().lower()
    schemes = power_schemes()
    if token_l in schemes:
        return schemes[token_l]
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", token_l):
        return token_l
    for name, guid in schemes.items():
        if token_l and token_l in name:
            return guid
    return None


# -------------------------------------------------------------- executors
def act_power_plan(params: dict, ctx: dict) -> dict:
    plan = str(params.get("plan", "High performance"))
    guid = resolve_power_plan(plan)
    if not guid:
        return _result(False, f"power plan '{plan}' not found (have: {', '.join(power_schemes())})")
    if not is_admin():
        return _result(False, "elevation required to change the active power plan")
    rc, out = run(["powercfg", "/setactive", guid])
    if rc != 0:
        return _result(False, f"powercfg /setactive failed: {out}")
    return _result(True, f"activated '{plan}' ({guid})", plan=plan, guid=guid)


def act_power_setting(params: dict, ctx: dict) -> dict:
    subgroup = str(params.get("subgroup", ""))
    setting = str(params.get("setting", ""))
    if not subgroup or not setting:
        return _result(False, "power.setting needs group/subgroup GUIDs")
    ac = params.get("ac_value")
    dc = params.get("dc_value", ac)
    cmds = []
    if ac is not None:
        cmds.append(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", subgroup, setting, str(int(ac))])
    if dc is not None:
        cmds.append(["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", subgroup, setting, str(int(dc))])
    if not cmds:
        return _result(False, "power.setting needs ac_value and/or dc_value")
    for c in cmds:
        rc, out = run(c)
        if rc != 0:
            return _result(False, f"{' '.join(c[1:3])} failed: {out}")
    rc, out = run(["powercfg", "/setactive", "SCHEME_CURRENT"])
    return _result(rc == 0, "power setting applied" if rc == 0 else out)


def act_process_start(params: dict, ctx: dict) -> dict:
    exe = os.path.expandvars(str(params.get("path", "")))
    args = params.get("args") or []
    cwd = os.path.expandvars(str(params.get("cwd") or os.path.dirname(exe) or "."))
    if not exe or not os.path.exists(exe):
        return _result(False, f"executable not found: {exe}")
    if isinstance(args, str):
        args = [args]
    names = [str(n).lower() for n in (params.get("names") or [os.path.basename(exe).lower()])]
    live = _live_names()
    if params.get("singular", True) and any(n in live for n in names):
        return _result(True, f"already running: {', '.join(n for n in names if n in live)}", skipped=True)
    try:
        subprocess.Popen([exe, *map(str, args)], cwd=cwd,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
    except OSError as exc:
        return _result(False, f"spawn failed: {exc}")
    deadline = time.time() + float(params.get("wait_s", 12))
    while time.time() < deadline:
        time.sleep(0.5)
        if any(n in _live_names() for n in names):
            return _result(True, f"started {os.path.basename(exe)}")
    return _result(False, f"spawned but {', '.join(names)} not visible after {params.get('wait_s', 12)}s")


def act_process_stop(params: dict, ctx: dict) -> dict:
    names = [str(n).lower() for n in (params.get("names") or ([params["name"]] if params.get("name") else []))]
    if not names:
        return _result(False, "process.stop needs 'names'")
    keep = {str(k).lower() for k in (params.get("keep") or [])}
    names = [n for n in names if n not in keep]
    grace = float(params.get("grace_s", 8))
    include_children = bool(params.get("children", True))
    targets: list[psutil.Process] = []
    for p in psutil.process_iter(["name", "pid"]):
        try:
            if (p.info.get("name") or "").lower() in names:
                targets.append(p)
                if include_children:
                    targets.extend(p.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not targets:
        return _result(True, "nothing to stop", skipped=True)
    victims = {p.pid: p for p in targets if p.pid != os.getpid()}
    for p in victims.values():
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(list(victims.values()), timeout=grace)
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.4)
    still = [p.info["name"].lower() for p in psutil.process_iter(["name"])
             if (p.info.get("name") or "").lower() in names]
    if still:
        return _result(False, f"still running after stop: {', '.join(sorted(set(still)))}")
    return _result(True, f"stopped {len(victims)} process(es): {', '.join(sorted(set(n for n in names)))}")


def act_process_ensure_running(params: dict, ctx: dict) -> dict:
    names = [str(n).lower() for n in (params.get("names") or [])]
    live = _live_names()
    missing = [n for n in names if n not in live]
    if not names:
        return _result(False, "process.ensure_running needs 'names'")
    if missing:
        return _result(False, "not running: " + ", ".join(missing), missing=missing)
    return _result(True, "running: " + ", ".join(names))


def act_process_ensure_stopped(params: dict, ctx: dict) -> dict:
    names = [str(n).lower() for n in (params.get("names") or [])]
    live = _live_names()
    present = [n for n in names if n in live]
    if present:
        return _result(False, "still running: " + ", ".join(present), present=present)
    return _result(True, "not running: " + ", ".join(names) or "none")


def act_service_set(params: dict, ctx: dict) -> dict:
    name = str(params.get("name", ""))
    if not name:
        return _result(False, "service.set needs 'name'")
    startup = params.get("startup")  # auto | demand | disabled | delayed-auto
    state = str(params.get("state", "")).lower()  # running | stopped | ""
    if not is_admin():
        return _result(False, "elevation required to configure services")
    if startup:
        arg = {"delayed-auto": "delayed-auto"}.get(str(startup).lower(), str(startup).lower())
        rc, out = run(["sc.exe", "config", name, "start=", arg])
        if rc != 0:
            return _result(False, f"sc config failed: {out}")
    if state == "stopped":
        rc, out = run(["sc.exe", "stop", name])
        if rc not in (0, 1062):  # 1062 = not started
            return _result(False, f"sc stop failed: {out}")
    elif state == "running":
        rc, out = run(["sc.exe", "start", name])
        if rc not in (0, 1056):  # 1056 = already running
            return _result(False, f"sc start failed: {out}")
    return _result(True, f"service '{name}' set (startup={startup or '-'}, state={state or '-'})")


def act_registry_set(params: dict, ctx: dict) -> dict:
    hive = str(params.get("hive", "HKCU")).upper()
    key = str(params.get("key", ""))
    name = params.get("name")
    value = params.get("value")
    vtype = str(params.get("type", "REG_SZ")).upper()
    if not key:
        return _result(False, "registry.set needs 'key'")
    args = ["reg", "add", rf"{hive}\{key}", "/f"]
    if name:
        args += ["/v", str(name)]
    else:
        args += ["/ve"]
    args += ["/t", vtype, "/d", str(value)]
    rc, out = run(args)
    return _result(rc == 0, out if rc else f"set {hive}\\{key}\\{name or '(default)'} = {value}")


def act_env_set(params: dict, ctx: dict) -> dict:
    name = str(params.get("name", ""))
    value = str(params.get("value", ""))
    scope = str(params.get("scope", "user")).lower()
    if not name:
        return _result(False, "env.set needs 'name'")
    flag = "/M" if scope == "machine" else ""
    args = ["setx", name, value] + ([flag] if flag else [])
    if scope == "machine" and not is_admin():
        return _result(False, "elevation required for machine-scope env var")
    rc, out = run(args)
    return _result(rc == 0, out if rc else f"set {scope} env {name}={value}")


def act_script_run(params: dict, ctx: dict) -> dict:
    path = os.path.expandvars(str(params.get("path", "")))
    if not path or not os.path.exists(path):
        return _result(False, f"script not found: {path}")
    args = params.get("args") or []
    timeout = float(params.get("timeout_s", ctx.get("default_timeout_s", 120)))
    shell = str(params.get("shell", "powershell")).lower()
    if shell == "powershell":
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    else:
        cmd = ["cmd.exe", "/c", path]
    cmd += [str(a) for a in args]
    rc, out = run(cmd, timeout=timeout)
    return _result(rc == 0, f"exit {rc}: " + (out[-600:] if out else "(no output)"))


def act_cmd_run(params: dict, ctx: dict) -> dict:
    command = str(params.get("command", ""))
    if not command:
        return _result(False, "cmd.run needs 'command'")
    timeout = float(params.get("timeout_s", ctx.get("default_timeout_s", 120)))
    rc, out = run(["cmd.exe", "/c", command], timeout=timeout)
    expect = params.get("expect_regex")
    if expect and not re.search(str(expect), out, re.MULTILINE):
        return _result(False, f"output did not match /{expect}/: {out[-400:]}")
    return _result(rc == 0, f"exit {rc}: " + (out[-600:] if out else "(no output)"))


def act_noop(params: dict, ctx: dict) -> dict:
    return _result(True, str(params.get("note", "no-op")))


EXECUTORS: dict[str, Callable[[dict, dict], dict]] = {
    "power.plan": act_power_plan,
    "power.setting": act_power_setting,
    "process.start": act_process_start,
    "process.stop": act_process_stop,
    "process.ensure_running": act_process_ensure_running,
    "process.ensure_stopped": act_process_ensure_stopped,
    "service.set": act_service_set,
    "registry.set": act_registry_set,
    "env.set": act_env_set,
    "script.run": act_script_run,
    "cmd.run": act_cmd_run,
    "noop": act_noop,
}


# -------------------------------------------------------------- verifiers
def verify(spec: dict | None, step: Any) -> dict[str, Any]:
    """Read-back verification. Falls back to the step's own action semantics."""
    if not spec:
        return _verify_default(step)
    kind = str(spec.get("kind", ""))
    if kind == "process_running":
        names = [str(n).lower() for n in spec.get("names", [])]
        live = _live_names()
        missing = [n for n in names if n not in live]
        return {"ok": not missing, "detail": f"running: {', '.join(names) if not missing else 'missing ' + ', '.join(missing)}"}
    if kind == "process_stopped":
        names = [str(n).lower() for n in spec.get("names", [])]
        live = _live_names()
        present = [n for n in names if n in live]
        return {"ok": not present, "detail": "stopped" if not present else "still running: " + ", ".join(present)}
    if kind == "service_state":
        name = str(spec.get("name", ""))
        want = str(spec.get("state", "running")).lower()
        rc, out = run(["sc.exe", "query", name])
        cur = "running" if "RUNNING" in out.upper() else ("stopped" if "STOPPED" in out.upper() else "unknown")
        return {"ok": rc == 0 and cur == want, "detail": f"{name} = {cur} (want {want})"}
    if kind == "power_plan":
        want = str(spec.get("plan", ""))
        guid_want = resolve_power_plan(want) or want.lower()
        guid_now, name_now = active_power_scheme()
        return {"ok": guid_now == guid_want, "detail": f"active plan = {name_now} ({guid_now})"}
    if kind == "registry_value":
        hive = str(spec.get("hive", "HKCU")).upper()
        key = str(spec.get("key", ""))
        name = spec.get("name")
        want = str(spec.get("value", ""))
        args = ["reg", "query", rf"{hive}\{key}"] + (["/v", str(name)] if name else ["/ve"])
        rc, out = run(args)
        got = ""
        for line in out.splitlines():
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) >= 3 and (not name or parts[0].lower() == str(name).lower()):
                got = parts[-1]
        return {"ok": rc == 0 and got.strip().lower() == want.strip().lower(),
                "detail": f"registry read-back = {got or '(missing)'} (want {want})"}
    if kind == "power_setting":
        subgroup = str(spec.get("subgroup", ""))
        setting = str(spec.get("setting", ""))
        want = spec.get("ac_value")
        rc, out = run(["powercfg", "/query", "SCHEME_CURRENT", subgroup, setting])
        got = None
        for line in out.splitlines():
            if "Current AC Power Setting Index" in line and ":" in line:
                raw = line.split(":", 1)[1].strip()
                try:
                    got = int(raw, 16) if raw.lower().startswith("0x") else int(raw)
                except ValueError:
                    got = None
        ok = rc == 0 and want is not None and got == int(want)
        return {"ok": ok, "detail": f"AC index = {got} (want {want})"}
    if kind == "cmd":
        rc, out = run(["cmd.exe", "/c", str(spec.get("command", ""))], timeout=float(spec.get("timeout_s", 30)))
        pattern = spec.get("expect_regex")
        ok = rc == 0 and (not pattern or bool(re.search(str(pattern), out, re.MULTILINE)))
        return {"ok": ok, "detail": (out[-300:] or f"exit {rc}")}
    return _verify_default(step)


def _verify_default(step: Any) -> dict[str, Any]:
    action = getattr(step, "action", "")
    p = getattr(step, "params", {}) or {}
    if action == "power.plan":
        return verify({"kind": "power_plan", "plan": p.get("plan", "")}, step)
    if action in ("process.stop", "process.ensure_stopped"):
        return verify({"kind": "process_stopped",
                       "names": p.get("names") or ([p["name"]] if p.get("name") else [])}, step)
    if action in ("process.start", "process.ensure_running"):
        names = p.get("names") or ([os.path.basename(str(p.get("path", ""))).lower()] if p.get("path") else [])
        return verify({"kind": "process_running", "names": names}, step)
    if action == "service.set" and p.get("state"):
        return verify({"kind": "service_state", "name": p.get("name"), "state": p.get("state")}, step)
    if action == "power.setting" and p.get("ac_value") is not None:
        return verify({"kind": "power_setting", "subgroup": p.get("subgroup", ""),
                       "setting": p.get("setting", ""), "ac_value": p.get("ac_value")}, step)
    return {"ok": True, "detail": "no read-back defined (executor result only)"}
