import asyncio
import socket
import ipaddress
import time
import os
import logging
from typing import Optional

logger = logging.getLogger("nmea.engine")

LOG_BATCH_BYTES = 4096


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, engine, source, out_sockets, log_fh):
        self.engine = engine
        self.source = source
        self.out_sockets = out_sockets
        self.log_fh = log_fh
        self.transport = None
        self._log_buf = []
        self._log_buf_size = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        sid = self.source["id"]
        entry = self.engine._tasks.get(sid)
        if entry is None or not entry["running"]:
            return

        stats = entry["stats"]
        stats["packets_in"] += 1

        ts = time.time()
        src_ip = addr[0]

        if self.log_fh:
            payload = data.decode("ascii", errors="replace").strip()
            if payload:
                line = f"{ts};{src_ip};{payload}\n"
                self._log_buf.append(line)
                self._log_buf_size += len(line)

        sent_any = False
        for out_sock, dest_addr in self.out_sockets:
            try:
                out_sock.sendto(data, dest_addr)
                sent_any = True
            except Exception:
                stats["errors"] += 1

        if sent_any:
            stats["packets_out"] += 1

        if self._log_buf_size >= LOG_BATCH_BYTES:
            self._log_fh_flush()

    def _log_fh_flush(self):
        if self._log_buf:
            try:
                self.log_fh.write("".join(self._log_buf))
                self.log_fh.flush()
            except Exception:
                pass
            self._log_buf.clear()
            self._log_buf_size = 0

    def error_received(self, exc):
        sid = self.source["id"]
        entry = self.engine._tasks.get(sid)
        if entry:
            entry["stats"]["errors"] += 1

    def connection_lost(self, exc):
        self._log_fh_flush()


class ForwarderEngine:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop = None
        self._tasks: dict[int, dict] = {}
        self._gc_task: Optional[asyncio.Task] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def start_gc(self):
        if self._loop and self._loop.is_running():
            self._gc_task = asyncio.run_coroutine_threadsafe(self._gc_loop(), self._loop)
        else:
            self._gc_task = asyncio.create_task(self._gc_loop())

    async def start_source(self, source: dict) -> bool:
        sid = source["id"]
        if sid in self._tasks:
            return False

        self._tasks[sid] = {
            "task": None,
            "stats": {"packets_in": 0, "packets_out": 0, "errors": 0, "dropped": 0},
            "snapshot": {"packets_in": 0, "packets_out": 0, "errors": 0, "dropped": 0},
            "running": True,
            "event": asyncio.Event(),
        }
        task = asyncio.create_task(self._run(source))
        self._tasks[sid]["task"] = task
        logger.info("Source %s (%s) started", source["name"], sid)
        return True

    async def stop_source(self, source_id: int) -> bool:
        entry = self._tasks.get(source_id)
        if entry is None:
            return False

        entry["running"] = False
        try:
            entry["event"].set()
        except Exception:
            pass

        transport = entry.get("transport")
        if transport:
            try:
                transport.close()
            except Exception:
                pass

        try:
            entry["task"].cancel()
            await entry["task"]
        except (asyncio.CancelledError, Exception):
            pass

        self._tasks.pop(source_id, None)
        logger.info("Source %s stopped", source_id)
        return True

    def get_stats(self, source_id: int) -> Optional[dict]:
        entry = self._tasks.get(source_id)
        if entry is None:
            return None
        cur = entry["stats"]
        snap = entry["snapshot"]
        delta = {
            "packets_in": cur["packets_in"] - snap["packets_in"],
            "packets_out": cur["packets_out"] - snap["packets_out"],
            "errors": cur["errors"] - snap["errors"],
            "dropped": cur["dropped"] - snap["dropped"],
        }
        entry["snapshot"] = {
            "packets_in": cur["packets_in"],
            "packets_out": cur["packets_out"],
            "errors": cur["errors"],
            "dropped": cur["dropped"],
        }
        return {**delta, "running": entry["running"]}

    def is_running(self, source_id: int) -> bool:
        entry = self._tasks.get(source_id)
        return entry is not None and entry.get("running", False)

    async def _run(self, source: dict):
        sid = source["id"]
        name = source["name"]
        listen_ip = source.get("listen_ip", "0.0.0.0")
        listen_port = source["listen_port"]
        interface = source.get("interface")
        log_enabled = source.get("log_enabled")
        log_file = source.get("log_file")
        destinations = source.get("destinations", [])

        loop = asyncio.get_running_loop()

        out_sockets = []
        log_fh = None
        sock = None
        transport = None
        protocol = None

        try:
            for d in destinations:
                out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                if d.get("bind_ip"):
                    try:
                        out.bind((d["bind_ip"], 0))
                    except OSError as e:
                        logger.warning("Cannot bind outbound to %s: %s", d["bind_ip"], e)
                if d["mode"] == "broadcast":
                    out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    try:
                        net = ipaddress.IPv4Network(
                            f"{d['dest_ip']}/{d['prefix']}", strict=False
                        )
                        dest_addr = (str(net.broadcast_address), d["dest_port"])
                    except ValueError:
                        dest_addr = (d["dest_ip"], d["dest_port"])
                else:
                    dest_addr = (d["dest_ip"], d["dest_port"])
                out_sockets.append((out, dest_addr))

            if log_enabled and log_file:
                try:
                    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
                    exists = os.path.exists(log_file)
                    empty = (os.path.getsize(log_file) == 0) if exists else True
                    log_fh = open(log_file, mode="a", encoding="utf-8")
                    if empty:
                        log_fh.write("timestamp;src_ip;nmea_sentence\n")
                        log_fh.flush()
                except OSError as e:
                    logger.error("Cannot open log %s: %s", log_file, e)

            logger.info("Source %s listening %s:%s -> %s destinations",
                         name, listen_ip, listen_port, len(out_sockets))

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

            if interface:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, 25, interface.encode() + b"\0")
                except (OSError, PermissionError) as e:
                    logger.error("Failed SO_BINDTODEVICE %s: %s", interface, e)

            sock.bind((listen_ip, listen_port))
            sock.setblocking(False)

            protocol = _DatagramProtocol(self, source, out_sockets, log_fh)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                sock=sock,
            )

            entry = self._tasks.get(sid)
            if entry:
                entry["transport"] = transport
                entry["protocol"] = protocol

            while True:
                entry = self._tasks.get(sid)
                if entry is None or not entry.get("running"):
                    break
                await entry["event"].wait()
                entry["event"].clear()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Source %s crashed: %s", name, e)
        finally:
            if protocol is not None and hasattr(protocol, "_log_fh_flush"):
                try:
                    protocol._log_fh_flush()
                except Exception:
                    pass
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass
            for out, _ in out_sockets:
                try:
                    out.close()
                except Exception:
                    pass
            if log_fh:
                try:
                    log_fh.close()
                except Exception:
                    pass
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            self._tasks.pop(sid, None)
            logger.info("Source %s stopped", name)

    async def _gc_loop(self):
        while True:
            await asyncio.sleep(60)
            to_remove = []
            for sid, entry in list(self._tasks.items()):
                if not entry.get("running") and entry.get("task") and entry["task"].done():
                    to_remove.append(sid)
            for sid in to_remove:
                self._tasks.pop(sid, None)
                logger.debug("GC removed orphaned entry for source %s", sid)


engine = ForwarderEngine()
