from __future__ import annotations

import json
import time

import pytest

from sysmon.engine import ABORTED, DONE, FAILED, RUNNING, Engine
from sysmon.runstates import Registry


def make_registry(tmp_path, manifests: dict[str, dict]) -> Registry:
    d = tmp_path / "rs"
    d.mkdir(exist_ok=True)
    for name, body in manifests.items():
        (d / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
    reg = Registry(d)
    reg.reload()
    assert not reg.errors, reg.errors
    return reg


def wait_for(engine: Engine, timeout: float = 20.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        tr = engine.transition
        if tr and tr["state"] not in (RUNNING, "aborting"):
            return tr
        time.sleep(0.05)
    raise AssertionError(f"transition did not settle: {engine.transition}")


@pytest.fixture()
def simple_registry(tmp_path):
    return make_registry(tmp_path, {
        "a": {"id": "a", "label": "A", "steps": [
            {"id": "s1", "action": "noop", "params": {"note": "one"}, "elevated": False},
            {"id": "s2", "action": "noop", "params": {"note": "two"}, "elevated": False},
        ]},
        "b": {"id": "b", "label": "B", "steps": [
            {"id": "s1", "action": "cmd.run", "params": {"command": "exit 0"}, "elevated": False},
        ]},
    })


def test_transition_runs_all_steps(cfg, simple_registry):
    eng = Engine(cfg, simple_registry)
    eng.start("a")
    tr = wait_for(eng)
    assert tr["state"] == DONE
    assert tr["progress"] == 100.0
    assert [s["status"] for s in tr["steps"]] == ["ok", "ok"]
    assert eng.current["runstate"] == "a"


def test_dry_run_executes_nothing(cfg, simple_registry):
    eng = Engine(cfg, simple_registry)
    eng.start("a", {"dry_run": True})
    tr = wait_for(eng)
    assert tr["dry_run"] is True
    assert all(s["status"] == "skipped" for s in tr["steps"])


def test_failing_critical_step_stops_transition(cfg, tmp_path):
    reg = make_registry(tmp_path, {"bad": {"id": "bad", "steps": [
        {"id": "boom", "label": "Boom", "action": "cmd.run",
         "params": {"command": "exit 7"}, "elevated": False},
        {"id": "after", "action": "noop", "elevated": False},
    ]}})
    eng = Engine(cfg, reg)
    eng.start("bad")
    tr = wait_for(eng)
    assert tr["state"] == FAILED
    assert "Boom" in tr["error"]
    assert tr["steps"][1]["status"] == "pending"
    assert eng.current["runstate"] is None


def test_noncritical_failure_continues(cfg, tmp_path):
    reg = make_registry(tmp_path, {"soft": {"id": "soft", "steps": [
        {"id": "boom", "action": "cmd.run", "params": {"command": "exit 7"},
         "elevated": False, "critical": False},
        {"id": "after", "action": "noop", "elevated": False},
    ]}})
    eng = Engine(cfg, reg)
    eng.start("soft")
    tr = wait_for(eng)
    assert tr["state"] == DONE
    assert [s["status"] for s in tr["steps"]] == ["failed", "ok"]


def test_abort_leaves_partial_state(cfg, tmp_path):
    reg = make_registry(tmp_path, {"slow": {"id": "slow", "steps": [
        {"id": "slow1", "action": "cmd.run",
         "params": {"command": "ping -n 4 127.0.0.1 > nul"},
         "elevated": False},
        {"id": "s2", "action": "noop", "elevated": False},
    ]}})
    eng = Engine(cfg, reg)
    eng.start("slow")
    time.sleep(0.4)
    assert eng.busy
    eng.abort()
    tr = wait_for(eng)
    assert tr["state"] == ABORTED
    assert tr["steps"][0]["status"] == "ok"      # it really ran
    assert tr["steps"][1]["status"] == "aborted"
    assert eng.current["runstate"] is None       # never claims the target state


def test_second_transition_rejected_while_busy(cfg, tmp_path):
    reg = make_registry(tmp_path, {"slow": {"id": "slow", "steps": [
        {"id": "slow1", "action": "cmd.run",
         "params": {"command": "ping -n 3 127.0.0.1 > nul"},
         "elevated": False}]}})
    eng = Engine(cfg, reg)
    eng.start("slow")
    with pytest.raises(RuntimeError):
        eng.start("slow")
    wait_for(eng)


def test_blocking_gate_prevents_execution(cfg, tmp_path):
    reg = make_registry(tmp_path, {"gated": {"id": "gated", "gates": [
        {"kind": "uptime_min_h", "value": 100000, "policy": "block"}],
        "steps": [{"id": "s", "action": "noop", "elevated": False}]}})
    eng = Engine(cfg, reg)
    eng.start("gated")
    tr = wait_for(eng)
    assert tr["state"] == FAILED
    assert "gate" in tr["error"]


def test_leave_steps_run_when_switching(cfg, tmp_path):
    reg = make_registry(tmp_path, {
        "one": {"id": "one", "steps": [{"id": "s", "action": "noop", "elevated": False}],
                "on_leave": [{"id": "bye", "action": "noop", "elevated": False}]},
        "two": {"id": "two", "steps": [{"id": "s", "action": "noop", "elevated": False}]},
    })
    eng = Engine(cfg, reg)
    eng.start("one")
    wait_for(eng)
    eng.start("two")
    tr = wait_for(eng)
    phases = [s["phase"] for s in tr["steps"]]
    assert phases[0] == "on_leave"
    assert tr["state"] == DONE


def test_userland_restart_inserted_when_manifest_requests_it(cfg, tmp_path):
    reg = make_registry(tmp_path, {"ustate": {
        "id": "ustate", "userland": {"mode": "restart", "keep": ["explorer.exe"]},
        "steps": [{"id": "s", "action": "noop", "elevated": False}]}})
    eng = Engine(cfg, reg)
    eng.start("ustate", {"dry_run": True})
    tr = wait_for(eng)
    assert any(s["action"] == "internal.userland" for s in tr["steps"])


def test_event_stream_receives_step_events(cfg, simple_registry):
    events: list[tuple[str, dict]] = []
    eng = Engine(cfg, simple_registry, on_event=lambda k, d: events.append((k, d)))
    eng.start("b")
    wait_for(eng)
    kinds = [k for k, _ in events]
    assert "step.start" in kinds and "step.end" in kinds and "transition.end" in kinds


def test_history_records_completed_transition(cfg, simple_registry):
    eng = Engine(cfg, simple_registry)
    eng.start("a")
    wait_for(eng)
    assert eng.status()["history"][-1]["to"] == "a"


def test_state_survives_worker_restart(cfg, simple_registry):
    eng = Engine(cfg, simple_registry)
    eng.start("a")
    wait_for(eng)
    reborn = Engine(cfg, simple_registry)
    assert reborn.current["runstate"] == "a"
    assert reborn.status()["history"][-1]["to"] == "a"


def test_interrupted_transition_is_marked_not_resumed(cfg, simple_registry, tmp_path):
    import json as _json

    eng = Engine(cfg, simple_registry)
    eng.start("a")
    wait_for(eng)
    state_file = eng.state_path
    data = _json.loads(state_file.read_text(encoding="utf-8"))
    data["last_transition"] = {"id": "x", "state": "running", "to": "a", "from": None,
                               "progress": 10.0, "steps": []}
    state_file.write_text(_json.dumps(data), encoding="utf-8")
    reborn = Engine(cfg, simple_registry)
    assert reborn.transition["state"] == FAILED
    assert "interrupted" in reborn.transition["error"]
