"""
blockchain.py
Data Logging Algorithm — the tamper-evident ledger.

Implements a hash chain: every entry stores sha256(previous_hash +
event payload + timestamp), so altering any historical row changes
its hash and breaks the chain for every entry after it. This gives
the two properties the thesis scope actually asks for — data
integrity and tamper-resistant storage — without standing up a full
distributed blockchain node, which is out of scope for a pilot
running on a single household's hardware.

Genesis hash is a fixed constant so the very first entry for a
patient still has a defined "previous hash" to build on.
"""
import hashlib
import json
from datetime import datetime, timezone

from database import get_conn

GENESIS_HASH = "0" * 64


def _compute_hash(prev_hash: str, patient_id: str, event_type: str, payload: dict, timestamp: str) -> str:
    payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    raw = f"{prev_hash}|{patient_id}|{event_type}|{payload_str}|{timestamp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def append_entry(patient_id: str, event_type: str, payload: dict) -> dict:
    """Append a new tamper-evident entry to the patient's ledger.

    Called by the Alert Algorithm and Adherence Detection Algorithm
    whenever a dose is dispensed/confirmed/missed, a vital reading is
    logged, or a caregiver alert is raised.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT hash FROM ledger WHERE patient_id = ? ORDER BY id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
        prev_hash = row["hash"] if row else GENESIS_HASH

        new_hash = _compute_hash(prev_hash, patient_id, event_type, payload, timestamp)

        cur = conn.execute(
            """INSERT INTO ledger (patient_id, event_type, payload, prev_hash, hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (patient_id, event_type, json.dumps(payload), prev_hash, new_hash, timestamp),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "patient_id": patient_id,
            "event_type": event_type,
            "payload": payload,
            "prev_hash": prev_hash,
            "hash": new_hash,
            "created_at": timestamp,
        }


def get_log(patient_id: str, limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, event_type, payload, prev_hash, hash, created_at
               FROM ledger WHERE patient_id = ? ORDER BY id DESC LIMIT ?""",
            (patient_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def verify_chain(patient_id: str) -> dict:
    """Recompute every hash in order and confirm the chain is unbroken.

    Returns {"valid": bool, "broken_at": id | None}. Exposed as an
    endpoint so a healthcare provider (or your thesis defense demo)
    can show the ledger is verifiably untampered, not just labeled
    "Verified" in the UI.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, event_type, payload, prev_hash, hash, created_at
               FROM ledger WHERE patient_id = ? ORDER BY id ASC""",
            (patient_id,),
        ).fetchall()

    expected_prev = GENESIS_HASH
    for row in rows:
        payload = json.loads(row["payload"])
        recomputed = _compute_hash(expected_prev, patient_id, row["event_type"], payload, row["created_at"])
        if row["prev_hash"] != expected_prev or row["hash"] != recomputed:
            return {"valid": False, "broken_at": row["id"]}
        expected_prev = row["hash"]

    return {"valid": True, "broken_at": None}
