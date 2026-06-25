import asyncio
import fcntl
import logging
import os
import secrets
import socket
import struct
import subprocess
import time
from pathlib import Path

import uvloop
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.responses import Response

from database import init_db, get_all_sources, get_source, create_source, update_source, delete_source, set_enabled
from engine import engine
from models import SourceCreate, SourceUpdate, SourceOut, DestinationOut, SourceStats

uvloop.install()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
logger = logging.getLogger("nmea-web")

app = FastAPI(title="NMEA Forwarder")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

security = HTTPBasic()

VALID_USER = "admin"
VALID_PASS = "admin"


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    is_ok = secrets.compare_digest(credentials.username, VALID_USER) and \
            secrets.compare_digest(credentials.password, VALID_PASS)
    if not is_ok:
        raise HTTPException(401, "Unauthorized",
                            headers={"WWW-Authenticate": "Basic realm=\"NMEA Forwarder\""})
    return True


@app.on_event("startup")
async def startup():
    loop = asyncio.get_running_loop()
    engine.bind_loop(loop)
    init_db()
    engine.start_gc()
    sources = get_all_sources()
    for s in sources:
        if s.get("enabled"):
            try:
                await engine.start_source(s)
            except Exception as e:
                logger.error("Failed to autostart %s: %s", s["name"], e)


def _source_out(src: dict) -> dict:
    sid = src["id"]
    running = engine.is_running(sid)
    stats = engine.get_stats(sid) or {"packets_in": 0, "packets_out": 0, "errors": 0, "dropped": 0, "running": running}
    return {
        **src,
        "running": running,
        "stats_packets_in": stats["packets_in"],
        "stats_packets_out": stats["packets_out"],
        "stats_errors": stats["errors"],
        "stats_dropped": stats.get("dropped", 0),
    }


_interfaces_cache = {"data": [], "ts": 0.0}
_INTERFACES_TTL = 30


def _interfaces():
    now = time.monotonic()
    if _interfaces_cache["data"] and (now - _interfaces_cache["ts"]) < _INTERFACES_TTL:
        return _interfaces_cache["data"]
    try:
        iface_names = sorted(os.listdir("/sys/class/net/"))
    except Exception:
        return _interfaces_cache.get("data", [])
    ifaces = []
    for name in iface_names:
        ips = []
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                ifr = struct.pack("256s", name[:15].encode())
                result = fcntl.ioctl(s.fileno(), 0x8915, ifr)
                ip = socket.inet_ntoa(result[20:24])
                if ip and ip != "0.0.0.0":
                    ips.append(ip)
            finally:
                s.close()
        except Exception:
            pass
        ifaces.append({"name": name, "ips": ips})
    _interfaces_cache["data"] = ifaces
    _interfaces_cache["ts"] = now
    return ifaces


def _engine_async(coro):
    loop = engine._loop
    if loop is None:
        raise HTTPException(503, "Engine not ready")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=10)
    except Exception as e:
        raise HTTPException(500, str(e))


# --- API ---

@app.get("/api/sources", dependencies=[Depends(require_auth)])
def api_list_sources():
    sources = get_all_sources()
    return [_source_out(s) for s in sources]


@app.get("/api/sources/{source_id}", dependencies=[Depends(require_auth)])
def api_get_source(source_id: int):
    src = get_source(source_id)
    if src is None:
        raise HTTPException(404, "Source not found")
    return _source_out(src)


@app.post("/api/sources", status_code=201, dependencies=[Depends(require_auth)])
def api_create_source(data: SourceCreate):
    if not data.destinations:
        raise HTTPException(400, "At least one destination is required")
    src = create_source(data.dict())
    return _source_out(src)


@app.put("/api/sources/{source_id}", dependencies=[Depends(require_auth)])
def api_update_source(source_id: int, data: SourceUpdate):
    was_running = engine.is_running(source_id)
    if was_running:
        _engine_async(engine.stop_source(source_id))
    src = update_source(source_id, data.dict(exclude_unset=True))
    if src is None:
        raise HTTPException(404, "Source not found")
    if was_running:
        _engine_async(engine.start_source(src))
    return _source_out(src)


@app.delete("/api/sources/{source_id}", dependencies=[Depends(require_auth)])
def api_delete_source(source_id: int):
    if engine.is_running(source_id):
        _engine_async(engine.stop_source(source_id))
    if not delete_source(source_id):
        raise HTTPException(404, "Source not found")
    return {"ok": True}


@app.post("/api/sources/{source_id}/start", dependencies=[Depends(require_auth)])
def api_start_source(source_id: int):
    src = get_source(source_id)
    if src is None:
        raise HTTPException(404, "Source not found")
    if engine.is_running(source_id):
        return {"ok": True, "status": "already_running"}
    _engine_async(engine.start_source(src))
    set_enabled(source_id, True)
    return {"ok": True, "status": "started"}


@app.post("/api/sources/{source_id}/stop", dependencies=[Depends(require_auth)])
def api_stop_source(source_id: int):
    src = get_source(source_id)
    if src is None:
        raise HTTPException(404, "Source not found")
    if not engine.is_running(source_id):
        return {"ok": True, "status": "already_stopped"}
    _engine_async(engine.stop_source(source_id))
    set_enabled(source_id, False)
    return {"ok": True, "status": "stopped"}


@app.get("/api/sources/{source_id}/stats", dependencies=[Depends(require_auth)])
def api_source_stats(source_id: int):
    stats = engine.get_stats(source_id)
    if stats is None:
        raise HTTPException(404, "Not running")
    return stats


@app.get("/api/interfaces", dependencies=[Depends(require_auth)])
def api_interfaces():
    return _interfaces()


@app.get("/api/sources/{source_id}/log", dependencies=[Depends(require_auth)])
def api_source_log(source_id: int, lines: int = Query(default=10, ge=1, le=200)):
    src = get_source(source_id)
    if src is None:
        raise HTTPException(404, "Source not found")
    if not src.get("log_enabled") or not src.get("log_file"):
        raise HTTPException(400, "Logging not enabled for this source")

    log_path = src["log_file"]
    if not os.path.isfile(log_path):
        return {"file": log_path, "lines": [], "total": 0}

    try:
        total = -1
        file_size = os.path.getsize(log_path)
        if file_size < 100 * 1024 * 1024:
            result = subprocess.run(
                ["wc", "-l", log_path], capture_output=True, text=True, timeout=10
            )
            total = int(result.stdout.split()[0]) if result.stdout.strip() else 0
        else:
            total = -1

        result = subprocess.run(
            ["tail", "-n", str(lines), log_path], capture_output=True, text=True, timeout=5
        )
        last = result.stdout.splitlines()
        return {
            "file": log_path,
            "lines": last,
            "shown": len(last),
            "total": total,
            "size_bytes": file_size,
        }
    except Exception as e:
        raise HTTPException(500, f"Cannot read log file: {e}")


@app.get("/api/system", dependencies=[Depends(require_auth)])
def api_system():
    mem = {"total_mb": 0.0, "used_mb": 0.0, "available_mb": 0.0}
    try:
        mem_total = mem_avail = 0
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1])
        mem["total_mb"] = round(mem_total / 1024, 1)
        mem["available_mb"] = round(mem_avail / 1024, 1)
        mem["used_mb"] = round((mem_total - mem_avail) / 1024, 1)
    except Exception:
        pass

    load = {"1m": 0.0, "5m": 0.0, "15m": 0.0}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            load["1m"] = float(parts[0])
            load["5m"] = float(parts[1])
            load["15m"] = float(parts[2])
    except Exception:
        pass

    uptime_seconds = 0.0
    try:
        with open("/proc/uptime") as f:
            uptime_seconds = float(f.read().split()[0])
    except Exception:
        pass

    proc_rss_mb = 0.0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    proc_rss_mb = round(int(line.split()[1]) / 1024, 1)
                    break
    except Exception:
        pass

    active = sum(1 for e in engine._tasks.values() if e.get("running"))
    total = len(engine._tasks)

    return {
        "memory": mem,
        "cpu_load": load,
        "uptime_seconds": round(uptime_seconds, 1),
        "process_rss_mb": proc_rss_mb,
        "engine": {
            "active_sources": active,
            "total_tracked": total,
        },
    }


# --- Web UI ---

@app.get("/", dependencies=[Depends(require_auth)])
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
