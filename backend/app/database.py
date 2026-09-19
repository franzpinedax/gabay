"""
database.py
Connection helper + schema definition for the Gabay backend.

Uses Python's built-in sqlite3 module — zero external dependencies.
This keeps the storage layer simple and fully testable without pip
installs, and is more than sufficient for a pilot/prototype deployment
(Scope: "prototype or pilot implementation in a controlled or
simulated environment").

To move to PostgreSQL later for a multi-household deployment, only
this file needs to change (swap sqlite3 for psycopg2 + connection
string) — every other module talks to the database only through the
functions defined here.
"""
import sqlite3
import os
import re
import secrets
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gabay.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    nickname        TEXT,
    age             INTEGER,
    condition       TEXT,
    caregiver_name  TEXT,
    status          TEXT NOT NULL DEFAULT 'good',   -- good | attention | critical
    reminder_type   TEXT NOT NULL DEFAULT 'beep',   -- beep | voice
    reminder_audio_filename TEXT,                   -- set when reminder_type='voice'
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS medication_schedule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      TEXT NOT NULL REFERENCES patients(id),
    med_name        TEXT NOT NULL,
    dosage          TEXT NOT NULL,
    scheduled_time  TEXT NOT NULL,   -- 'HH:MM' 24h format
    active          INTEGER NOT NULL DEFAULT 1
);

-- One row per *scheduled instance* of a dose (generated daily from
-- medication_schedule by the Reminder Algorithm in analytics.py)
CREATE TABLE IF NOT EXISTS dispense_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id          TEXT NOT NULL REFERENCES patients(id),
    schedule_id         INTEGER NOT NULL REFERENCES medication_schedule(id),
    med_name            TEXT NOT NULL,
    dosage              TEXT NOT NULL,
    scheduled_datetime  TEXT NOT NULL,   -- ISO datetime
    dispensed_at        TEXT,            -- set when the mechanism actuates
    confirmed_at        TEXT,            -- set when intake is confirmed
    status              TEXT NOT NULL DEFAULT 'pending',
        -- pending | dispensed | confirmed | missed
    confirm_source      TEXT             -- dispenser_sensor | wearable_gesture | manual
);

CREATE TABLE IF NOT EXISTS vitals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id  TEXT NOT NULL REFERENCES patients(id),
    hr          REAL,
    spo2        REAL,
    temp        REAL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id  TEXT NOT NULL REFERENCES patients(id),
    type        TEXT NOT NULL,   -- missed | warning | info | critical
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved    INTEGER NOT NULL DEFAULT 0
);

-- Hash-chained ledger: a lightweight, dependency-free stand-in for a
-- blockchain that still gives the two properties that matter for this
-- thesis's scope (data integrity + tamper-evidence), without running a
-- full distributed node. See blockchain.py for the chaining logic and
-- README.md for the tradeoffs versus a real distributed ledger.
CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id  TEXT NOT NULL REFERENCES patients(id),
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL,   -- JSON string
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db():
    """Create all tables if they don't already exist."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def get_conn():
    """Yield a sqlite3 connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def reset_db():
    """Danger: drops and recreates all tables. Used by seed.py for demos."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()


def generate_patient_id(name: str, conn) -> str:
    """Builds a readable, unique patient id from their name, e.g.
    "Juan Dela Cruz" -> "juan-dela-cruz-4f2a". Takes an open connection
    so the uniqueness check happens in the same transaction as the
    insert that follows it."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "patient"
    for _ in range(20):
        candidate = f"{slug}-{secrets.token_hex(2)}"
        exists = conn.execute("SELECT 1 FROM patients WHERE id=?", (candidate,)).fetchone()
        if not exists:
            return candidate
    raise RuntimeError("Could not generate a unique patient id — this should never happen")
