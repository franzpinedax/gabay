# Gabay Backend — Data & Analytics Layer

Backend for the IoT-Based Smart Pill Dispenser and Health Device thesis
project. Provides the database, the five algorithms from Chapter 3.5,
and a REST + WebSocket API that the Gabay dashboard talks to.

## What was actually tested here vs. what you need to test yourself

This environment has no internet access, so `fastapi` and `uvicorn`
could not be installed or run here. To make sure you're still getting
something solid:

- **`database.py`, `blockchain.py`, `analytics.py`, `seed.py`** use only
  Python's built-in `sqlite3` module. I ran these directly and verified,
  end-to-end: schema creation, dose confirmation, missed-dose detection,
  vital-sign alerting, adherence-rate math, and — importantly — that the
  hash-chain ledger correctly detects tampering (I edited a row directly
  in the database and confirmed `verify_chain()` catches it).
- **`main.py`** (the FastAPI layer) is syntax-checked (`py_compile`) and
  each route is a thin wrapper around the tested functions above, but I
  could not actually start the server here. Run it yourself with the
  steps below — if anything errors, it'll be in the FastAPI wiring, not
  the underlying logic.

## Setup

```bash
cd gabay-backend
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cd app
python seed.py                    # creates gabay.db with Franz Pineda as the single sample patient + 90 days of history
uvicorn main:app --reload --port 8000
```

Visit `http://localhost:8000/docs` — FastAPI auto-generates an
interactive API explorer there, which is also useful for your defense
if a panelist wants to see the API directly.

## Why SQLite + a hash chain instead of SQLAlchemy + a real blockchain

Your thesis scope explicitly states blockchain use is "limited to data
integrity and access control" for a single-household pilot — not a
production multi-node deployment. Two simplifications follow directly
from that:

1. **SQLite** (via stdlib `sqlite3`) needs no separate database server,
   which matches "prototype or pilot implementation in a controlled or
   simulated environment." If you later need multi-household scale,
   only `database.py` changes — every other module only ever calls
   `get_conn()`, never raw SQL connection details.
2. **A hash chain** (`blockchain.py`) gives you the two properties your
   research questions actually ask for — data integrity and tamper
   resistance — without running a real distributed ledger (which would
   need multiple nodes reaching consensus, wildly out of scope for a
   pilot on one Arduino). Each ledger row stores
   `sha256(previous_hash + event + timestamp)`; changing any historical
   row breaks every hash after it, which `verify_chain()` detects. This
   is a legitimate, defensible design decision — cite it as a
   deliberate scope tradeoff in Chapter 3, not a shortcut.

## Mapping to Chapter 3.5 (Programmed Logic Algorithms)

| Thesis algorithm | Where it lives |
|---|---|
| Reminder Algorithm | `analytics.generate_todays_schedule()` |
| Adherence Detection Algorithm | `analytics.mark_dispensed()`, `confirm_dose()`, `sweep_missed_doses()`, `compute_adherence_rate()` |
| Vital Sign Monitoring Algorithm | `analytics.record_vital()`, `assess_vital()`, `get_vitals_trend()` |
| Alert Algorithm | `analytics.raise_alert()`, `check_vitals_and_alert()` |
| Data Logging Algorithm | `blockchain.append_entry()` — called by every function above |

## Data analytics: where it's stored and what runs on it

**Storage.** Everything lives in the SQLite tables defined in `database.py`:
`dispense_events` (one row per scheduled dose instance — time, status,
who/what confirmed it) and `vitals` (one row per sensor reading) are
the two tables all analytics below are computed from. No separate data
warehouse or aggregation table exists yet — at pilot scale (one
household, a handful of doses/day) computing statistics directly from
these raw tables on each request is fast enough that pre-aggregating
would be premature optimization. If you scale to many households
later, that's the point where you'd add a nightly rollup table.

**Analytics run in three tiers**, which map directly onto your RQ4
("identify medication adherence patterns, detect missed doses, and
generate meaningful insights") and onto standard data-analytics
maturity levels — a framing you can use directly in Chapter 2/3:

| Tier | Question answered | Functions |
|---|---|---|
| **1. Descriptive** — what happened | Adherence rate, daily/weekly trend, vitals trend | `analytics.compute_adherence_rate()`, `get_adherence_history()`, `get_vitals_trend()` |
| **2. Diagnostic** — why it happened | Which time slot / weekday / medication is driving missed doses | `analytics.get_missed_by_timeslot()`, `get_missed_by_weekday()`, `get_missed_by_medication()` |
| **3. Predictive** — what's likely next | Risk score for each upcoming dose | `predictive.train_risk_model()`, `predictive.predict_upcoming_risk()` |

### Tier 3 in detail: the predictive model (`predictive.py`)

**Algorithm: logistic regression** (scikit-learn), trained per patient
on their own dispense history. Two deliberate choices worth stating in
your defense if asked "why not a neural network / why not one global
model for all patients":

1. **Data volume.** A single-household pilot produces a few hundred
   dose events — not enough to responsibly train a deep model. This
   mirrors the ML approach in your own cited literature (Bohlmann et
   al., 2021; Iino et al., 2025), which also uses classical ML on
   adherence data rather than deep learning, for the same reason.
2. **Explainability.** Your lit review specifically cites the value of
   explainable outputs for clinicians (Xu et al., 2023). Logistic
   regression coefficients are directly interpretable — the model
   output literally includes a `top_risk_factors` list saying which
   features increase or decrease risk and by how much, rather than a
   black-box score.

**Features used** (all computed only from information available
*before* the dose in question, to avoid leaking future information into
a prediction): which medication, time slot (Morning/Afternoon/
Evening/Night), weekend vs. weekday, the patient's own rolling
adherence rate over their last ~7 days, days since their last missed
dose, and current consecutive-miss streak.

**An honest limitation to name in Chapter 5:** because each patient's
medications are each tied to one fixed daily time (e.g., Glimepiride is
always the 1pm dose), medication identity and time-of-day are
correlated with each other in this dataset. That means an individual
coefficient like "afternoon doses are riskier" and "Glimepiride is
risky" are partly capturing the same underlying signal, not two fully
independent effects. This is a real property of a fixed-prescription
schedule, not a bug — worth naming as a limitation and a reason a
larger, more varied dataset (more patients, occasional schedule shifts)
would let the model separate these effects more cleanly.

**Fallback behavior:** if a patient has fewer than 40 historical dose
events (`MIN_TRAINING_SAMPLES` in `predictive.py`), the model doesn't
train at all — `predict_upcoming_risk()` instead returns the patient's
simple historical miss rate, clearly labeled `"basis": "rate_based_fallback"`.
This avoids presenting a model trained on too little data as if it were
reliable, which matters if a panelist asks about model validity on a
freshly-onboarded patient.

## API reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/patients` | List all patients |
| GET | `/patients/{id}` | Single patient record |
| GET | `/patients/{id}/vitals/latest` | Most recent vitals |
| GET | `/patients/{id}/vitals/trend?days=7` | Daily vitals averages |
| POST | `/patients/{id}/vitals` | **Arduino/wearable posts a reading here** — `{"hr":78,"spo2":97}` |
| GET | `/patients/{id}/schedule/today` | Today's doses, shaped for the dashboard's blister strip |
| POST | `/dispense-events/{event_id}/dispensed` | Dispenser firmware calls this when the servo actuates |
| POST | `/dispense-events/{event_id}/confirm` | Wearable calls this when intake gesture is detected — `{"source":"wearable_gesture"}` |
| POST | `/patients/{id}/sweep-missed?grace_minutes=30` | Manually trigger missed-dose detection (also runs automatically every 5 min) |
| GET | `/patients/{id}/adherence?days=7` | `{ "rate": 92.1, "history": [...] }` |
| GET | `/patients/{id}/missed-by-timeslot?days=30` | Missed doses bucketed Morning/Afternoon/Evening/Night |
| GET | `/patients/{id}/missed-by-weekday?days=30` | Missed doses + rate per weekday |
| GET | `/patients/{id}/missed-by-medication?days=30` | Missed doses + rate per medication |
| GET | `/patients/{id}/risk/model-metrics` | Predictive model accuracy, ROC-AUC, explainable top risk factors |
| GET | `/patients/{id}/risk/upcoming` | Risk score + Low/Medium/High for each of today's remaining doses |
| GET | `/patients/{id}/alerts?limit=10` | Recent caregiver alerts |
| GET | `/patients/{id}/ledger?limit=50` | Blockchain-style audit log |
| GET | `/patients/{id}/ledger/verify` | Recomputes the whole chain, confirms it's untampered |
| WS | `/ws/{patient_id}` | Live push: new vitals / schedule changes / alerts |

## Wiring up the dashboard

✅ Already done — `frontend/src/App.jsx`'s `dataService` calls this API
directly (see the top-level `README.md` for how to run both projects
together in VS Code). The pattern used there:

```js
const API_BASE = "http://localhost:8000";

const dataService = {
  async getPatients() {
    const res = await fetch(`${API_BASE}/patients`);
    return res.json();
  },
  async getVitalsSnapshot(patientId) {
    const res = await fetch(`${API_BASE}/patients/${patientId}/vitals/latest`);
    return res.json();
  },
  async getTodaySchedule(patientId) {
    const res = await fetch(`${API_BASE}/patients/${patientId}/schedule/today`);
    return res.json();
  },
  // ...same pattern for getAlerts, getAdherenceHistory, getBlockchainLog, etc.
};
```

For true real-time vitals (instead of the dashboard's 5-second poll),
open a WebSocket instead:

```js
const ws = new WebSocket(`ws://localhost:8000/ws/${patientId}`);
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === "vitals") setVitals(msg.data);
};
```

## Wiring up the ESP32 dispenser

The API is hardware-agnostic — any device that can send an HTTP POST
works here, so the ESP32 doesn't need anything special on the backend
side. On the firmware side, use:

- `HTTPClient.h` (bundled with the ESP32 Arduino core) for the POST
  calls below
- `NimBLE-Arduino` (lighter-weight than the stock `BLEDevice.h`, and
  the more common choice for reading a smartwatch's Heart Rate
  Service) for the wearable BLE connection

Once your dispenser/wearable pipeline is ready, have the ESP32 POST
here instead of the mock dashboard data:

```
POST http://<your-server-ip>:8000/patients/p1/vitals
Content-Type: application/json

{"hr": 78, "spo2": 97}
```

```
POST http://<your-server-ip>:8000/dispense-events/12/dispensed
POST http://<your-server-ip>:8000/dispense-events/12/confirm
Content-Type: application/json

{"source": "wearable_gesture"}
```

## Before any real deployment (not needed for your defense, but good to note in Ch. 5 future work)

- Add authentication (JWT) — right now any client can hit any patient's data.
- Move `allow_origins=["*"]` in `main.py` to your actual frontend's domain.
- Consider PostgreSQL if you scale past one household.
- Consider a real scheduler (APScheduler, or a cron job) instead of the
  simple `asyncio` loop in `main.py` if you need it to survive server
  restarts precisely.
