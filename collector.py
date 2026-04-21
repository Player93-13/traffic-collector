#!/usr/bin/env python3

import os
import time
import json
import subprocess
import psycopg2

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS")
}

INTERVAL = int(os.getenv("INTERVAL", "60"))

XRAY_API = os.getenv("XRAY_API")
XRAY_BIN = os.getenv("XRAY_BIN")

WG_CONTAINER = os.getenv("WG_CONTAINER")
WG_INTERFACE = os.getenv("WG_INTERFACE")
WG_CLIENTS_PATH = os.getenv("WG_CLIENTS_PATH")


# ---------------- DB ----------------

def db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    return conn


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
    return {(src, ext): uid for uid, src, ext in cur.fetchall()}


# ---------------- XRAY ----------------

def is_xray_available():
    try:
        subprocess.check_output(f"{XRAY_BIN} api statsquery --server={XRAY_API}", shell=True)
        return True
    except:
        return False


def get_xray_stats():
    cmd = f"{XRAY_BIN} api statsquery --server={XRAY_API}"
    output = subprocess.check_output(cmd, shell=True).decode()
    data = json.loads(output)

    users = {}

    for item in data.get("stat", []):
        name = item["name"]
        value = item.get("value", 0)

        if name.startswith("user>>>"):
            _, user, _, direction = name.split(">>>")

            if user not in users:
                users[user] = {"uplink": 0, "downlink": 0}

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


def collect_xray(conn, cache):
    data = get_xray_stats()
    ts = int(time.time())
    cur = conn.cursor()

    for user, stats in data.items():
        key = ("xray", user)
        if key not in cache:
            continue

        user_id = cache[key]

        rx = stats["downlink"]
        tx = stats["uplink"]

        last_rx, last_tx = get_last(cur, user_id)

        delta_rx = max(0, rx - last_rx)
        delta_tx = max(0, tx - last_tx)

        if delta_rx or delta_tx:
            cur.execute(
                "INSERT INTO stats VALUES (%s, %s, %s, %s)",
                (ts, user_id, int(delta_rx), int(delta_tx))
            )

        update_last(cur, user_id, rx, tx)


# ---------------- WG ----------------

def is_wg_available():
    try:
        subprocess.check_output(f"docker exec {WG_CONTAINER} wg show", shell=True)
        return True
    except:
        return False


def collect_wg(conn, cache):
    ts = int(time.time())
    cur = conn.cursor()

    cmd = f"docker exec {WG_CONTAINER} wg show {WG_INTERFACE}"
    output = subprocess.check_output(cmd, shell=True).decode()

    peer = None

    for line in output.split("\n"):
        line = line.strip()

        if line.startswith("peer:"):
            peer = line.split()[1]

        elif "transfer:" in line and peer:
            parts = line.split("transfer:")[1].split(",")

            rx = int(parts[0].strip().split()[0])
            tx = int(parts[1].strip().split()[0])

            key = ("wg", peer)
            if key not in cache:
                continue

            user_id = cache[key]

            last_rx, last_tx = get_last(cur, user_id)

            delta_rx = max(0, rx - last_rx)
            delta_tx = max(0, tx - last_tx)

            if delta_rx or delta_tx:
                cur.execute(
                    "INSERT INTO stats VALUES (%s, %s, %s, %s)",
                    (ts, user_id, delta_rx, delta_tx)
                )

            update_last(cur, user_id, rx, tx)


# ---------------- MAIN ----------------

def main():
    while True:
        try:
            conn = db()
            init_db(conn)

            cache = build_cache(conn)

            if is_xray_available():
                xray_users = get_xray_stats()
                sync_xray_users(conn, xray_users)
                cache = build_cache(conn)
                collect_xray(conn, cache)
                print("[XRAY] collected")

            if is_wg_available():
                collect_wg(conn, cache)
                print("[WG] collected")

            conn.close()

        except Exception as e:
            print("ERROR:", e)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()