"""Shared pytest fixtures: a throwaway config + registry rooted in tmp_path."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    from sysmon.config import load

    data_dir = tmp_path / "data"
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "agent": {"name": "test:sysmon", "id": "test-sysmon"},
        "server": {"host": "127.0.0.1", "port": 18765, "bind_extra_hosts": []},
        "paths": {"data_dir": str(data_dir), "runstates_dir": str(REPO / "runstates")},
        "head": {"local_dir": str(tmp_path / "head"), "share_dir": "", "heartbeat_s": 999},
        "logs": {"ship": {"share_dir": "", "http_url": ""}},
        "sampler": {"interval_s": 0.5, "buffer": 120},
    }), encoding="utf-8")
    monkeypatch.setenv("SYSMON_CONFIG", str(cfg_path))
    c = load(cfg_path)
    return c


@pytest.fixture()
def registry(cfg):
    from sysmon.runstates import Registry
    r = Registry(cfg.runstates_dir)
    r.reload()
    return r
