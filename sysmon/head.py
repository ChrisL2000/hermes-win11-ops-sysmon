"""The *head* -- what a supervisory node reads to find and connect to us.

A head is a small, stable JSON record (``sysmon.head/v1``) carrying identity,
where the API lives (loopback + tailnet), a token *fingerprint* (never the
token), the current run-state and the links to the shared dashboards. It is
written to:

* ``<data_dir>/head/<agent-slug>.json`` -- always, atomically;
* ``\\\\us-hm-proxmox01\\bots\\heads\\<agent-slug>.json`` -- when a share is
  configured (best effort; a share outage must never take the worker down);
* POSTed to ``head.supervisor_url`` -- when a supervisor is configured.

The share copy is the rendezvous: the supervisor scans that directory, picks a
head, dials the endpoint, and presents the token it already holds.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from . import AGENT_KIND, __version__

SCHEMA = "sysmon.head/v1"


def slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in text).strip("-").lower()


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _tailnet_ip() -> str:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=6)
        if out.returncode == 0:
            return out.stdout.strip().splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return ""


def _os_state() -> dict[str, Any]:
    info: dict[str, Any] = {"platform": "windows"}
    try:
        info["hostname"] = socket.gethostname()
        info["build"] = f"{psutil.os.uname().version}" if hasattr(psutil, "os") else ""
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "$o=Get-CimInstance Win32_OperatingSystem; \"$($o.Caption)|$($o.BuildNumber)\""],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if out.returncode == 0 and "|" in out.stdout:
            cap, build = out.stdout.strip().split("|", 1)
            info.update({"caption": cap.strip(), "build": build.strip(),
                         "version": f"10.0.{build.strip()}"})
    except (OSError, subprocess.SubprocessError):
        pass
    return info


class HeadPublisher:
    def __init__(self, cfg, engine, registry, logger=None):
        self.cfg = cfg
        self.engine = engine
        self.registry = registry
        self.logger = logger
        self.started_at = time.time()
        self.slug = slug(cfg.agent["name"])
        self.local_dir = Path(cfg.head.get("local_dir") or (cfg.data_dir / "head"))
        self.share_dir = cfg.head.get("share_dir") or ""
        self.interval = float(cfg.head.get("heartbeat_s", 30))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_error = ""
        self.last_written_at: float | None = None
        self.tailnet_ip = ""
        self.os_state: dict[str, Any] = {}

    # ------------------------------------------------------------------
    def endpoint_urls(self) -> list[dict[str, str]]:
        port = int(self.cfg.server.get("port", 8765))
        urls = [{"url": f"http://127.0.0.1:{port}", "scope": "loopback"}]
        hosts = [h for h in (self.cfg.server.get("bind_extra_hosts") or []) if h]
        for h in hosts:
            urls.append({"url": f"http://{h}:{port}", "scope": "tailnet" if h.startswith("100.") else "lan"})
        return urls

    def record(self) -> dict[str, Any]:
        st = self.engine.status()
        cur = st["current"]
        tr = st["transition"]
        manifest = self.registry.get(cur.get("runstate") or "")
        rec = {
            "schema": SCHEMA,
            "kind": AGENT_KIND,
            "version": __version__,
            "agent": {
                "id": self.cfg.agent["id"],
                "name": self.cfg.agent["name"],
                "slug": self.slug,
                "role": self.cfg.agent.get("role", "worker"),
                "labels": self.cfg.agent.get("labels", {}),
            },
            "host": {
                "hostname": socket.gethostname(),
                "os": self.os_state,
                "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 2),
                "tailnet_ip": self.tailnet_ip or _tailnet_ip(),
            },
            "api": {
                "endpoints": self.endpoint_urls(),
                "token_fingerprint": token_fingerprint(self.cfg.token),
                "auth": "bearer",
            },
            "state": {
                "runstate": cur.get("runstate"),
                "runstate_label": manifest.label if manifest else None,
                "since": cur.get("since"),
                "transition": None if not tr else {
                    "id": tr["id"], "from": tr["from"], "to": tr["to"],
                    "progress": tr["progress"], "state": tr["state"],
                },
            },
            "links": self.cfg.links,
            "heartbeat_at": time.time(),
            "heartbeat_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "started_at": self.started_at,
            "pid": os.getpid(),
        }
        return rec

    # ------------------------------------------------------------------
    def publish(self, reason: str = "heartbeat") -> dict[str, Any]:
        rec = self.record()
        payload = json.dumps(rec, indent=2)
        with self._lock:
            self.local_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.local_dir / f"{self.slug}.json", payload)
            self.last_written_at = time.time()
            errors: list[str] = []
            if self.share_dir:
                try:
                    share = Path(self.share_dir)
                    share.mkdir(parents=True, exist_ok=True)
                    _atomic_write(share / f"{self.slug}.json", payload)
                except OSError as exc:
                    errors.append(f"share: {exc}")
            supervisor = self.cfg.head.get("supervisor_url") or ""
            if supervisor:
                try:
                    import urllib.request
                    req = urllib.request.Request(
                        supervisor.rstrip("/") + "/api/head",
                        data=payload.encode("utf-8"),
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {self.cfg.head.get('supervisor_token', '')}"},
                        method="POST")
                    urllib.request.urlopen(req, timeout=8).read(2048)
                except Exception as exc:  # noqa: BLE001 - network is optional
                    errors.append(f"supervisor: {exc}")
            self.last_error = "; ".join(errors)
        if self.logger:
            self.logger.log("info", "head.publish", f"head published ({reason})",
                            {"errors": self.last_error, "share": bool(self.share_dir)})
        return {"ok": not self.last_error, "detail": self.last_error or "published", "path": str(self.local_dir)}

    # ------------------------------------------------------------------
    def start(self) -> None:
        self.os_state = _os_state()
        self.publish("startup")
        self._thread = threading.Thread(target=self._run, name="sysmon-head", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.publish("heartbeat")

    def status(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "local_dir": str(self.local_dir),
            "share_dir": self.share_dir,
            "heartbeat_s": self.interval,
            "last_written_at": self.last_written_at,
            "last_error": self.last_error,
        }


def _atomic_write(path: Path, payload: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
