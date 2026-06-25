import sqlite3
import threading
from typing import Optional

DB_PATH = "/opt/nmea-web/nmea.db"

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()
_db_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
                _conn.row_factory = sqlite3.Row
                _conn.execute("PRAGMA journal_mode=WAL")
                _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db():
    with _db_lock:
        conn = _get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE NOT NULL,
                listen_ip   TEXT NOT NULL DEFAULT '0.0.0.0',
                listen_port INTEGER NOT NULL,
                interface   TEXT,
                enabled     INTEGER NOT NULL DEFAULT 0,
                log_enabled INTEGER NOT NULL DEFAULT 0,
                log_file    TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS destinations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                bind_ip    TEXT,
                dest_ip    TEXT NOT NULL,
                dest_port  INTEGER NOT NULL,
                prefix     INTEGER DEFAULT 24,
                mode       TEXT NOT NULL DEFAULT 'unicast'
            );
        """)
        conn.commit()


def get_all_sources() -> list[dict]:
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT s.id AS s_id, s.name AS s_name, s.listen_ip, s.listen_port, "
            "s.interface, s.enabled, s.log_enabled, s.log_file, "
            "s.created_at AS s_created_at, s.updated_at AS s_updated_at, "
            "d.id AS d_id, d.source_id, d.bind_ip, d.dest_ip, d.dest_port, d.prefix, d.mode "
            "FROM sources s LEFT JOIN destinations d ON s.id = d.source_id "
            "ORDER BY s.name"
        ).fetchall()

        sources: dict[int, dict] = {}
        for row in rows:
            sid = row["s_id"]
            if sid not in sources:
                sources[sid] = {
                    "id": sid,
                    "name": row["s_name"],
                    "listen_ip": row["listen_ip"],
                    "listen_port": row["listen_port"],
                    "interface": row["interface"],
                    "enabled": bool(row["enabled"]),
                    "log_enabled": bool(row["log_enabled"]),
                    "log_file": row["log_file"],
                    "created_at": row["s_created_at"],
                    "updated_at": row["s_updated_at"],
                    "destinations": [],
                }
            if row["d_id"] is not None:
                sources[sid]["destinations"].append({
                    "id": row["d_id"],
                    "source_id": row["source_id"],
                    "bind_ip": row["bind_ip"],
                    "dest_ip": row["dest_ip"],
                    "dest_port": row["dest_port"],
                    "prefix": row["prefix"],
                    "mode": row["mode"],
                })
        return list(sources.values())


def get_source(source_id: int) -> Optional[dict]:
    with _db_lock:
        conn = _get_conn()
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return None
        src = dict(row)
        src["enabled"] = bool(src["enabled"])
        src["log_enabled"] = bool(src["log_enabled"])
        src["destinations"] = [
            dict(d)
            for d in conn.execute(
                "SELECT * FROM destinations WHERE source_id = ?", (source_id,)
            ).fetchall()
        ]
        return src


def create_source(data: dict) -> dict:
    with _db_lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO sources (name, listen_ip, listen_port, interface, enabled, log_enabled, log_file)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (
                data["name"],
                data.get("listen_ip", "0.0.0.0"),
                data["listen_port"],
                data.get("interface", None),
                1 if data.get("log_enabled") else 0,
                data.get("log_file", None),
            ),
        )
        source_id = cur.lastrowid

        for dest in data.get("destinations", []):
            conn.execute(
                """INSERT INTO destinations (source_id, bind_ip, dest_ip, dest_port, prefix, mode)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (source_id, dest.get("bind_ip"), dest["dest_ip"], dest["dest_port"],
                 dest.get("prefix", 24), dest.get("mode", "unicast")),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        src = dict(row)
        src["enabled"] = bool(src["enabled"])
        src["log_enabled"] = bool(src["log_enabled"])
        src["destinations"] = [
            dict(d)
            for d in conn.execute(
                "SELECT * FROM destinations WHERE source_id = ?", (source_id,)
            ).fetchall()
        ]
        return src


def update_source(source_id: int, data: dict) -> Optional[dict]:
    with _db_lock:
        conn = _get_conn()
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return None

        fields = []
        values = []
        for key in ("name", "listen_ip", "listen_port", "interface", "log_enabled", "log_file", "enabled"):
            if key in data:
                if key in ("log_enabled", "enabled"):
                    fields.append(f"{key} = ?")
                    values.append(1 if data[key] else 0)
                else:
                    fields.append(f"{key} = ?")
                    values.append(data[key])

        if fields:
            fields.append("updated_at = datetime('now')")
            values.append(source_id)
            conn.execute(f"UPDATE sources SET {', '.join(fields)} WHERE id = ?", values)

        if "destinations" in data and data["destinations"] is not None:
            conn.execute("DELETE FROM destinations WHERE source_id = ?", (source_id,))
            for dest in data["destinations"]:
                conn.execute(
                    """INSERT INTO destinations (source_id, bind_ip, dest_ip, dest_port, prefix, mode)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (source_id, dest.get("bind_ip"), dest["dest_ip"], dest["dest_port"],
                     dest.get("prefix", 24), dest.get("mode", "unicast")),
                )

        conn.commit()
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        src = dict(row)
        src["enabled"] = bool(src["enabled"])
        src["log_enabled"] = bool(src["log_enabled"])
        src["destinations"] = [
            dict(d)
            for d in conn.execute(
                "SELECT * FROM destinations WHERE source_id = ?", (source_id,)
            ).fetchall()
        ]
        return src


def delete_source(source_id: int) -> bool:
    with _db_lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted


def set_enabled(source_id: int, enabled: bool):
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE sources SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if enabled else 0, source_id),
        )
        conn.commit()
