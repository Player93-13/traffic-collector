#!/usr/bin/env python3

import os
import time
import json
import subprocess
import psycopg2
import logging
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS")
}

INTERVAL = int(os.getenv("INTERVAL", "60"))
XRAY_API = os.getenv("XRAY_API")
XRAY_BIN = os.getenv("XRAY_BIN", "xray")
WG_CONTAINER = os.getenv("WG_CONTAINER")
WG_INTERFACE = os.getenv("WG_INTERFACE")
WG_CLIENTS_PATH = os.getenv("WG_CLIENTS_PATH")
HEALTH_BIND = os.getenv("HEALTH_BIND", "127.0.0.1")
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "9229"))

running = True

def handle_signal(signum, frame):
    global running
    logger.info("Received signal %s, shutting down...", signum)
    running = False

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# Simple health HTTP server
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

def start_health_server():
    try:
        server = HTTPServer((HEALTH_BIND, HEALTH_PORT), HealthHandler)
    except Exception as e:
        logger.warning("Health server failed to start: %s", e)
        return None

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Health server listening on %s:%d", HEALTH_BIND, HEALTH_PORT)
    return server

# ---------------- DB ----------------
def db():
    # Basic connect with raising exception to be handled by caller
    return psycopg2.connect(**DB_CONFIG)

def init_db(conn):
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        source TEXT,
        external_id TEXT,
        name TEXT,
        UNIQUE(source, external_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stats (
        ts INTEGER,
        user_id INTEGER,
        rx BIGINT,
        tx BIGINT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS last_stats (
        user_id INTEGER PRIMARY KEY,
        rx BIGINT,
        tx BIGINT
    )
    """)
    cur.close()

# ---------------- helpers ----------------
def get_last(cur, user_id):
    cur.execute("SELECT rx, tx FROM last_stats WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    return row if row else (0, 0)

def update_last(cur, user_id, rx, tx):
    cur.execute("""
    INSERT INTO last_stats (user_id, rx, tx)
    VALUES (%s, %s, %s)
    ON CONFLICT (user_id)
    DO UPDATE SET rx=EXCLUDED.rx, tx=EXCLUDED.tx
    """, (user_id, rx, tx))

def build_cache(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, source, external_id FROM users")
    cache = {(src, ext): uid for uid, src, ext in cur.fetchall()}
    cur.close()
    return cache

# ---------------- XRAY ----------------
def run_cmd(cmd):
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            logger.debug("Command failed: %s ; stderr: %s", cmd, out.stderr.strip())
            return None
        return out.stdout
    except Exception as e:
        logger.debug("Command exception: %s -> %s", cmd, e)
        return None

def is_xray_available():
    if not XRAY_API:
        return False
    out = run_cmd(f"{XRAY_BIN} api statsquery --server={XRAY_API}")
    return out is not None

def get_xray_stats():
    cmd = f"{XRAY_BIN} api statsquery --server={XRAY_API}"
    output = run_cmd(cmd)
    if not output:
        return {}
    try:
        data = json.loads(output)
    except Exception as e:
        logger.warning("Failed to parse xray output: %s", e)
        return {}
    users = {}
    for item in data.get("stat", []):
        name = item.get("name", "")
        value = item.get("value", 0)
        if name.startswith("user>>>"):
            parts = name.split(">>>")
            if len(parts) >= 4:
                _, user, _, direction = parts[:4]
                users.setdefault(user, {"uplink": 0, "downlink": 0})
                users[user][direction] = value
    return users

def sync_xray_users(conn, users):
    cur = conn.cursor()
    for user in users:
        cur.execute("""
        INSERT INTO users (source, external_id, name)
        VALUES ('xray', %s, %s)
        ON CONFLICT (source, external_id)
        DO UPDATE SET name=EXCLUDED.name
        """, (user, user))
    cur.close()

def collect_xray(conn, cache):
    data = get_xray_stats()
    ts = int(time.time())
    cur = conn.cursor()
    for user, stats in data.items():
        key = ("xray", user)
        if key not in cache:
            continue
        user_id = cache[key]
        rx = stats.get("downlink", 0)
        tx = stats.get("uplink", 0)
        last_rx, last_tx = get_last(cur, user_id)
        delta_rx = max(0, rx - last_rx)
        delta_tx = max(0, tx - last_tx)
        if delta_rx or delta_tx:
            cur.execute("INSERT INTO stats VALUES (%s, %s, %s, %s)", (ts, user_id, int(delta_rx), int(delta_tx)))
        update_last(cur, user_id, rx, tx)
    cur.close()

# ---------------- WG ----------------
def is_wg_available():
    if not WG_CONTAINER or not WG_INTERFACE:
        return False
    out = run_cmd(f"docker exec {WG_CONTAINER} wg show")
    return out is not None

def collect_wg(conn, cache):
    ts = int(time.time())
    cur = conn.cursor()
    cmd = f"docker exec {WG_CONTAINER} wg show {WG_INTERFACE}"
    output = run_cmd(cmd)
    if not output:
        logger.debug("wg show returned no output")
        cur.close()
        return
    peer = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("peer:"):
            parts = line.split()
            if len(parts) >= 2:
                peer = parts[1]
            else:
                peer = None
        elif "transfer:" in line and peer:
            try:
                parts = line.split("transfer:")[1].split(",")
                rx = int(parts[0].strip().split()[0])
                tx = int(parts[1].strip().split()[0])
            except Exception:
                continue
            key = ("wg", peer)
            if key not in cache:
                continue
            user_id = cache[key]
            last_rx, last_tx = get_last(cur, user_id)
            delta_rx = max(0, rx - last_rx)
            delta_tx = max(0, tx - last_tx)
            if delta_rx or delta_tx:
                cur.execute("INSERT INTO stats VALUES (%s, %s, %s, %s)", (ts, user_id, delta_rx, delta_tx))
            update_last(cur, user_id, rx, tx)
    cur.close()

# ---------------- MAIN ----------------
def main():
    server = start_health_server()
    backoff = 1
    global running
    while running:
        conn = None
        try:
            conn = db()
            conn.autocommit = True
            init_db(conn)
            cache = build_cache(conn)
            # XRAY
            if is_xray_available():
                xray_users = get_xray_stats()
                if xray_users:
                    sync_xray_users(conn, xray_users)
                    cache = build_cache(conn)
                    collect_xray(conn, cache)
                    logger.info("[XRAY] collected %d users", len(xray_users))
            # WG
            if is_wg_available():
                collect_wg(conn, cache)
                logger.info("[WG] collected")
            backoff = 1
        except Exception as e:
            logger.exception("Main loop error: %s", e)
            # on error, increase backoff but keep running
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        # sleep between cycles but allow fast shutdown
        for _ in range(max(1, int(INTERVAL))):
            if not running:
                break
            time.sleep(1)
    if server:
        try:
            server.shutdown()
        except Exception:
            pass
    logger.info("Exited main loop")

if __name__ == "__main__":
    # basic env check
    if not DB_CONFIG["host"] or not DB_CONFIG["dbname"]:
        logger.warning("DB_HOST/DB_NAME not set; app will fail to connect until configured")
    main()