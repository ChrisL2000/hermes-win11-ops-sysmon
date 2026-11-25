"""HTTP API + embedded dashboard.

Two audiences:
* the local GUI (small window, no auth needed from loopback);
* a supervisory node / any remote tool on the tailnet (bearer token).

All mutating endpoints are idempotent-ish and report *what actually happened*
(step-by-step records with read-back verdicts), never just "ok".
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import psutil
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import AGENT_KIND, __version__
from . import gates as gates_mod

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    return host in LOOPBACK or host.startswith("127.")


class Runtime:
    """Everything the app needs, built once in __main__ and shared by routes."""

    def __init__(self, cfg, registry, engine, sampler, head, logger, started_at: float):
        self.cfg = cfg
        self.registry = registry
        self.engine = engine
        self.sampler = sampler
        self.head = head
        self.logger = logger
        self.started_at = started_at
        self.heads: dict[str, dict[str, Any]] = {}


def build_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title=f"{AGENT_KIND} worker", version=__version__, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    cfg = rt.cfg

    # ------------------------------------------------------------- auth
    def require_auth(request: Request) -> None:
        if not cfg.server.get("require_token_off_loopback", True):
            return
        client = request.client.host if request.client else ""
        if _is_loopback(client):
            return
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if token and token == cfg.token:
            return
        raise HTTPException(status_code=401, detail="bearer token required off loopback")

    auth = Depends(require_auth)

    # ------------------------------------------------------------ basics
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "agent": cfg.agent["name"],
            "version": __version__,
            "uptime_s": round(time.time() - rt.started_at, 1),
            "elevated": _elevated(),
            "runstate": rt.engine.current.get("runstate"),
            "busy": rt.engine.busy,
            "time": time.time(),
        }

    @app.get("/api/state", dependencies=[auth])
    def state() -> dict[str, Any]:
        st = rt.engine.status()
        st["summary"] = rt.sampler.summary()
        st["latest"] = rt.sampler.latest()
        st["head"] = rt.head.status()
        st["logs"] = rt.logger.status()
        return st

    @app.get("/api/runstates", dependencies=[auth])
    def runstates() -> dict[str, Any]:
        return {
            "runstates": [s.summary() for s in rt.registry.all()],
            "errors": rt.registry.errors,
            "current": rt.engine.current.get("runstate"),
        }

    @app.get("/api/runstates/{state_id}", dependencies=[auth])
    def runstate_detail(state_id: str) -> dict[str, Any]:
        s = rt.registry.get(state_id)
        if s is None:
            raise HTTPException(404, f"unknown run-state '{state_id}'")
        return s.as_dict()

    @app.post("/api/runstates/reload", dependencies=[auth])
    def reload_runstates() -> dict[str, Any]:
        states = rt.registry.reload()
        rt.logger.log("info", "runstates.reload", f"reloaded {len(states)} run-state(s)",
                      {"errors": rt.registry.errors})
        return {"ok": not rt.registry.errors, "ids": sorted(states), "errors": rt.registry.errors}

    # --------------------------------------------------------- transition
    @app.post("/api/transition", dependencies=[auth])
    def transition(body: dict[str, Any]) -> dict[str, Any]:
        target = str(body.get("target") or body.get("runstate") or "").strip()
        if not target:
            raise HTTPException(400, "body needs 'target'")
        options = {
            "dry_run": bool(body.get("dry_run", False)),
            "force_userland_restart": body.get("force_userland_restart"),
        }
        try:
            result = rt.engine.start(target, options)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        rt.logger.log("info", "transition.request", f"transition -> {target}", options)
        rt.head.publish("transition.start")
        return result

    @app.post("/api/abort", dependencies=[auth])
    def abort() -> dict[str, Any]:
        res = rt.engine.abort()
        if res.get("ok"):
            rt.logger.log("warn", "transition.abort", "abort requested by operator")
            rt.head.publish("transition.abort")
        return res

    @app.get("/api/gates", dependencies=[auth])
    def gates(target: str | None = None) -> dict[str, Any]:
        specs = gates_mod.host_snapshot_cfg(cfg.gates)
        if target:
            st = rt.registry.get(target)
            if st is None:
                raise HTTPException(404, f"unknown run-state '{target}'")
            specs = st.gates or specs
        results = gates_mod.evaluate_all(specs, cfg.gates)
        return {"gates": [g.as_dict() for g in results],
                "blocking": [g.kind for g in results if g.blocks]}

    # ------------------------------------------------------------ samples
    @app.get("/api/samples", dependencies=[auth])
    def samples(seconds: float = 120.0, limit: int = 3600) -> dict[str, Any]:
        rows = rt.sampler.window(max(5.0, min(seconds, 3600.0)))
        return {"samples": rows[-limit:], "count": len(rows)}

    @app.get("/api/stream")
    async def stream(request: Request, seconds: float = 300.0) -> StreamingResponse:
        # SSE. Loopback-only by default; remote callers must append ?token=.
        client = request.client.host if request.client else ""
        if not _is_loopback(client):
            tok = request.query_params.get("token", "")
            if tok != cfg.token:
                raise HTTPException(401, "token required")

        async def gen():
            last_log = 0.0
            while True:
                if await request.is_disconnected():
                    break
                payload = {
                    "health": {"runstate": rt.engine.current.get("runstate"),
                               "busy": rt.engine.busy, "uptime_s": round(time.time() - rt.started_at, 1)},
                    "state": rt.engine.status(),
                    "latest": rt.sampler.latest(),
                    "summary": rt.sampler.summary(),
                }
                yield f"data: {json.dumps(payload)}\n\n"
                last_log = time.time()
                await asyncio.sleep(1.0)
            _ = last_log

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --------------------------------------------------------------- logs
    @app.get("/api/logs", dependencies=[auth])
    def logs(n: int = 200) -> dict[str, Any]:
        return {"logs": rt.logger.tail(max(1, min(n, 1000))), "status": rt.logger.status()}

    @app.get("/api/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        latest = rt.sampler.latest() or {}
        st = rt.engine.status()
        tr = st["transition"] or {}
        lines = [
            "# HELP sysmon_cpu_percent CPU utilisation (all cores).",
            "# TYPE sysmon_cpu_percent gauge",
            f'sysmon_cpu_percent{{agent="{cfg.agent["name"]}"}} {latest.get("cpu", 0)}',
            "# TYPE sysmon_mem_percent gauge",
            f'sysmon_mem_percent{{agent="{cfg.agent["name"]}"}} {latest.get("mem", 0)}',
            "# TYPE sysmon_gpu_percent gauge",
            f'sysmon_gpu_percent{{agent="{cfg.agent["name"]}"}} {latest.get("gpu") if latest.get("gpu") is not None else "NaN"}',
            "# TYPE sysmon_disk_read_mbs gauge",
            f'sysmon_disk_read_mbs{{agent="{cfg.agent["name"]}"}} {latest.get("disk_read_mbs", 0)}',
            "# TYPE sysmon_disk_write_mbs gauge",
            f'sysmon_disk_write_mbs{{agent="{cfg.agent["name"]}"}} {latest.get("disk_write_mbs", 0)}',
            "# TYPE sysmon_net_rx_mbs gauge",
            f'sysmon_net_rx_mbs{{agent="{cfg.agent["name"]}"}} {latest.get("net_rx_mbs", 0)}',
            "# TYPE sysmon_transition_progress gauge",
            f'sysmon_transition_progress{{agent="{cfg.agent["name"]}",to="{tr.get("to", "")}"}} {tr.get("progress", 0)}',
            "# TYPE sysmon_transition_active gauge",
            f'sysmon_transition_active{{agent="{cfg.agent["name"]}"}} {1 if rt.engine.busy else 0}',
            "# TYPE sysmon_uptime_seconds gauge",
            f'sysmon_uptime_seconds{{agent="{cfg.agent["name"]}"}} {round(time.time() - rt.started_at, 1)}',
        ]
        return "\n".join(lines) + "\n"

    # --------------------------------------------------------------- head
    @app.get("/api/head", dependencies=[auth])
    def head_record() -> dict[str, Any]:
        return rt.head.record()

    @app.post("/api/head/publish", dependencies=[auth])
    def head_publish() -> dict[str, Any]:
        return rt.head.publish("api")

    @app.post("/api/head", dependencies=[auth])
    def head_ingest(body: dict[str, Any]) -> dict[str, Any]:
        """Supervisor side: accept a worker's head so it appears on the dashboard."""
        if body.get("schema") != "sysmon.head/v1":
            raise HTTPException(400, "not a sysmon.head/v1 record")
        aid = (body.get("agent") or {}).get("id")
        if not aid:
            raise HTTPException(400, "head record missing agent.id")
        body["_received_at"] = time.time()
        rt.heads[aid] = body
        rt.logger.log("info", "head.ingest", f"head registered: {aid}")
        return {"ok": True, "agents": len(rt.heads)}

    @app.get("/api/heads", dependencies=[auth])
    def heads() -> dict[str, Any]:
        return {"self": rt.head.record(), "peers": list(rt.heads.values()),
                "links": cfg.links}

    @app.get("/api/config", dependencies=[auth])
    def config_view() -> dict[str, Any]:
        data = json.loads(json.dumps(cfg.data))
        data["server"].pop("require_token_off_loopback", None)
        data["head"]["supervisor_token"] = "***" if data["head"].get("supervisor_token") else ""
        data["api_token_fingerprint"] = __import__("hashlib").sha256(cfg.token.encode()).hexdigest()[:16]
        return data

    # ---------------------------------------------------------------- web
    gui_dir = _gui_dir()
    if gui_dir and (gui_dir / "index.html").exists():
        app.mount("/static", StaticFiles(directory=str(gui_dir)), name="static")

        @app.get("/", response_class=HTMLResponse)
        def index() -> HTMLResponse:
            return HTMLResponse((gui_dir / "index.html").read_text(encoding="utf-8"))

        @app.get("/ui", response_class=HTMLResponse)
        def ui() -> HTMLResponse:
            return HTMLResponse((gui_dir / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/ping", response_class=PlainTextResponse)
    def ping() -> str:
        return "pong"

    @app.exception_handler(RuntimeError)
    def runtime_error(_request: Request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    return app


def _gui_dir() -> Path | None:
    here = Path(__file__).resolve().parent
    for cand in (here / "gui", here.parent / "gui"):
        if cand.is_dir():
            return cand
    return None


def _elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False
