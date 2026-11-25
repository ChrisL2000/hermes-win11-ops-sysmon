"""CLI + server bootstrap.

    python -m sysmon serve                # run the worker (default)
    python -m sysmon state                # print current state as JSON
    python -m sysmon go gaming            # request a transition
    python -m sysmon abort                # stop a transition in flight
    python -m sysmon gates                # host preflight, no token needed on loopback
    python -m sysmon head                 # print the head record
    python -m sysmon open                 # open the small GUI window
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from . import __version__
from . import steps as steps_mod
from .config import first_run_bootstrap, load
from .engine import Engine
from .head import HeadPublisher
from .logship import LogShip
from .runstates import Registry
from .sampler import Sampler

MAINTAIN_INTERVAL_S = 120.0


def _base_url(cfg) -> str:
    return f"http://127.0.0.1:{int(cfg.server.get('port', 8765))}"


def _call(cfg, method: str, path: str, body: dict | None = None) -> dict:
    url = _base_url(cfg) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


# ----------------------------------------------------------------- server
def serve(args: argparse.Namespace) -> int:
    import uvicorn
    from .api import Runtime, build_app

    cfg = load()
    registry = Registry(cfg.runstates_dir)
    registry.reload()
    logger = LogShip(cfg, cfg.agent["id"])

    def on_event(kind: str, data: dict) -> None:
        level = "error" if kind.endswith("error") else "info"
        logger.log(level, kind, kind, data)

    engine = Engine(cfg, registry, on_event=on_event)
    sampler = Sampler(interval_s=cfg.sampler.get("interval_s", 1.0),
                      buffer=cfg.sampler.get("buffer", 3600),
                      top_n=cfg.sampler.get("top_processes", 12))
    head = HeadPublisher(cfg, engine, registry, logger)
    rt = Runtime(cfg, registry, engine, sampler, head, logger, started_at=time.time())

    boot_state = cfg.data.get("engine", {}).get("boot_runstate")
    if boot_state and registry.get(boot_state):
        engine.set_current(boot_state, source="boot")

    sampler.start()
    head.start()
    logger.log("info", "sysmon.start",
               f"{cfg.agent['name']} v{__version__} starting",
               {"config": str(cfg.path), "runstates": registry.ids(),
                "errors": registry.errors, "elevated": steps_mod.is_admin()})

    maintain = threading.Event()

    def maintainer() -> None:
        auto = bool(cfg.data.get("engine", {}).get("auto_remediate", False))
        while not maintain.wait(MAINTAIN_INTERVAL_S):
            if engine.busy:
                continue
            cur = engine.current.get("runstate")
            manifest = registry.get(cur or "")
            if manifest is None:
                continue
            drift = []
            for s in manifest.steps:
                if not s.critical:
                    continue
                if steps_mod.EXECUTORS.get(s.action) in (None, steps_mod.act_noop,
                                                         steps_mod.act_process_start,
                                                         steps_mod.act_script_run):
                    continue
                v = steps_mod.verify(s.verify, s)
                if not v.get("ok"):
                    drift.append({"step": s.id, "detail": v.get("detail", "")})
            if drift:
                engine.drift.append({"at": time.time(), "runstate": manifest.id, "drift": drift})
                logger.log("warn", "state.drift", f"drift detected in {manifest.id}",
                           {"drift": drift, "auto_remediate": auto})
                if auto:
                    try:
                        engine.start(manifest.id, {"force_userland_restart": False})
                    except (KeyError, RuntimeError) as exc:
                        logger.log("error", "state.remediate_failed", str(exc))

    threading.Thread(target=maintainer, name="sysmon-maintainer", daemon=True).start()

    app = build_app(rt)
    host = cfg.server.get("host", "127.0.0.1")
    port = int(cfg.server.get("port", 8765))
    servers = [uvicorn.Config(app, host=host, port=port, log_level="warning")]
    for extra in cfg.server.get("bind_extra_hosts") or []:
        if extra:
            servers.append(uvicorn.Config(app, host=extra, port=port, log_level="warning"))

    uvicorns = [uvicorn.Server(c) for c in servers]
    threads = []
    for srv in uvicorns[1:]:
        t = threading.Thread(target=srv.run, name=f"uvicorn-{srv.config.host}", daemon=True)
        t.start()
        threads.append(t)

    print(f"[sysmon] {cfg.agent['name']} v{__version__}")
    print(f"[sysmon] config    : {cfg.path}")
    print(f"[sysmon] dashboard : {_base_url(cfg)}/")
    print(f"[sysmon] elevated  : {steps_mod.is_admin()}")
    print(f"[sysmon] runstates : {', '.join(registry.ids()) or '(none)'}")
    if registry.errors:
        print(f"[sysmon] MANIFEST ERRORS: {registry.errors}", file=sys.stderr)

    try:
        uvicorns[0].run()
    finally:
        for srv in uvicorns[1:]:
            srv.should_exit = True
        sampler.stop()
        head.stop()
        logger.log("info", "sysmon.stop", "worker stopping")
        logger.stop()
    return 0


# -------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    first_run_bootstrap()
    parser = argparse.ArgumentParser(prog="sysmon", description="Windows run-state worker")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="run the worker")

    p_go = sub.add_parser("go", help="request a transition")
    p_go.add_argument("target")
    p_go.add_argument("--dry-run", action="store_true")
    p_go.add_argument("--userland", choices=["keep", "restart"], default=None)

    sub.add_parser("abort", help="abort the running transition")
    sub.add_parser("state", help="print engine state")
    sub.add_parser("runstates", help="list run-states")
    sub.add_parser("head", help="print the head record")
    sub.add_parser("gates", help="evaluate host preflight gates")
    p_open = sub.add_parser("open", help="open the GUI window")
    p_open.add_argument("--url")

    args = parser.parse_args(argv)
    cmd = args.cmd or "serve"

    try:
        cfg = load()
    except Exception as exc:  # noqa: BLE001
        print(f"[sysmon] config error: {exc}", file=sys.stderr)
        return 2

    if cmd == "serve":
        return serve(args)

    try:
        if cmd == "go":
            body = {"target": args.target, "dry_run": args.dry_run}
            if args.userland:
                body["force_userland_restart"] = args.userland == "restart"
            out = _call(cfg, "POST", "/api/transition", body)
            print(json.dumps(out.get("transition") or out, indent=2))
            return 0
        if cmd == "abort":
            print(json.dumps(_call(cfg, "POST", "/api/abort"), indent=2))
            return 0
        if cmd == "state":
            print(json.dumps(_call(cfg, "GET", "/api/state"), indent=2))
            return 0
        if cmd == "runstates":
            print(json.dumps(_call(cfg, "GET", "/api/runstates"), indent=2))
            return 0
        if cmd == "head":
            print(json.dumps(_call(cfg, "GET", "/api/head"), indent=2))
            return 0
        if cmd == "gates":
            print(json.dumps(_call(cfg, "GET", "/api/gates"), indent=2))
            return 0
        if cmd == "open":
            url = args.url or _base_url(cfg) + "/ui"
            _open_window(url)
            return 0
    except urllib.error.URLError as exc:
        print(f"[sysmon] worker not reachable at {_base_url(cfg)}: {exc}", file=sys.stderr)
        return 3
    except urllib.error.HTTPError as exc:
        print(f"[sysmon] HTTP {exc.code}: {exc.read().decode(errors='replace')}", file=sys.stderr)
        return 4

    parser.print_help()
    return 1


def _open_window(url: str) -> None:
    """Prefer a chromeless Edge/Chrome app window (WebView2 is already installed)."""
    import subprocess
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=560,860",
                              "--window-position=1200,80",
                              "--user-data-dir=" + str(os.path.join(os.environ.get("LOCALAPPDATA", "."),
                                                                    "hermes-sysmon", "ui-profile"))])
            return
    webbrowser.open(url)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
