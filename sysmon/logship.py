"""Log shipping: local JSONL -> optional SMB mirror -> optional Logstash HTTP.

Records are ECS-flavoured enough for the ELK stack on ``robertdobbs``:
``@timestamp``, ``event.dataset``, ``host.name``, ``agent.id``, ``log.level``,
``event.kind`` and a free-form ``sysmon`` object. Local file is the source of
truth; both ships are best-effort and never block the caller.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


class LogShip:
    def __init__(self, cfg, agent_id: str):
        self.cfg = cfg
        self.agent_id = agent_id
        self.dir = Path(cfg.data_dir) / "logs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(float(cfg.logs.get("rotate_mb", 16)) * 1_048_576)
        self.keep_days = int(cfg.logs.get("keep_days", 14))
        ship = cfg.logs.get("ship") or {}
        self.share_dir = ship.get("share_dir") or ""
        self.http_url = ship.get("http_url") or ""
        self.http_index = ship.get("http_index", "sysmon")
        self.batch = int(ship.get("batch", 200))
        self.hostname = os.environ.get("COMPUTERNAME", "unknown")

        self.recent: deque[dict[str, Any]] = deque(maxlen=1000)
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=20000)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sysmon-logship", daemon=True)
        self._thread.start()
        self.shipped = 0
        self.share_failures = 0
        self.http_failures = 0
        self.last_error = ""

    # ------------------------------------------------------------------
    def log(self, level: str, kind: str, message: str, data: dict | None = None) -> None:
        rec = {
            "@timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time()*1000)%1000:03d}Z",
            "event": {"dataset": "sysmon", "kind": kind},
            "log": {"level": level},
            "message": message,
            "host": {"name": self.hostname},
            "agent": {"id": self.agent_id, "type": "hermes-win11-ops:sysmon"},
            "sysmon": data or {},
        }
        try:
            self._q.put_nowait(rec)
        except queue.Full:
            pass

    def tail(self, n: int = 200) -> list[dict[str, Any]]:
        return list(self.recent)[-n:]

    def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    def _current_file(self) -> Path:
        return self.dir / f"sysmon-{time.strftime('%Y%m%d')}.jsonl"

    def _run(self) -> None:
        pending_http: list[dict[str, Any]] = []
        last_share_try = 0.0
        while not self._stop.is_set():
            batch: list[dict[str, Any]] = []
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item is None and not batch:
                pass
            else:
                if item is not None:
                    batch.append(item)
                while len(batch) < self.batch:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        break
                    batch.append(nxt)
            if batch:
                self.recent.extend(batch)
                self._write_local(batch)
                if self.share_dir and time.time() - last_share_try > 5:
                    last_share_try = time.time()
                    self._append_share(batch)
                if self.http_url:
                    pending_http.extend(batch)
                    if len(pending_http) >= self.batch or not batch:
                        self._post_http(pending_http)
                        pending_http = pending_http[self.batch:]
                        if len(pending_http) > 5000:
                            pending_http = pending_http[-5000:]
            if self._stop.is_set():
                break
        if pending_http and self.http_url:
            self._post_http(pending_http)

    def _write_local(self, batch: list[dict]) -> None:
        path = self._current_file()
        try:
            with path.open("a", encoding="utf-8") as fh:
                for rec in batch:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            if path.stat().st_size > self.max_bytes:
                path.rename(path.with_suffix(f".{int(time.time())}.jsonl"))
            self._prune()
        except OSError as exc:
            self.last_error = f"local write: {exc}"

    def _append_share(self, batch: list[dict]) -> None:
        try:
            share = Path(os.path.expandvars(self.share_dir))
            share.mkdir(parents=True, exist_ok=True)
            f = share / f"sysmon-{self.hostname.lower()}-{time.strftime('%Y%m%d')}.jsonl"
            with f.open("a", encoding="utf-8") as fh:
                for rec in batch:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            self.share_failures = 0
        except OSError as exc:
            self.share_failures += 1
            self.last_error = f"share: {exc}"

    def _post_http(self, batch: list[dict]) -> None:
        if not batch:
            return
        try:
            import urllib.request
            payload = "\n".join(json.dumps(r, separators=(",", ":")) for r in batch).encode("utf-8")
            req = urllib.request.Request(self.http_url, data=payload,
                                         headers={"Content-Type": "application/x-ndjson"}, method="POST")
            urllib.request.urlopen(req, timeout=10).read(2048)
            self.shipped += len(batch)
            self.http_failures = 0
        except Exception as exc:  # noqa: BLE001 - ELK may be down; keep local truth
            self.http_failures += 1
            self.last_error = f"http ship: {exc}"

    def _prune(self) -> None:
        cutoff = time.time() - self.keep_days * 86400
        for f in self.dir.glob("sysmon-*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue

    def status(self) -> dict[str, Any]:
        return {
            "dir": str(self.dir),
            "share_dir": self.share_dir,
            "http_url": self.http_url,
            "queued": self._q.qsize(),
            "shipped_http": self.shipped,
            "share_failures": self.share_failures,
            "http_failures": self.http_failures,
            "last_error": self.last_error,
        }
