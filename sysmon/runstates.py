"""Run-state manifests: load, validate, index.

A run-state is a declarative list of *steps* plus the policy knobs the user
asked about (whether userland is restarted, which gates must pass, what happens
when leaving the state). The manifest is the source of truth; the engine is a
thin, cancellable applier. Nothing here executes anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_ACTIONS = {
    "power.plan",
    "power.setting",
    "process.start",
    "process.stop",
    "process.ensure_running",
    "process.ensure_stopped",
    "service.set",
    "registry.set",
    "env.set",
    "script.run",
    "cmd.run",
    "noop",
}

VALID_GATES = {
    "uptime_min_h",
    "disk_free_min_gb",
    "no_pending_reboot",
    "no_crash_events",
    "process_running",
    "process_stopped",
}

VALID_POLICY = {"block", "warn", "off"}


class ManifestError(ValueError):
    pass


@dataclass
class Step:
    id: str
    label: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    est_s: float = 3.0
    weight: float = 1.0
    critical: bool = True
    verify: dict[str, Any] | None = None
    elevated: bool = True          # most run-state knobs need admin
    requires_reboot: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "action": self.action,
            "params": self.params, "est_s": self.est_s, "weight": self.weight,
            "critical": self.critical, "verify": self.verify,
            "elevated": self.elevated, "requires_reboot": self.requires_reboot,
        }


@dataclass
class RunState:
    id: str
    label: str
    description: str = ""
    est_seconds: float = 60.0
    steps: list[Step] = field(default_factory=list)
    on_leave: list[Step] = field(default_factory=list)
    gates: list[dict[str, Any]] = field(default_factory=list)
    userland: dict[str, Any] = field(default_factory=lambda: {"mode": "keep"})
    icon: str = ""
    source: str = ""

    # -- helpers ---------------------------------------------------------
    @property
    def restart_userland(self) -> bool:
        return str(self.userland.get("mode", "keep")).lower() == "restart"

    def weight_total(self) -> float:
        return sum(max(0.01, s.weight) for s in self.steps) or 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "description": self.description,
            "est_seconds": self.est_seconds, "icon": self.icon,
            "userland": self.userland, "gates": self.gates,
            "steps": [s.as_dict() for s in self.steps],
            "on_leave": [s.as_dict() for s in self.on_leave],
            "source": self.source,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "description": self.description,
            "est_seconds": self.est_seconds, "icon": self.icon,
            "steps": len(self.steps), "restart_userland": self.restart_userland,
        }


def _parse_steps(raw: Any, where: str) -> list[Step]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(f"{where}: 'steps' must be a list")
    out: list[Step] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ManifestError(f"{where}: step #{i} must be an object")
        action = str(item.get("action", "noop"))
        if action not in VALID_ACTIONS:
            raise ManifestError(f"{where}: step #{i} has unknown action '{action}'")
        sid = str(item.get("id") or f"s{i+1}")
        if sid in seen:
            raise ManifestError(f"{where}: duplicate step id '{sid}'")
        seen.add(sid)
        out.append(Step(
            id=sid,
            label=str(item.get("label") or sid),
            action=action,
            params=dict(item.get("params") or {}),
            est_s=float(item.get("est_s", 3.0)),
            weight=float(item.get("weight", 1.0)),
            critical=bool(item.get("critical", True)),
            verify=item.get("verify"),
            elevated=bool(item.get("elevated", True)),
            requires_reboot=bool(item.get("requires_reboot", False)),
        ))
    return out


def parse_manifest(data: dict[str, Any], source: str = "") -> RunState:
    if not isinstance(data, dict):
        raise ManifestError(f"{source}: manifest must be a JSON object")
    rid = data.get("id")
    if not rid or not isinstance(rid, str):
        raise ManifestError(f"{source}: missing string 'id'")
    where = f"{source or rid}"
    gates = data.get("gates") or []
    if not isinstance(gates, list):
        raise ManifestError(f"{where}: 'gates' must be a list")
    clean_gates: list[dict[str, Any]] = []
    for g in gates:
        if not isinstance(g, dict) or g.get("kind") not in VALID_GATES:
            raise ManifestError(f"{where}: invalid gate {g!r}")
        policy = str(g.get("policy", "warn")).lower()
        if policy not in VALID_POLICY:
            raise ManifestError(f"{where}: gate policy must be one of {sorted(VALID_POLICY)}")
        clean_gates.append({**g, "policy": policy})
    userland = data.get("userland") or {"mode": "keep"}
    mode = str(userland.get("mode", "keep")).lower()
    if mode not in {"keep", "restart"}:
        raise ManifestError(f"{where}: userland.mode must be 'keep' or 'restart'")
    return RunState(
        id=rid,
        label=str(data.get("label") or rid.title()),
        description=str(data.get("description") or ""),
        est_seconds=float(data.get("est_seconds", 60.0)),
        steps=_parse_steps(data.get("steps"), where),
        on_leave=_parse_steps(data.get("on_leave"), f"{where}:on_leave"),
        gates=clean_gates,
        userland={**userland, "mode": mode},
        icon=str(data.get("icon") or ""),
        source=source,
    )


class Registry:
    """Directory-backed run-state registry with mtime-aware reload."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._states: dict[str, RunState] = {}
        self._mtimes: dict[str, float] = {}
        self.errors: list[str] = []

    def reload(self) -> dict[str, RunState]:
        states: dict[str, RunState] = {}
        mtimes: dict[str, float] = {}
        errors: list[str] = []
        if not self.directory.is_dir():
            errors.append(f"runstates dir not found: {self.directory}")
            self._states, self._mtimes, self.errors = states, mtimes, errors
            return states
        for path in sorted(self.directory.glob("*.json")):
            try:
                mtime = path.stat().st_mtime
                if self._mtimes.get(str(path)) == mtime and str(path) in self._mtimes:
                    cached = self._states.get(path.stem)
                    if cached is not None:
                        states[cached.id] = cached
                        mtimes[str(path)] = mtime
                        continue
                data = json.loads(path.read_text(encoding="utf-8"))
                state = parse_manifest(data, source=str(path))
                states[state.id] = state
                mtimes[str(path)] = mtime
            except (json.JSONDecodeError, ManifestError, OSError) as exc:
                errors.append(f"{path.name}: {exc}")
        self._states, self._mtimes, self.errors = states, mtimes, errors
        return states

    def get(self, state_id: str) -> RunState | None:
        return self._states.get(state_id)

    def all(self) -> list[RunState]:
        return sorted(self._states.values(), key=lambda s: s.label.lower())

    def ids(self) -> list[str]:
        return [s.id for s in self.all()]
