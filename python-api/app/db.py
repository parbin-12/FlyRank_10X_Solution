"""
Database layer — Concept #2: Database (real persistence).

Uses Python's built-in sqlite3 module writing to a file on disk, so data
survives a process restart. No external DB server needed (fits the $0,
no-credit-card constraint), but it is a real SQL database with real schema,
indexes, and transactions — not an in-memory mock.
"""
import sqlite3
import os
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("PULSELOG_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "pulselog.db"))


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL REFERENCES services(id),
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_service_ts ON events(service_id, ts);

            CREATE TABLE IF NOT EXISTS llm_cost_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER,
                input_chars INTEGER NOT NULL,
                output_chars INTEGER NOT NULL,
                estimated_cost_usd REAL NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )


def seed_demo_data():
    """Seed script — lets a stranger see the system work without manual setup."""
    with db() as conn:
        existing = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        if existing > 0:
            return  # already seeded

        import hashlib

        def hash_pw(pw, salt):
            return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()

        salt = "demo-salt"
        conn.execute(
            "INSERT INTO users (email, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            ("demo@pulselog.dev", hash_pw("demo1234", salt), salt, time.time()),
        )
        user_id = conn.execute("SELECT id FROM users WHERE email = ?", ("demo@pulselog.dev",)).fetchone()["id"]

        conn.execute(
            "INSERT INTO services (owner_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, "checkout-api", time.time()),
        )
        service_id = conn.execute("SELECT id FROM services WHERE name = ?", ("checkout-api",)).fetchone()["id"]

        import random

        now = time.time()
        levels = ["INFO"] * 70 + ["WARN"] * 20 + ["ERROR"] * 10
        messages = {
            "INFO": ["request handled", "cache warm", "health check ok"],
            "WARN": ["latency above 500ms", "retrying upstream call", "queue depth rising"],
            "ERROR": ["upstream timeout on payment gateway", "db connection reset", "5xx returned to client"],
        }
        rows = []
        for i in range(500):
            level = random.choice(levels)
            msg = random.choice(messages[level])
            ts = now - random.uniform(0, 7 * 24 * 3600)  # spread over last 7 days
            rows.append((service_id, level, msg, ts))
        conn.executemany(
            "INSERT INTO events (service_id, level, message, ts) VALUES (?, ?, ?, ?)", rows
        )
