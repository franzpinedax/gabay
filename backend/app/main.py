"""
main.py
FastAPI layer — thin routing on top of analytics.py / blockchain.py /
database.py. Every route here maps 1:1 onto a method the dashboard's
`dataService` object expects, so wiring the frontend to this backend
is a matter of pointing fetch() calls at these URLs (see README.md).

Run with:  uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

import analytics
import blockchain
import mqtt_bridge
import predictive
import reminders
from database import get_conn, init_db, generate_patient_id
from schemas import VitalIn, ConfirmIn, ScheduleIn, PatientIn

app = FastAPI(title="Gabay API", version="0.1.0")

# Allow the Vite/React dev server (and any origin, for a thesis prototype)
# to call this API. Tighten allow_origins before any real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()
    mqtt_bridge.start()
    asyncio.create_task(_background_sweeper())


# ---------------------------------------------------------------
# WebSocket connection manager — one set of sockets per patient_id
# ---------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, set[WebSocket]] = {}

    async def connect(self, patient_id: str, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(patient_id, set()).add(ws)

    def disconnect(self, patient_id: str, ws: WebSocket):
        self.connections.get(patient_id, set()).discard(ws)

    async def broadcast(self, patient_id: str, message: dict):
        dead = []
        for ws in self.connections.get(patient_id, set()):
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(patient_id, ws)


manager = ConnectionManager()


async def _background_sweeper():
    """Runs for the lifetime of the server: regenerates today's dose
    instances at day-rollover and sweeps for missed doses every few
    minutes, for every known patient. Replaces a manually-triggered
    cron job for the thesis prototype; swap for APScheduler or a real
    task queue for a production deployment."""
    while True:
        try:
            with get_conn() as conn:
                patient_ids = [r["id"] for r in conn.execute("SELECT id FROM patients").fetchall()]
            for pid in patient_ids:
                analytics.generate_todays_schedule(pid)
                newly_missed = analytics.sweep_missed_doses(pid, grace_minutes=30)
                for _ in newly_missed:
                    await manager.broadcast(pid, {"type": "alert_update"})
            mqtt_bridge.publish_due_commands()
        except sqlite3.OperationalError:
            # Keep scheduled monitoring alive; the failing cycle is logged
            # while the next cycle retries after the database/broker recovers.
            logging.getLogger("gabay.background").exception("Background monitoring cycle failed")
        await asyncio.sleep(30)


@app.on_event("shutdown")
def on_shutdown():
    mqtt_bridge.stop()


# ---------------------------------------------------------------
# Patients
# ---------------------------------------------------------------
@app.get("/patients")
def list_patients():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM patients ORDER BY name").fetchall()
    return [dict(r) for r in rows]


@app.get("/patients/{patient_id}")
def get_patient(patient_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Patient not found")
    return dict(row)


@app.get("/patients/{patient_id}/connection-status")
def connection_status(patient_id: str):
    """Report connection evidence for the selected patient's devices."""
    with get_conn() as conn:
        patient = conn.execute("SELECT id FROM patients WHERE id=?", (patient_id,)).fetchone()
        latest_vital = conn.execute(
            "SELECT recorded_at FROM vitals WHERE patient_id=? ORDER BY id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
        latest_ble = conn.execute(
            "SELECT connected, status, device_name, last_seen FROM wearable_status WHERE patient_id=?",
            (patient_id,),
        ).fetchone()
    if not patient:
        raise HTTPException(404, "Patient not found")

    wearable_connected = False
    wearable_last_seen = None
    wearable_status = "disconnected"
    wearable_device_name = None
    if latest_ble:
        wearable_last_seen = latest_ble["last_seen"]
        wearable_status = latest_ble["status"]
        wearable_device_name = latest_ble["device_name"]
        try:
            last_seen = datetime.fromisoformat(latest_ble["last_seen"])
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            wearable_connected = bool(latest_ble["connected"]) and (
                datetime.now(timezone.utc) - last_seen
            ).total_seconds() <= 90
        except ValueError:
            wearable_connected = False
    elif latest_vital:
        # Backward-compatible fallback for older publishers that only send vitals.
        try:
            recorded_at = datetime.fromisoformat(latest_vital["recorded_at"])
            if recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=timezone.utc)
            wearable_connected = (
                datetime.now(timezone.utc) - recorded_at
            ).total_seconds() <= 10 * 60
            wearable_last_seen = latest_vital["recorded_at"]
            wearable_status = "connected" if wearable_connected else "disconnected"
        except ValueError:
            wearable_connected = False

    ledger_status = blockchain.verify_chain(patient_id)
    return {
        "dispenser_connected": mqtt_bridge.is_connected(),
        "wearable_connected": wearable_connected,
        "blockchain_synced": ledger_status["valid"],
        "wearable_status": wearable_status,
        "wearable_device_name": wearable_device_name,
        "wearable_last_seen": wearable_last_seen,
    }


@app.post("/patients")
def create_patient(body: PatientIn):
    """Called by the dashboard's "Add patient" form. A newly created
    patient starts with an empty history — the adherence rate and
    predictive risk endpoints already handle that gracefully (rate
    defaults to 100%, risk falls back to a flat estimate until enough
    real doses accumulate)."""
    with get_conn() as conn:
        new_id = generate_patient_id(body.name, conn)
        conn.execute(
            """INSERT INTO patients (id, name, nickname, age, condition, caregiver_name, status)
               VALUES (?, ?, ?, ?, ?, ?, 'good')""",
            (new_id, body.name, body.nickname, body.age, body.condition, body.caregiver_name),
        )
        conn.commit()
    return {
        "id": new_id, "name": body.name, "nickname": body.nickname, "age": body.age,
        "condition": body.condition, "caregiver_name": body.caregiver_name, "status": "good",
    }


# ---------------------------------------------------------------
# Vitals  (Vital Sign Monitoring Algorithm)
# ---------------------------------------------------------------
@app.get("/patients/{patient_id}/vitals/latest")
def vitals_latest(patient_id: str):
    v = analytics.get_latest_vitals(patient_id)
    if not v:
        raise HTTPException(404, "No vitals recorded yet for this patient")
    return v


@app.get("/patients/{patient_id}/vitals/trend")
def vitals_trend(patient_id: str, days: int = 7):
    return analytics.get_vitals_trend(patient_id, days)


@app.post("/patients/{patient_id}/vitals")
async def post_vital(patient_id: str, body: VitalIn):
    """Ingestion endpoint: point your Arduino/wearable gateway here.
    e.g. POST {"hr": 78, "spo2": 97}"""
    result = analytics.record_vital(patient_id, body.hr, body.spo2)
    await manager.broadcast(patient_id, {"type": "vitals", "data": result})
    return result


# ---------------------------------------------------------------
# Medication schedule + dispensing  (Reminder + Adherence Detection)
# ---------------------------------------------------------------
@app.get("/patients/{patient_id}/schedule/today")
def schedule_today(patient_id: str):
    return analytics.generate_todays_schedule(patient_id)


@app.post("/patients/{patient_id}/schedule")
def add_schedule(patient_id: str, body: ScheduleIn):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO medication_schedule (patient_id, med_name, dosage, scheduled_time) VALUES (?, ?, ?, ?)",
            (patient_id, body.med_name, body.dosage, body.scheduled_time),
        )
        conn.commit()
    return {"id": cur.lastrowid, "patient_id": patient_id, "med_name": body.med_name,
            "dosage": body.dosage, "scheduled_time": body.scheduled_time}


@app.post("/dispense-events/{event_id}/dispensed")
async def dispensed(event_id: int):
    """Called by the dispenser firmware the moment the servo/carousel
    actuates and releases a dose."""
    row = analytics.mark_dispensed(event_id)
    await manager.broadcast(row["patient_id"], {"type": "schedule_update"})
    return row


@app.post("/dispense-events/{event_id}/command")
async def send_dispense_command(event_id: int):
    """Send an immediate MQTT dispense command to the ESP32."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM dispense_events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Dispense event not found")
    if row["status"] not in ("pending", "dispensed"):
        raise HTTPException(409, f"Dose is already {row['status']}")
    if not mqtt_bridge.publish_dispense_command(dict(row)):
        raise HTTPException(503, "MQTT dispenser is not connected")

    with get_conn() as conn:
        conn.execute(
            "UPDATE dispense_events SET command_sent_at=? WHERE id=?",
            (datetime.now().isoformat(), event_id),
        )
        conn.commit()
    return {"sent": True, "event_id": event_id}


@app.post("/dispense-events/{event_id}/confirm")
async def confirm(event_id: int, body: ConfirmIn):
    """Called when the wearable/sensor confirms actual intake
    (e.g. a detected medication-taking gesture)."""
    try:
        row = analytics.confirm_dose(event_id, source=body.source)
    except ValueError as error:
        raise HTTPException(409, str(error))
    await manager.broadcast(row["patient_id"], {"type": "schedule_update"})
    return row


@app.post("/patients/{patient_id}/sweep-missed")
async def sweep(patient_id: str, grace_minutes: int = 30):
    """Manually trigger a missed-dose sweep (also runs automatically
    every 5 minutes in the background)."""
    missed = analytics.sweep_missed_doses(patient_id, grace_minutes)
    if missed:
        await manager.broadcast(patient_id, {"type": "alert_update"})
    return {"newly_missed": missed}


# ---------------------------------------------------------------
# Reminder / alarm sound (defaults to a beep; can be a custom voice
# clip). The ESP32 checks GET .../reminder at each scheduled dose
# time — "voice" means it should fetch and play GET .../reminder/audio
# through the speaker; "beep" means it just plays its own tone(), no
# audio file involved. See reminders.py for the hardware note on why
# voice mode needs an audio-capable speaker, not just a piezo buzzer.
# ---------------------------------------------------------------
@app.get("/patients/{patient_id}/reminder")
def get_reminder(patient_id: str):
    return reminders.get_reminder_config(patient_id)


@app.post("/patients/{patient_id}/reminder/voice")
async def upload_reminder_voice(patient_id: str, file: UploadFile = File(...)):
    file_bytes = await file.read()
    try:
        return reminders.save_custom_reminder(patient_id, file.filename, file.content_type, file_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/patients/{patient_id}/reminder/reset")
def reset_reminder(patient_id: str):
    return reminders.reset_to_beep(patient_id)


@app.get("/patients/{patient_id}/reminder/audio")
def get_reminder_audio(patient_id: str):
    path = reminders.get_audio_file_path(patient_id)
    if not path:
        raise HTTPException(404, "No custom reminder audio set for this patient")
    return FileResponse(path)


# ---------------------------------------------------------------
# Adherence analytics
# ---------------------------------------------------------------
@app.get("/patients/{patient_id}/adherence")
def adherence(patient_id: str, days: int = 7):
    return {
        "rate": analytics.compute_adherence_rate(patient_id, days),
        "history": analytics.get_adherence_history(patient_id, days),
    }


@app.get("/patients/{patient_id}/missed-by-timeslot")
def missed_by_timeslot(patient_id: str, days: int = 30):
    return analytics.get_missed_by_timeslot(patient_id, days)


@app.get("/patients/{patient_id}/missed-by-weekday")
def missed_by_weekday(patient_id: str, days: int = 30):
    return analytics.get_missed_by_weekday(patient_id, days)


@app.get("/patients/{patient_id}/missed-by-medication")
def missed_by_medication(patient_id: str, days: int = 30):
    return analytics.get_missed_by_medication(patient_id, days)


# ---------------------------------------------------------------
# Predictive analytics — Tier 3 (see predictive.py)
# ---------------------------------------------------------------
@app.get("/patients/{patient_id}/risk/model-metrics")
def risk_model_metrics(patient_id: str):
    """Training metrics + explainable coefficients — useful for your
    Chapter 4 results section (accuracy, ROC-AUC, top risk factors)."""
    result = predictive.train_risk_model(patient_id)
    if isinstance(result, dict):
        return result
    metrics, _model, _cols = result
    return metrics


@app.get("/patients/{patient_id}/risk/upcoming")
def risk_upcoming(patient_id: str):
    """Risk score (0-1) + Low/Medium/High label for each of today's
    not-yet-resolved doses — this is what the dashboard shows to flag
    which upcoming dose most needs caregiver attention."""
    return predictive.predict_upcoming_risk(patient_id)


# ---------------------------------------------------------------
# Provider report export
# ---------------------------------------------------------------
@app.get("/patients/{patient_id}/report")
def patient_report(patient_id: str):
    """Return the provider-facing report data used by the dashboard export."""
    with get_conn() as conn:
        patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if not patient:
        raise HTTPException(404, "Patient not found")

    model_result = predictive.train_risk_model(patient_id)
    model_metrics = model_result if isinstance(model_result, dict) else model_result[0]
    return {
        "generated_at": datetime.now().isoformat(),
        "patient": dict(patient),
        "schedule": analytics.get_today_schedule(patient_id),
        "adherence": {
            "last_7_days_rate": analytics.compute_adherence_rate(patient_id, days=7),
            "history": analytics.get_adherence_history(patient_id, days=30),
        },
        "missed_by_timeslot": analytics.get_missed_by_timeslot(patient_id, days=30),
        "missed_by_weekday": analytics.get_missed_by_weekday(patient_id, days=30),
        "missed_by_medication": analytics.get_missed_by_medication(patient_id, days=30),
        "vitals_trend": analytics.get_vitals_trend(patient_id, days=7),
        "alerts": analytics.get_alerts(patient_id, limit=50),
        "upcoming_risk": predictive.predict_upcoming_risk(patient_id),
        "risk_model": model_metrics,
    }


# ---------------------------------------------------------------
# Alerts  (Alert Algorithm)
# ---------------------------------------------------------------
@app.get("/patients/{patient_id}/alerts")
def alerts(patient_id: str, limit: int = 10):
    return analytics.get_alerts(patient_id, limit)


# ---------------------------------------------------------------
# Blockchain ledger  (Data Logging Algorithm)
# ---------------------------------------------------------------
@app.get("/patients/{patient_id}/ledger")
def ledger(patient_id: str, limit: int = 50):
    return blockchain.get_log(patient_id, limit)


@app.get("/patients/{patient_id}/ledger/verify")
def ledger_verify(patient_id: str):
    return blockchain.verify_chain(patient_id)


# ---------------------------------------------------------------
# Real-time push
# ---------------------------------------------------------------
@app.websocket("/ws/{patient_id}")
async def ws_endpoint(websocket: WebSocket, patient_id: str):
    await manager.connect(patient_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive; client doesn't need to send anything meaningful
    except WebSocketDisconnect:
        manager.disconnect(patient_id, websocket)


@app.get("/")
def health():
    return {"status": "ok", "service": "gabay-api"}
