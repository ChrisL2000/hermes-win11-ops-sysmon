from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from sysmon.api import Runtime, build_app
from sysmon.engine import Engine
from sysmon.head import HeadPublisher
from sysmon.logship import LogShip
from sysmon.runstates import Registry
from sysmon.sampler import Sampler


@pytest.fixture()
def client(cfg, tmp_path):
    d = tmp_path / "rs"
    d.mkdir()
    (d / "alpha.json").write_text(json.dumps({"id": "alpha", "label": "Alpha", "steps": [
        {"id": "s1", "action": "noop", "elevated": False}]}), encoding="utf-8")
    (d / "beta.json").write_text(json.dumps({"id": "beta", "label": "Beta", "steps": [
        {"id": "s1", "action": "noop", "elevated": False}]}), encoding="utf-8")
    reg = Registry(d)
    reg.reload()
    logger = LogShip(cfg, cfg.agent["id"])
    engine = Engine(cfg, reg)
    sampler = Sampler(interval_s=0.5, buffer=60)
    head = HeadPublisher(cfg, engine, reg, logger)
    rt = Runtime(cfg, reg, engine, sampler, head, logger, started_at=time.time())
    app = build_app(rt)
    with TestClient(app) as c:
        c.rt = rt  # type: ignore[attr-defined]
        yield c
    sampler.stop()
    logger.stop()


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["runstate"] is None


def test_runstates_listing(client):
    body = client.get("/api/runstates").json()
    assert {r["id"] for r in body["runstates"]} == {"alpha", "beta"}


def test_unknown_runstate_404(client):
    assert client.get("/api/runstates/nope").status_code == 404


def test_transition_and_state(client):
    r = client.post("/api/transition", json={"target": "alpha"})
    assert r.status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        st = client.get("/api/state").json()
        if not st["busy"]:
            break
        time.sleep(0.05)
    st = client.get("/api/state").json()
    assert st["transition"]["state"] == "done"
    assert st["current"]["runstate"] == "alpha"


def test_transition_unknown_target_404(client):
    assert client.post("/api/transition", json={"target": "ghost"}).status_code == 404


def test_transition_missing_target_400(client):
    assert client.post("/api/transition", json={}).status_code == 400


def test_abort_when_idle(client):
    body = client.post("/api/abort").json()
    assert body["ok"] is False


def test_head_record_shape(client):
    head = client.get("/api/head").json()
    assert head["schema"] == "sysmon.head/v1"
    assert head["api"]["token_fingerprint"]
    assert head["api"]["endpoints"][0]["scope"] == "loopback"


def test_head_inbox_roundtrip(client):
    head = client.get("/api/head").json()
    head["agent"] = {"id": "peer-1", "name": "peer"}
    assert client.post("/api/head", json=head).json()["ok"] is True
    peers = client.get("/api/heads").json()["peers"]
    assert peers[0]["agent"]["id"] == "peer-1"


def test_head_inbox_rejects_junk(client):
    assert client.post("/api/head", json={"nope": True}).status_code == 400


def test_metrics_exposition(client):
    text = client.get("/api/metrics").text
    assert "sysmon_cpu_percent" in text and "sysmon_transition_active" in text


def test_gates_endpoint(client):
    body = client.get("/api/gates").json()
    assert isinstance(body["gates"], list) and body["gates"]


def test_logs_capture_transition(client):
    client.post("/api/transition", json={"target": "beta"})
    deadline = time.time() + 10
    while time.time() < deadline and client.get("/api/state").json()["busy"]:
        time.sleep(0.05)
    time.sleep(0.3)
    logs = client.get("/api/logs?n=50").json()["logs"]
    assert any(r["event"]["kind"] == "transition.request" for r in logs)


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<canvas" in r.text
