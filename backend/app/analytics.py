"""
analytics.py
Implements the five algorithms named in Chapter 3.5 of the thesis:
  1. Reminder Algorithm
  2. Adherence Detection Algorithm
  3. Vital Sign Monitoring Algorithm
  4. Alert Algorithm
  5. Data Logging Algorithm -> delegated to blockchain.append_entry()

Every function that changes state also writes a ledger entry, so the
audit trail (Data Logging Algorithm) is a side effect of normal
operation rather than a separate manual step.
"""
from datetime import datetime, timedelta, date, time as dt_time

from database import get_conn
import blockchain


def format_12h(dt: datetime) -> str:
    """Formats a datetime as '7:00 AM' / '12:30 PM' — no leading zero
    on the hour, cross-platform.

    strftime's "no leading zero" flag is NOT portable: Linux/macOS use
    %-I, Windows uses %#I, and using the wrong one raises
    "ValueError: Invalid format string" on the other OS. Computing it
    manually here avoids the platform check entirely.
    """
    hour = dt.hour % 12
    if hour == 0:
        hour = 12
    period = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {period}"


# ---------------------------------------------------------------
# Vital sign thresholds (mirrors the frontend's assessVital logic,
# kept in one place here so the backend is the source of truth)
# ---------------------------------------------------------------
def assess_vital(kind: str, value: float) -> str:
    if value is None:
        return "good"
    if kind == "hr":
        if 60 <= value <= 100:
            return "good"
        if value < 50 or value > 130:
            return "critical"
        return "attention"
    if kind == "spo2":
        if value >= 95:
            return "good"
        if value >= 90:
            return "attention"
        return "critical"
    if kind == "temp":
        if 36.1 <= value <= 37.4:
            return "good"
        if value <= 38.2:
            return "attention"
        return "critical"
    return "good"


TIME_SLOTS = [
    ("Morning", 5, 11),
    ("Afternoon", 11, 17),
    ("Evening", 17, 21),
    ("Night", 21, 29),  # 21:00–23:59 and 00:00–04:59 wrap handled below
]


def _time_slot(hour: int) -> str:
    for name, start, end in TIME_SLOTS:
        if end <= 24:
            if start <= hour < end:
                return name
        else:  # Night wraps past midnight
            if hour >= start or hour < end - 24:
                return name
    return "Night"


# =================================================================
# 1. REMINDER ALGORITHM
# Generates today's concrete dose instances from the recurring
# medication_schedule template. Idempotent — safe to call repeatedly
# (e.g. once at midnight and again on every dashboard load).
# =================================================================
def generate_todays_schedule(patient_id: str, today: date = None) -> list:
    today = today or date.today()
    with get_conn() as conn:
        templates = conn.execute(
            "SELECT * FROM medication_schedule WHERE patient_id = ? AND active = 1",
            (patient_id,),
        ).fetchall()

        for tpl in templates:
            hh, mm = map(int, tpl["scheduled_time"].split(":"))
            scheduled_dt = datetime.combine(today, dt_time(hh, mm)).isoformat()

            exists = conn.execute(
                """SELECT id FROM dispense_events
                   WHERE patient_id = ? AND schedule_id = ? AND scheduled_datetime = ?""",
                (patient_id, tpl["id"], scheduled_dt),
            ).fetchone()

            if not exists:
                conn.execute(
                    """INSERT INTO dispense_events
                       (patient_id, schedule_id, med_name, dosage, scheduled_datetime, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    (patient_id, tpl["id"], tpl["med_name"], tpl["dosage"], scheduled_dt),
                )
        conn.commit()

    return get_today_schedule(patient_id, today)


def get_today_schedule(patient_id: str, today: date = None) -> list:
    """Shaped for the dashboard's MedicationDayStrip: time, label, status."""
    today = today or date.today()
    start = datetime.combine(today, dt_time(0, 0)).isoformat()
    end = datetime.combine(today, dt_time(23, 59, 59)).isoformat()

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM dispense_events
               WHERE patient_id = ? AND scheduled_datetime BETWEEN ? AND ?
               ORDER BY scheduled_datetime ASC""",
            (patient_id, start, end),
        ).fetchall()

    result = []
    next_assigned = False
    for r in rows:
        dt = datetime.fromisoformat(r["scheduled_datetime"])
        if r["status"] == "confirmed":
            status = "taken"
        elif r["status"] == "missed":
            status = "missed"
        else:
            if not next_assigned:
                status = "next"
                next_assigned = True
            else:
                status = "upcoming"
        result.append({
            "id": r["id"],
            "time": format_12h(dt),
            "label": f'{r["med_name"]} {r["dosage"]}',
            "status": status,
        })
    return result


# =================================================================
# 2. ADHERENCE DETECTION ALGORITHM
# =================================================================
def mark_dispensed(dispense_event_id: int) -> dict:
    """Called when the servo/carousel actuates and releases a dose."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE dispense_events SET status='dispensed', dispensed_at=? WHERE id=?",
            (datetime.now().isoformat(), dispense_event_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM dispense_events WHERE id=?", (dispense_event_id,)).fetchone()

    blockchain.append_entry(row["patient_id"], "dose_dispensed", {
        "dispense_event_id": dispense_event_id, "med": row["med_name"], "dosage": row["dosage"],
    })
    return dict(row)


def confirm_dose(dispense_event_id: int, source: str = "wearable_gesture") -> dict:
    """Called when the wearable/sensor confirms the patient actually took it."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE dispense_events SET status='confirmed', confirmed_at=?, confirm_source=? WHERE id=?",
            (datetime.now().isoformat(), source, dispense_event_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM dispense_events WHERE id=?", (dispense_event_id,)).fetchone()

    blockchain.append_entry(row["patient_id"], "dose_confirmed", {
        "dispense_event_id": dispense_event_id, "med": row["med_name"], "source": source,
    })
    update_patient_status(row["patient_id"])
    return dict(row)


def sweep_missed_doses(patient_id: str, grace_minutes: int = 30) -> list:
    """Run periodically (e.g. every 5 min via a scheduler) to flag doses
    that passed their scheduled time plus a grace period without
    confirmation. Raises a caregiver alert for each one found."""
    cutoff = datetime.now() - timedelta(minutes=grace_minutes)
    newly_missed = []

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM dispense_events
               WHERE patient_id = ? AND status IN ('pending','dispensed')
               AND scheduled_datetime <= ?""",
            (patient_id, cutoff.isoformat()),
        ).fetchall()

        for r in rows:
            conn.execute("UPDATE dispense_events SET status='missed' WHERE id=?", (r["id"],))
            newly_missed.append(dict(r))
        conn.commit()

    for r in newly_missed:
        blockchain.append_entry(patient_id, "dose_missed", {
            "dispense_event_id": r["id"], "med": r["med_name"], "scheduled": r["scheduled_datetime"],
        })
        raise_alert(patient_id, "missed", f'Missed {format_12h(datetime.fromisoformat(r["scheduled_datetime"]))} dose — {r["med_name"]} {r["dosage"]}')

    if newly_missed:
        update_patient_status(patient_id)
    return newly_missed


def compute_adherence_rate(patient_id: str, days: int = 7) -> float:
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) c FROM dispense_events WHERE patient_id=? AND scheduled_datetime >= ? AND status != 'pending'",
            (patient_id, since),
        ).fetchone()["c"]
        confirmed = conn.execute(
            "SELECT COUNT(*) c FROM dispense_events WHERE patient_id=? AND scheduled_datetime >= ? AND status='confirmed'",
            (patient_id, since),
        ).fetchone()["c"]
    if total == 0:
        return 100.0
    return round(confirmed / total * 100, 1)


def get_adherence_history(patient_id: str, days: int = 7) -> list:
    """Daily adherence % for the last `days` days — feeds the dashboard's
    7-day and 30-day adherence charts."""
    result = []
    for i in range(days - 1, -1, -1):
        day = date.today() - timedelta(days=i)
        start = datetime.combine(day, dt_time(0, 0)).isoformat()
        end = datetime.combine(day, dt_time(23, 59, 59)).isoformat()
        with get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) c FROM dispense_events WHERE patient_id=? AND scheduled_datetime BETWEEN ? AND ? AND status != 'pending'",
                (patient_id, start, end),
            ).fetchone()["c"]
            confirmed = conn.execute(
                "SELECT COUNT(*) c FROM dispense_events WHERE patient_id=? AND scheduled_datetime BETWEEN ? AND ? AND status='confirmed'",
                (patient_id, start, end),
            ).fetchone()["c"]
        pct = round(confirmed / total * 100) if total else None
        result.append({"day": day.strftime("%a") if days <= 7 else f"D{days - i}", "pct": pct})
    return result


def get_missed_by_timeslot(patient_id: str, days: int = 30) -> list:
    since = (datetime.now() - timedelta(days=days)).isoformat()
    counts = {name: 0 for name, _, _ in TIME_SLOTS}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT scheduled_datetime FROM dispense_events WHERE patient_id=? AND status='missed' AND scheduled_datetime >= ?",
            (patient_id, since),
        ).fetchall()
    for r in rows:
        hour = datetime.fromisoformat(r["scheduled_datetime"]).hour
        counts[_time_slot(hour)] += 1
    return [{"slot": name, "missed": counts[name]} for name, _, _ in TIME_SLOTS]


WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def get_missed_by_weekday(patient_id: str, days: int = 30) -> list:
    """Diagnostic tier: is a specific day of the week riskier? (e.g.
    weekends, when routines and caregiver supervision often change)."""
    since = (datetime.now() - timedelta(days=days)).isoformat()
    counts = [0] * 7
    totals = [0] * 7
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT scheduled_datetime, status FROM dispense_events
               WHERE patient_id=? AND scheduled_datetime >= ? AND status IN ('confirmed','missed')""",
            (patient_id, since),
        ).fetchall()
    for r in rows:
        wd = datetime.fromisoformat(r["scheduled_datetime"]).weekday()
        totals[wd] += 1
        if r["status"] == "missed":
            counts[wd] += 1
    return [
        {"day": WEEKDAY_LABELS[i], "missed": counts[i], "total": totals[i],
         "rate": round(counts[i] / totals[i] * 100, 1) if totals[i] else None}
        for i in range(7)
    ]


def get_missed_by_medication(patient_id: str, days: int = 30) -> list:
    """Diagnostic tier: is one specific medication driving most of the
    non-adherence, versus doses being missed broadly?"""
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT med_name,
                      SUM(CASE WHEN status='missed' THEN 1 ELSE 0 END) missed,
                      COUNT(*) total
               FROM dispense_events
               WHERE patient_id=? AND scheduled_datetime >= ? AND status IN ('confirmed','missed')
               GROUP BY med_name ORDER BY missed DESC""",
            (patient_id, since),
        ).fetchall()
    return [
        {"med_name": r["med_name"], "missed": r["missed"], "total": r["total"],
         "rate": round(r["missed"] / r["total"] * 100, 1) if r["total"] else None}
        for r in rows
    ]


# =================================================================
# 3. VITAL SIGN MONITORING ALGORITHM
# =================================================================
def record_vital(
    patient_id: str,
    hr: float = None,
    spo2: float = None,
    temp: float = None,
    fall_detected: bool = False,
    recorded_at: str = None,
) -> dict:
    # recorded_at is set explicitly here (rather than relying on the
    # column's SQL-level DEFAULT) because SQLite's datetime('now') produces
    # a space-separated timestamp ("2026-09-19 12:55:15"), while every other
    # timestamp in this app is Python-generated ISO format with a "T"
    # separator. Comparing those as plain text in a BETWEEN clause put
    # today's vitals outside the expected range and silently dropped them
    # from trend queries — this keeps the format consistent everywhere.
    now = recorded_at or datetime.now().isoformat()
    with get_conn() as conn:
        conn.execute(
        """INSERT INTO vitals
           (patient_id, hr, spo2, temp, fall_detected, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (patient_id, hr, spo2, temp, int(fall_detected), now),
        )
        conn.commit()

    reading = {
        "hr": hr, "spo2": spo2, "temp": temp,
        "fallDetected": fall_detected, "timestamp": now,
    }
    blockchain.append_entry(patient_id, "vital_reading_logged", reading)
    check_vitals_and_alert(patient_id, hr, spo2, temp, fall_detected)
    return reading


def get_latest_vitals(patient_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM vitals WHERE patient_id=? ORDER BY id DESC LIMIT 1", (patient_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "hr": row["hr"], "spo2": row["spo2"], "temp": row["temp"],
        "fallDetected": bool(row["fall_detected"]),
        "timestamp": row["recorded_at"],
    }


def get_vitals_trend(patient_id: str, days: int = 7) -> list:
    result = []
    for i in range(days - 1, -1, -1):
        day = date.today() - timedelta(days=i)
        start = datetime.combine(day, dt_time(0, 0)).isoformat()
        end = datetime.combine(day, dt_time(23, 59, 59)).isoformat()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT AVG(hr) hr, AVG(spo2) spo2, AVG(temp) temp FROM vitals WHERE patient_id=? AND recorded_at BETWEEN ? AND ?",
                (patient_id, start, end),
            ).fetchone()
        result.append({
            "day": day.strftime("%a"),
            "hr": round(row["hr"]) if row["hr"] is not None else None,
            "spo2": round(row["spo2"]) if row["spo2"] is not None else None,
            "temp": round(row["temp"], 1) if row["temp"] is not None else None,
        })
    return result


# =================================================================
# 4. ALERT ALGORITHM
# =================================================================
def raise_alert(
    patient_id: str,
    alert_type: str,
    message: str,
    recipients: str = "caregiver",
) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (patient_id, type, message, recipients) VALUES (?, ?, ?, ?)",
            (patient_id, alert_type, message, recipients),
        )
        conn.commit()
        alert_id = cur.lastrowid

    blockchain.append_entry(
        patient_id,
        "caregiver_provider_alert_sent" if recipients == "caregiver,provider" else "caregiver_alert_sent",
        {"alert_id": alert_id, "type": alert_type, "message": message, "recipients": recipients},
    )
    return {"id": alert_id, "type": alert_type, "message": message, "recipients": recipients}


def check_vitals_and_alert(
    patient_id: str, hr: float, spo2: float, temp: float, fall_detected: bool = False
):
    if fall_detected:
        raise_alert(
            patient_id,
            "critical",
            "Fall detected by smartwatch — caregiver and healthcare provider notified",
            recipients="caregiver,provider",
        )
        set_patient_status_critical(patient_id)

    checks = [("hr", hr, "Heart rate"), ("spo2", spo2, "Oxygen (SpO2)"), ("temp", temp, "Temperature")]
    for kind, value, label in checks:
        level = assess_vital(kind, value)
        if level == "critical":
            raise_alert(patient_id, "warning", f"{label} reading out of safe range: {value}")
    if any(assess_vital(k, v) == "critical" for k, v, _ in checks):
        update_patient_status(patient_id)


def get_alerts(patient_id: str, limit: int = 10) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE patient_id=? ORDER BY id DESC LIMIT ?",
            (patient_id, limit),
        ).fetchall()

    def relative_time(ts):
        delta = datetime.now() - datetime.fromisoformat(ts)
        mins = int(delta.total_seconds() // 60)
        if mins < 60:
            return f"{max(mins,1)}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"

    return [
        {
            "id": r["id"],
            "type": r["type"],
            "message": r["message"],
            "recipients": r["recipients"],
            "time": relative_time(r["created_at"]),
        }
        for r in rows
    ]


def set_patient_status_critical(patient_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE patients SET status='critical' WHERE id=?", (patient_id,))
        conn.commit()


# ---------------------------------------------------------------
# Patient status rollup — shown as the badge on the dashboard header
# ---------------------------------------------------------------
def update_patient_status(patient_id: str):
    rate = compute_adherence_rate(patient_id, days=7)
    with get_conn() as conn:
        recent_critical = conn.execute(
            "SELECT COUNT(*) c FROM alerts WHERE patient_id=? AND type='missed' AND created_at >= ?",
            (patient_id, (datetime.now() - timedelta(hours=24)).isoformat()),
        ).fetchone()["c"]

    if rate < 70 or recent_critical >= 2:
        status = "critical"
    elif rate < 85 or recent_critical >= 1:
        status = "attention"
    else:
        status = "good"

    with get_conn() as conn:
        conn.execute("UPDATE patients SET status=? WHERE id=?", (status, patient_id))
        conn.commit()
    return status
