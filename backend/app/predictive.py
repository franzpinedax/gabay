"""
predictive.py
Tier 3 (Predictive Analytics) — answers your RQ4: applying data
analytics techniques to identify adherence patterns and generate
forward-looking insights, not just after-the-fact counts.

Approach: logistic regression (scikit-learn), trained per patient on
their own dispense history. Logistic regression is deliberately chosen
over a more complex model (e.g. a neural net) for two reasons that are
worth stating explicitly in your Chapter 3/4 write-up:

  1. Data volume — a single-household pilot produces a few hundred
     dose events, not enough to responsibly train a deep model.
  2. Explainability — your literature review specifically cites the
     importance of explainable outputs for clinicians and caregivers
     (Xu et al., 2023). Logistic regression coefficients are directly
     interpretable ("evening doses carry higher risk because..."),
     unlike a black-box model.

This mirrors the approach in your cited literature (Bohlmann et al.,
2021; Iino et al., 2025) — machine learning trained on historical
adherence data to predict future non-adherence.
"""
from datetime import datetime, timedelta, date

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, roc_auc_score, confusion_matrix

from database import get_conn

MIN_TRAINING_SAMPLES = 40  # below this, don't trust a trained model — fall back to a rate-based estimate
MAX_DAYS_SINCE_MISSED = 14  # cap the "days since last missed dose" feature — beyond ~2 weeks,
                            # further time adds no real information, and capping avoids an
                            # unbounded value contaminating the training distribution


def _time_slot(hour: int) -> str:
    if 5 <= hour < 11:
        return "Morning"
    if 11 <= hour < 17:
        return "Afternoon"
    if 17 <= hour < 21:
        return "Evening"
    return "Night"


# =================================================================
# FEATURE ENGINEERING
# Every feature here is something you could plausibly know BEFORE a
# dose is due — this matters for a predictive model to be honest
# (no leaking information from the future).
# =================================================================
def _build_feature_frame(patient_id: str) -> pd.DataFrame:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, med_name, scheduled_datetime, status
               FROM dispense_events
               WHERE patient_id = ? AND status IN ('confirmed','missed')
               ORDER BY scheduled_datetime ASC""",
            (patient_id,),
        ).fetchall()

    if not rows:
        return pd.DataFrame()

    records = []
    # Rolling state, updated chronologically so each event only "sees"
    # what happened strictly before it — this avoids feature leakage.
    recent_outcomes = []       # list of 0/1, most recent last
    days_since_last_missed = MAX_DAYS_SINCE_MISSED  # start "capped" (no miss on record yet)
    current_missed_streak = 0

    for r in rows:
        dt = datetime.fromisoformat(r["scheduled_datetime"])
        label = 1 if r["status"] == "missed" else 0  # 1 = will be missed

        window = recent_outcomes[-28:]  # ~7 days of a 4x/day schedule
        rolling_adherence_rate = (sum(window) / len(window)) if window else 1.0

        records.append({
            "dispense_event_id": r["id"],
            "med_name": r["med_name"],
            "is_weekend": int(dt.weekday() >= 5),
            "time_slot": _time_slot(dt.hour),
            "rolling_adherence_rate": rolling_adherence_rate,
            "days_since_last_missed": days_since_last_missed,
            "current_missed_streak": current_missed_streak,
            "label": label,
        })

        # --- update rolling state AFTER recording features for this row ---
        took_it = 1 - label
        recent_outcomes.append(took_it)
        if label == 1:
            days_since_last_missed = 0
            current_missed_streak += 1
        else:
            current_missed_streak = 0
            days_since_last_missed = min(days_since_last_missed + 1, MAX_DAYS_SINCE_MISSED)

    return pd.DataFrame(records)


def _encode(df: pd.DataFrame) -> pd.DataFrame:
    """One-hot encode categoricals, dropping the first level of each
    to avoid the dummy-variable trap (perfect multicollinearity between
    a category's dummies), which would otherwise make individual
    coefficients unstable and harder to defend as "this factor increases
    risk" in your results chapter. Kept as its own step so training and
    inference use identical column handling."""
    return pd.get_dummies(df, columns=["med_name", "time_slot"], prefix=["med", "slot"], drop_first=True)


def _encode_single_row(row: dict, feature_cols: list) -> pd.DataFrame:
    """Encode a single inference-time row against the FIXED vocabulary
    of columns the model was trained on.

    Important: pd.get_dummies() cannot be used directly on a one-row
    frame here — with only one row, it only ever sees one category
    value, so every dummy column it would produce collapses to nothing
    and silently zeroes out the very information (which medication,
    which time slot) the model needs. Building the row manually against
    the known `feature_cols` avoids that trap.
    """
    encoded = {c: 0 for c in feature_cols}
    for c in ("is_weekend", "rolling_adherence_rate", "days_since_last_missed", "current_missed_streak"):
        if c in encoded:
            encoded[c] = row[c]
    med_col = f'med_{row["med_name"]}'
    if med_col in encoded:
        encoded[med_col] = 1
    slot_col = f'slot_{row["time_slot"]}'
    if slot_col in encoded:
        encoded[slot_col] = 1
    return pd.DataFrame([encoded])[feature_cols]


# =================================================================
# TRAINING
# =================================================================
def train_risk_model(patient_id: str) -> dict:
    """Trains a fresh model on the patient's full history and reports
    honest held-out metrics. Retrained on demand — at this data scale
    (a few hundred rows) this takes milliseconds, so there's no need
    to persist the model to disk for a pilot deployment."""
    raw = _build_feature_frame(patient_id)
    if len(raw) < MIN_TRAINING_SAMPLES:
        return {
            "status": "insufficient_data",
            "samples": len(raw),
            "minimum_required": MIN_TRAINING_SAMPLES,
            "message": "Falling back to a simple rate-based estimate until more history accumulates.",
        }

    encoded = _encode(raw)
    feature_cols = [c for c in encoded.columns if c not in ("dispense_event_id", "label")]
    X = encoded[feature_cols]
    y = encoded["label"]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=42, stratify=y if y.nunique() > 1 else None
    )

    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    metrics = {
        "status": "trained",
        "samples": len(raw),
        "train_size": len(X_train),
        "test_size": len(X_test),
        "accuracy": round(accuracy_score(y_test, y_pred), 3),
        "roc_auc": round(roc_auc_score(y_test, y_proba), 3) if y_test.nunique() > 1 else None,
        "confusion_matrix": confusion_matrix(y_test, y_pred).tolist(),
    }

    # Explainability: which features push risk up vs down.
    coefs = sorted(zip(feature_cols, model.coef_[0]), key=lambda x: -abs(x[1]))
    metrics["top_risk_factors"] = [
        {"feature": name, "coefficient": round(float(coef), 3),
         "direction": "increases risk" if coef > 0 else "decreases risk"}
        for name, coef in coefs[:6]
    ]

    return metrics, model, feature_cols


# =================================================================
# INFERENCE — risk score for each of today's upcoming doses
# =================================================================
def predict_upcoming_risk(patient_id: str) -> list:
    """For each of today's not-yet-resolved doses, estimate the
    probability it gets missed, using everything known up to now."""
    trained = train_risk_model(patient_id)

    with get_conn() as conn:
        upcoming = conn.execute(
            """SELECT id, med_name, scheduled_datetime FROM dispense_events
               WHERE patient_id = ? AND status IN ('pending','dispensed')
               AND date(scheduled_datetime) = date('now')
               ORDER BY scheduled_datetime ASC""",
            (patient_id,),
        ).fetchall()

    if isinstance(trained, dict):  # insufficient data fallback
        # Fall back to the patient's overall historical miss rate.
        with get_conn() as conn:
            row = conn.execute(
                """SELECT AVG(CASE WHEN status='missed' THEN 1.0 ELSE 0.0 END) rate
                   FROM dispense_events WHERE patient_id=? AND status IN ('confirmed','missed')""",
                (patient_id,),
            ).fetchone()
        fallback_rate = row["rate"] if row and row["rate"] is not None else 0.15
        return [{
            "dispense_event_id": u["id"], "med_name": u["med_name"],
            "scheduled_datetime": u["scheduled_datetime"],
            "risk_score": round(fallback_rate, 2),
            "risk_level": _risk_level(fallback_rate),
            "basis": "rate_based_fallback",
        } for u in upcoming]

    metrics, model, feature_cols = trained
    history_df = _build_feature_frame(patient_id)

    # Recompute rolling state as of "now" so the upcoming doses use the
    # most current streaks / rolling adherence rate.
    recent = history_df.tail(28)["label"].tolist() if not history_df.empty else []
    rolling_rate = (1 - (sum(recent) / len(recent))) if recent else 1.0
    last_missed_row = history_df[history_df["label"] == 1].tail(1)
    streak = 0
    for lbl in reversed(history_df["label"].tolist()):
        if lbl == 1:
            streak += 1
        else:
            break

    results = []
    for u in upcoming:
        dt = datetime.fromisoformat(u["scheduled_datetime"])
        row = {
            "med_name": u["med_name"],
            "is_weekend": int(dt.weekday() >= 5), "time_slot": _time_slot(dt.hour),
            "rolling_adherence_rate": rolling_rate,
            "days_since_last_missed": 0 if streak > 0 else MAX_DAYS_SINCE_MISSED,
            "current_missed_streak": streak,
        }
        encoded_row = _encode_single_row(row, feature_cols)

        proba = model.predict_proba(encoded_row)[0, 1]
        results.append({
            "dispense_event_id": u["id"], "med_name": u["med_name"],
            "scheduled_datetime": u["scheduled_datetime"],
            "risk_score": round(float(proba), 2),
            "risk_level": _risk_level(proba),
            "basis": "logistic_regression",
        })
    return results


def _risk_level(p: float) -> str:
    if p >= 0.5:
        return "High"
    if p >= 0.25:
        return "Medium"
    return "Low"
