"""Real-time host sampler.

One thread, one ring buffer. CPU/mem/disk/net every tick; GPU (nvidia-smi) and
the process table on a slower cadence because both are comparatively expensive.
Everything is best-effort: a missing GPU or denied counter degrades the sample,
it never kills the loop.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any

import psutil

NVIDIA_SMI = shutil.which("nvidia-smi") or r"C:\WINDOWS\system32\nvidia-smi.exe"


@dataclass
class Sample:
    t: float                     # epoch seconds
    cpu: float = 0.0             # percent, all cores
    mem: float = 0.0             # percent
    mem_used_gb: float = 0.0
    disk_read_mbs: float = 0.0
    disk_write_mbs: float = 0.0
    net_rx_mbs: float = 0.0
    net_tx_mbs: float = 0.0
    gpu: float | None = None     # percent
    gpu_mem_mb: float | None = None
    gpu_temp_c: float | None = None
    gpu_power_w: float | None = None
    procs: int = 0
    threads: int = 0
    top: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gpu_sample() -> dict[str, float | None]:
    if not NVIDIA_SMI:
        return {}
    try:
        out = subprocess.run(
            [NVIDIA_SMI, "--query-gpu=utilization.gpu,memory.used,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0 or not out.stdout.strip():
            return {}
        parts = [p.strip() for p in out.stdout.splitlines()[0].split(",")]
        vals: list[float | None] = []
        for p in parts:
            try:
                vals.append(float(p))
            except ValueError:
                vals.append(None)
        while len(vals) < 4:
            vals.append(None)
        return {"gpu": vals[0], "gpu_mem_mb": vals[1], "gpu_temp_c": vals[2], "gpu_power_w": vals[3]}
    except (subprocess.SubprocessError, OSError, IndexError, ValueError):
        return {}


def _top_processes(n: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = p.info
            mem = info.get("memory_info")
            rows.append({
                "pid": info["pid"],
                "name": info["name"] or "?",
                "mem_mb": round((mem.rss if mem else 0) / 1_048_576, 1),
                "cpu": round(info.get("cpu_percent") or 0.0, 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: (r["mem_mb"], r["cpu"]), reverse=True)
    return rows[:n]


class Sampler:
    def __init__(self, interval_s: float = 1.0, buffer: int = 3600,
                 gpu_interval_s: float = 2.0, proc_interval_s: float = 5.0,
                 top_n: int = 12):
        self.interval_s = max(0.2, float(interval_s))
        self.gpu_interval_s = max(self.interval_s, float(gpu_interval_s))
        self.proc_interval_s = max(self.interval_s, float(proc_interval_s))
        self.top_n = top_n
        self.samples: deque[Sample] = deque(maxlen=max(60, int(buffer)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._gpu: dict[str, float | None] = {}
        self._top: list[dict[str, Any]] = []
        self._procs = 0
        self._threads = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        psutil.cpu_percent(interval=None)  # prime the first delta
        self._thread = threading.Thread(target=self._run, name="sysmon-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- loop ------------------------------------------------------------
    def _run(self) -> None:
        last_disk = psutil.disk_io_counters()
        last_net = psutil.net_io_counters()
        last_t = time.time()
        t_gpu = t_proc = 0.0
        while not self._stop.wait(self.interval_s):
            now = time.time()
            dt = max(1e-3, now - last_t)
            last_t = now
            s = Sample(t=now)
            try:
                s.cpu = psutil.cpu_percent(interval=None)
                vm = psutil.virtual_memory()
                s.mem = vm.percent
                s.mem_used_gb = round((vm.total - vm.available) / 1_073_741_824, 2)
                disk = psutil.disk_io_counters()
                if disk and last_disk:
                    s.disk_read_mbs = round((disk.read_bytes - last_disk.read_bytes) / dt / 1_048_576, 2)
                    s.disk_write_mbs = round((disk.write_bytes - last_disk.write_bytes) / dt / 1_048_576, 2)
                last_disk = disk or last_disk
                net = psutil.net_io_counters()
                if net and last_net:
                    s.net_rx_mbs = round((net.bytes_recv - last_net.bytes_recv) / dt / 1_048_576, 3)
                    s.net_tx_mbs = round((net.bytes_sent - last_net.bytes_sent) / dt / 1_048_576, 3)
                last_net = net or last_net
            except (psutil.Error, OSError):
                pass

            if now - t_gpu >= self.gpu_interval_s:
                self._gpu = _gpu_sample()
                t_gpu = now
            if now - t_proc >= self.proc_interval_s:
                try:
                    self._top = _top_processes(self.top_n)
                    self._procs = len(psutil.pids())
                    self._threads = sum(p.num_threads() for p in psutil.process_iter(["num_threads"])
                                        if p.info.get("num_threads"))
                except (psutil.Error, OSError):
                    pass
                t_proc = now

            for k, v in self._gpu.items():
                setattr(s, k, v)
            s.procs = self._procs
            s.threads = self._threads
            s.top = list(self._top)
            with self._lock:
                self.samples.append(s)

    # -- read side -------------------------------------------------------
    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return self.samples[-1].as_dict() if self.samples else None

    def window(self, seconds: float = 120.0) -> list[dict[str, Any]]:
        cutoff = time.time() - seconds
        with self._lock:
            return [s.as_dict() for s in self.samples if s.t >= cutoff]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            if not self.samples:
                return {}
            recent = list(self.samples)[-60:]
        cpu = [s.cpu for s in recent]
        mem = [s.mem for s in recent]
        gpu = [s.gpu for s in recent if s.gpu is not None]
        return {
            "cpu_avg_60s": round(sum(cpu) / len(cpu), 1),
            "cpu_max_60s": round(max(cpu), 1),
            "mem_avg_60s": round(sum(mem) / len(mem), 1),
            "gpu_avg_60s": round(sum(gpu) / len(gpu), 1) if gpu else None,
            "samples": len(self.samples),
        }
