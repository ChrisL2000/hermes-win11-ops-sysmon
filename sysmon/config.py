"""Configuration: defaults + JSON overlay + runtime paths.

The config file lives outside the repo (``%LOCALAPPDATA%\\hermes-sysmon\\config.json``)
so a git checkout never carries a host's secrets or paths. Run-states themselves
live in the repo (``runstates/*.json``) and are versionable.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

AGENT_KIND = "hermes-win11-ops:sysmon"

DEFAULTS: dict[str, Any] = {
    "agent": {
        "name": None,                 # filled in from hostname
        "id": None,                   # stable uuid, generated once
        "role": "worker",             # worker | supervisor
        "labels": {},
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        "bind_extra_hosts": [],                   # e.g. ["100.x.y.z"] tailnet IP
        "require_token_off_loopback": True,
        "cors_origins": [],
    },
    "paths": {
        "data_dir": "%LOCALAPPDATA%/hermes-sysmon",
        "runstates_dir": None,        # resolved to <repo>/runstates
    },
    "sampler": {"interval_s": 1.0, "buffer": 3600, "top_processes": 12},
    "head": {
        # The head is the record a supervisory node polls to find + connect to
        # this worker. Local copy always written; share copy is best-effort.
        "local_dir": "%LOCALAPPDATA%/hermes-sysmon/head",
        "share_dir": "",              # e.g. \\us-hm-proxmox01\bots\heads
        "heartbeat_s": 30,
        "supervisor_url": "",         # optional POST target for registration
        "supervisor_token": "",
    },
    "logs": {
        "jsonl": True,
        "rotate_mb": 16,
        "keep_days": 14,
        "ship": {
            "share_dir": "",          # mirror JSONL to SMB, e.g. \\us-hm-proxmox01\bots\logs\peeweeherman
            "http_url": "",           # e.g. http://100.120.245.25:8080/  (Logstash http input)
            "http_index": "sysmon",
            "batch": 200,
        },
    },
    "links": {
        "grafana": "",                # e.g. http://<grafana-host>:3000
        "kibana": "",                 # e.g. http://<kibana-host>:5601
        "elk_host": "",               # e.g. robertdobbs
        "other_agents": [],
    },
    "gates": {
        "uptime_min_h": 0.0,
        "disk_free_min_gb": 15,
        "crash_lookback_h": 24,
        "crash_event_ids": [1000, 1001],
        "pending_reboot": "warn",     # allow | warn | block
    },
    "engine": {
        "default_step_timeout_s": 120,
        "abort_grace_s": 5,
        "record_history": 200,
    },
}


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value))
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path

    # -- accessors -------------------------------------------------------
    @property
    def agent(self) -> dict:
        return self.data["agent"]

    @property
    def server(self) -> dict:
        return self.data["server"]

    @property
    def sampler(self) -> dict:
        return self.data["sampler"]

    @property
    def head(self) -> dict:
        return self.data["head"]

    @property
    def logs(self) -> dict:
        return self.data["logs"]

    @property
    def gates(self) -> dict:
        return self.data["gates"]

    @property
    def engine(self) -> dict:
        return self.data["engine"]

    @property
    def links(self) -> dict:
        return self.data["links"]

    @property
    def data_dir(self) -> Path:
        return Path(self.data["paths"]["data_dir"])

    @property
    def runstates_dir(self) -> Path:
        raw = self.data["paths"].get("runstates_dir")
        return Path(raw) if raw else repo_root() / "runstates"

    @property
    def token(self) -> str:
        tf = self.data_dir / "api_token.txt"
        if not tf.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tf.write_text(uuid.uuid4().hex + uuid.uuid4().hex, encoding="ascii")
            try:  # best effort: token readable only by the current user
                os.chmod(tf, 0o600)
            except OSError:
                pass
        return tf.read_text(encoding="ascii").strip()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def repo_dir() -> Path:
    """Directory that holds runstates/, gui/, docs/ (works from source or install)."""
    for cand in (repo_root(), Path(sys.argv[0]).resolve().parent):
        if (cand / "runstates").is_dir():
            return cand
    return repo_root()


def config_path() -> Path:
    override = os.environ.get("SYSMON_CONFIG")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "hermes-sysmon" / "config.json"


def load(path: Path | None = None) -> Config:
    p = path or config_path()
    overlay: dict[str, Any] = {}
    if p.exists():
        try:
            overlay = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:  # keep startup alive, surface loudly
            print(f"[config] WARN: {p} is not valid JSON ({exc}); using defaults", file=sys.stderr)
    data = _expand(deep_merge(DEFAULTS, overlay))

    host = socket.gethostname().lower()
    if not data["agent"].get("name"):
        data["agent"]["name"] = f"{AGENT_KIND}:{host}"
    if not data["agent"].get("id"):
        print(f"[config] NOTE: agent.id is unset; using name as id ({data['agent']['name']})")
        data["agent"]["id"] = data["agent"]["name"]

    cfg = Config(data, p)
    if not p.exists():
        cfg.save()
    return cfg


def first_run_bootstrap() -> Path:
    """Write a config.json seeded from config.example.json if none exists."""
    target = config_path()
    if target.exists():
        return target
    example = repo_dir() / "config.example.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if example.exists():
        example_data = json.loads(example.read_text(encoding="utf-8"))
        merged = _expand(deep_merge(DEFAULTS, example_data))
        target.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    else:
        cfg = load(target)
        cfg.save()
    return target
