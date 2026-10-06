"""
seed.py
Populates the database with sample patients, medication schedules, and
90 days of backfilled history (past doses + vitals) so the dashboard
charts — and the predictive risk model in predictive.py — have
something meaningful to learn from, before real hardware is connected.

Miss probability is deliberately NOT flat/random here: it varies by
time-of-day and weekday per patient, mirroring what the literature
review describes (evening/night doses and weekends are commonly
riskier for adherence in cognitively impaired patients). This gives
the predictive model actual signal to find, rather than pure noise.

Run directly: `python seed.py`
"""
import random
from datetime import datetime, timedelta, time as dt_time

from database import reset_db, get_conn
import analytics
import blockchain

PATIENTS = [
    dict(id="p1", name="Franz Pineda", nickname="Franz", age=31,
         condition="Medication Monitoring", caregiver_name="Caregiver", status="good"),
]

SCHEDULES = {
    "p1": [
        ("Metformin", "500mg", "08:00"),
        ("Atorvastatin", "20mg", "13:00"),
        ("Vitamin D", "1000IU", "20:00"),
    ],
}

# Single-patient test data for local development and validation.
TIME_SLOT_ADHERENCE = {
    "p1": {"Morning": 0.94, "Afternoon": 0.91, "Evening": 0.90, "Night": 0.88},
}
WEEKEND_PENALTY = 0.08  # adherence probability drops by this much on Sat/Sun

VITAL_BASELINE = {
    "p1": dict(hr=78, spo2=97),
}


def _time_slot(hour: int) -> str:
    if 5 <= hour < 11:
        return "Morning"
    if 11 <= hour < 17:
        return "Afternoon"
    if 17 <= hour < 21:
        return "Evening"
    return "Night"


def backfill_history(patient_id: str, days: int = 90):
    slot_rates = TIME_SLOT_ADHERENCE.get(patient_id, {"Morning": 0.9, "Afternoon": 0.9, "Evening": 0.85, "Night": 0.85})
    baseline = VITAL_BASELINE.get(patient_id, dict(hr=75, spo2=97))

    with get_conn() as conn:
        templates = conn.execute(
            "SELECT * FROM medication_schedule WHERE patient_id=? AND active=1", (patient_id,)
        ).fetchall()

        for i in range(days, 0, -1):
            day = datetime.now().date() - timedelta(days=i)
            is_weekend = day.weekday() >= 5  # Sat=5, Sun=6

            for tpl in templates:
                hh, mm = map(int, tpl["scheduled_time"].split(":"))
                scheduled_dt = datetime.combine(day, dt_time(hh, mm))
                slot = _time_slot(hh)
                p_take = slot_rates[slot] - (WEEKEND_PENALTY if is_weekend else 0)
                taken = random.random() < max(0.05, p_take)

                status = "confirmed" if taken else "missed"
                confirmed_at = (scheduled_dt + timedelta(minutes=random.randint(1, 20))).isoformat() if taken else None

                conn.execute(
                    """INSERT INTO dispense_events
                       (patient_id, schedule_id, med_name, dosage, scheduled_datetime,
                        dispensed_at, confirmed_at, status, confirm_source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (patient_id, tpl["id"], tpl["med_name"], tpl["dosage"], scheduled_dt.isoformat(),
                     scheduled_dt.isoformat(),
                     confirmed_at, status, "wearable_gesture" if taken else None),
                )

            # A few vital readings per day, jittered around baseline
            for hour in (8, 14, 20):
                ts = datetime.combine(day, dt_time(hour, 0)) + timedelta(minutes=random.randint(0, 45))
                conn.execute(
                    "INSERT INTO vitals (patient_id, hr, spo2, recorded_at) VALUES (?, ?, ?, ?)",
                    (patient_id,
                     round(baseline["hr"] + random.uniform(-6, 6)),
                     min(100, round(baseline["spo2"] + random.uniform(-2, 1.5))),
                     ts.isoformat()),
                )
        conn.commit()


def run():
    reset_db()
    with get_conn() as conn:
        for p in PATIENTS:
            conn.execute(
                """INSERT INTO patients (id, name, nickname, age, condition, caregiver_name, status)
                   VALUES (:id, :name, :nickname, :age, :condition, :caregiver_name, :status)""",
                p,
            )
        for patient_id, meds in SCHEDULES.items():
            for med_name, dosage, time_str in meds:
                conn.execute(
                    """INSERT INTO medication_schedule (patient_id, med_name, dosage, scheduled_time)
                       VALUES (?, ?, ?, ?)""",
                    (patient_id, med_name, dosage, time_str),
                )
        conn.commit()

    for patient_id in SCHEDULES:
        backfill_history(patient_id, days=90)
        analytics.generate_todays_schedule(patient_id)
        analytics.update_patient_status(patient_id)
        # Seed one ledger entry per patient so the audit log isn't empty
        blockchain.append_entry(patient_id, "system_initialized", {"note": "Patient onboarded"})

    print(f"Seeded {len(PATIENTS)} patients with 90 days of backfilled history.")


if __name__ == "__main__":
    run()
