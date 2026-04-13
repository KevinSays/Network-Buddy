"""
Traffic log and device session store (SQLite).

Tables:
  traffic_log      — one bandwidth sample per device per LOG_INTERVAL seconds
  wan_log          — one WAN sample per LOG_INTERVAL seconds
  device_sessions  — one row per contiguous online/offline session
  device_aliases   — user-set friendly names, keyed by IP

A session is "transient" when the device was online for less than
TRANSIENT_THRESHOLD_SECONDS before disconnecting.
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

DB_PATH = Path(__file__).parent.parent / "mhs.db"

RETENTION_DAYS               = 7    # purge traffic_log / wan_log rows older than this
STALE_THRESHOLD_SECONDS      = 180  # close a session after 3 min without a ping
TRANSIENT_THRESHOLD_SECONDS  = 300  # sessions under 5 min are flagged "transient"

_write_lock = threading.Lock()


@contextmanager
def _conn():
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS traffic_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL    NOT NULL,
                ip            TEXT    NOT NULL,
                mac           TEXT    NOT NULL DEFAULT '',
                hostname      TEXT    NOT NULL DEFAULT '',
                upload_bps    REAL    NOT NULL DEFAULT 0,
                download_bps  REAL    NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_tl_ip_ts ON traffic_log(ip, ts);
            CREATE INDEX IF NOT EXISTS idx_tl_ts    ON traffic_log(ts);

            CREATE TABLE IF NOT EXISTS wan_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            REAL    NOT NULL,
                download_bps  REAL    NOT NULL DEFAULT 0,
                upload_bps    REAL    NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_wl_ts ON wan_log(ts);

            CREATE TABLE IF NOT EXISTS device_sessions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ip            TEXT    NOT NULL,
                mac           TEXT    NOT NULL DEFAULT '',
                hostname      TEXT    NOT NULL DEFAULT '',
                vendor        TEXT    NOT NULL DEFAULT '',
                first_seen    REAL    NOT NULL,
                last_seen     REAL    NOT NULL,
                active        INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_ds_ip     ON device_sessions(ip);
            CREATE INDEX IF NOT EXISTS idx_ds_active ON device_sessions(active, last_seen);

            CREATE TABLE IF NOT EXISTS device_aliases (
                ip         TEXT PRIMARY KEY,
                alias      TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
        """)


# ---------------------------------------------------------------------------
# Traffic log
# ---------------------------------------------------------------------------

def log_traffic(devices: List[Dict]) -> None:
    """Insert one bandwidth row per device; prune rows older than RETENTION_DAYS."""
    if not devices:
        return
    now = time.time()
    rows = [
        (
            now,
            d.get("ip", ""),
            d.get("mac", ""),
            d.get("hostname", ""),
            float(d.get("upload_bps", 0)),
            float(d.get("download_bps", 0)),
        )
        for d in devices
    ]
    cutoff = now - RETENTION_DAYS * 86_400
    with _write_lock, _conn() as c:
        c.executemany(
            "INSERT INTO traffic_log(ts,ip,mac,hostname,upload_bps,download_bps) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        c.execute("DELETE FROM traffic_log WHERE ts < ?", (cutoff,))


def get_history(ip: str, minutes: int = 60) -> List[Dict]:
    """Return traffic samples for *ip* over the last *minutes* minutes."""
    cutoff = time.time() - minutes * 60
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, upload_bps, download_bps "
            "FROM traffic_log WHERE ip=? AND ts>=? ORDER BY ts ASC",
            (ip, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# WAN log
# ---------------------------------------------------------------------------

def log_wan(download_bps: float, upload_bps: float) -> None:
    """Insert one WAN sample; prune rows older than RETENTION_DAYS."""
    now = time.time()
    cutoff = now - RETENTION_DAYS * 86_400
    with _write_lock, _conn() as c:
        c.execute(
            "INSERT INTO wan_log(ts, download_bps, upload_bps) VALUES(?,?,?)",
            (now, float(download_bps), float(upload_bps)),
        )
        c.execute("DELETE FROM wan_log WHERE ts < ?", (cutoff,))


def get_wan_history(minutes: int = 60) -> List[Dict]:
    """Return WAN samples over the last *minutes* minutes."""
    cutoff = time.time() - minutes * 60
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, download_bps, upload_bps "
            "FROM wan_log WHERE ts>=? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Device sessions
# ---------------------------------------------------------------------------

def update_sessions(current_ips: set, device_map: Dict[str, Dict]) -> List[Dict]:
    """
    Sync device_sessions with the current online set:
      - Bump last_seen for devices still present.
      - Open a new session for newly-seen devices.
      - Close sessions not updated within STALE_THRESHOLD_SECONDS.

    Returns a list of device dicts for IPs that are brand-new (first time ever seen).
    """
    now = time.time()
    new_devices: List[Dict] = []

    with _write_lock, _conn() as c:
        active = {
            r["ip"]: r["id"]
            for r in c.execute(
                "SELECT id, ip FROM device_sessions WHERE active=1"
            ).fetchall()
        }

        # Bump last_seen for devices still online
        for ip in current_ips & set(active):
            d = device_map.get(ip, {})
            c.execute(
                "UPDATE device_sessions SET last_seen=?, hostname=? WHERE id=?",
                (now, d.get("hostname", ""), active[ip]),
            )

        # Open sessions for new arrivals
        for ip in current_ips - set(active):
            d = device_map.get(ip, {})
            c.execute(
                "INSERT INTO device_sessions"
                "(ip,mac,hostname,vendor,first_seen,last_seen,active) "
                "VALUES (?,?,?,?,?,?,1)",
                (ip, d.get("mac", ""), d.get("hostname", ""),
                 d.get("vendor", ""), now, now),
            )
            # Brand-new device if this is the only session ever for this IP
            total = c.execute(
                "SELECT COUNT(*) FROM device_sessions WHERE ip=?", (ip,)
            ).fetchone()[0]
            if total == 1:
                new_devices.append(device_map.get(ip, {"ip": ip}))

        # Close sessions that have gone stale
        c.execute(
            "UPDATE device_sessions SET active=0 "
            "WHERE active=1 AND last_seen < ?",
            (now - STALE_THRESHOLD_SECONDS,),
        )

    return new_devices


def get_device_seen_times() -> Dict[str, Dict]:
    """Return {ip: {first_seen, last_seen}} from all historical sessions."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT ip,
                   MIN(first_seen) AS first_seen,
                   MAX(last_seen)  AS last_seen
            FROM   device_sessions
            GROUP  BY ip
            """
        ).fetchall()
    return {r["ip"]: {"first_seen": r["first_seen"], "last_seen": r["last_seen"]}
            for r in rows}


def get_transient_devices(hours: int = 24) -> List[Dict]:
    """
    Return closed sessions from the last *hours* hours where the device was
    online for less than TRANSIENT_THRESHOLD_SECONDS.
    """
    cutoff = time.time() - hours * 3_600
    with _conn() as c:
        rows = c.execute(
            """
            SELECT ip, mac, hostname, vendor, first_seen, last_seen,
                   (last_seen - first_seen) AS duration_seconds
            FROM   device_sessions
            WHERE  active = 0
              AND  last_seen  >= ?
              AND  (last_seen - first_seen) < ?
            ORDER  BY last_seen DESC
            """,
            (cutoff, TRANSIENT_THRESHOLD_SECONDS),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Device aliases
# ---------------------------------------------------------------------------

def get_aliases() -> Dict[str, str]:
    """Return {ip: alias} for all stored aliases."""
    with _conn() as c:
        rows = c.execute("SELECT ip, alias FROM device_aliases").fetchall()
    return {r["ip"]: r["alias"] for r in rows}


def set_alias(ip: str, alias: str) -> None:
    """Upsert a friendly name for *ip*."""
    now = time.time()
    with _write_lock, _conn() as c:
        c.execute(
            "INSERT INTO device_aliases(ip, alias, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(ip) DO UPDATE SET alias=excluded.alias, updated_at=excluded.updated_at",
            (ip, alias.strip(), now),
        )


def delete_alias(ip: str) -> None:
    """Remove the alias for *ip* (revert to router-assigned hostname)."""
    with _write_lock, _conn() as c:
        c.execute("DELETE FROM device_aliases WHERE ip=?", (ip,))
