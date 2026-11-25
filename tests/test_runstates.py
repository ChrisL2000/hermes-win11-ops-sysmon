from __future__ import annotations

import json

import pytest

from sysmon.runstates import ManifestError, Registry, parse_manifest


def test_repo_runstates_all_parse(registry):
    assert not registry.errors, registry.errors
    ids = registry.ids()
    for expected in ("desktop", "work", "game", "game-stream", "discord", "create", "moves", "safe"):
        assert expected in ids, f"missing built-in run-state {expected}"


def test_manifest_minimal():
    st = parse_manifest({"id": "x", "label": "X", "steps": [{"action": "noop", "id": "a"}]})
    assert st.id == "x"
    assert st.restart_userland is False
    assert st.steps[0].action == "noop"


def test_unknown_action_rejected():
    with pytest.raises(ManifestError):
        parse_manifest({"id": "x", "steps": [{"action": "explode"}]})


def test_duplicate_step_ids_rejected():
    with pytest.raises(ManifestError):
        parse_manifest({"id": "x", "steps": [{"id": "a", "action": "noop"},
                                             {"id": "a", "action": "noop"}]})


def test_bad_gate_policy_rejected():
    with pytest.raises(ManifestError):
        parse_manifest({"id": "x", "gates": [{"kind": "uptime_min_h", "policy": "maybe"}]})


def test_userland_mode_validated():
    with pytest.raises(ManifestError):
        parse_manifest({"id": "x", "userland": {"mode": "nuke"}})


def test_registry_reload_picks_up_new_file(tmp_path):
    d = tmp_path / "rs"
    d.mkdir()
    (d / "one.json").write_text(json.dumps({"id": "one", "steps": []}), encoding="utf-8")
    reg = Registry(d)
    assert reg.reload()
    (d / "two.json").write_text(json.dumps({"id": "two", "steps": []}), encoding="utf-8")
    assert set(reg.reload()) == {"one", "two"}
    (d / "two.json").write_text("{not json", encoding="utf-8")
    reg.reload()
    assert reg.get("one") is not None
    assert any("two.json" in e for e in reg.errors)


def test_weight_total_positive(registry):
    for st in registry.all():
        assert st.weight_total() > 0


def test_game_stream_has_leave_steps(registry):
    st = registry.get("game-stream")
    assert st is not None and st.on_leave, "leaving game-stream should tear down OBS"
