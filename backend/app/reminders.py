"""
reminders.py
Reminder/alarm sound configuration for the dispenser.

Default: a beep — synthesized on the ESP32 itself via tone(), no audio
file needed. Custom: a caregiver-recorded or uploaded voice clip,
stored here and served to both the ESP32 (to play through the
dispenser's speaker at reminder time) and the dashboard (for preview).

Hardware note (worth flagging in your Chapter 3 hardware writeup): a
simple piezo buzzer can only produce tones (the beep case). Playing an
actual voice recording requires the speaker to reproduce arbitrary
waveforms, which means an audio-capable output stage — e.g. a small
I2S DAC/amplifier module (such as a MAX98357A) feeding a small speaker
— not just a two-wire piezo buzzer. If your current BOM only has a
piezo buzzer, the beep mode works as-is; the voice mode is what would
justify adding an amplifier module to the hardware list.
"""
import os
from typing import Optional

from database import get_conn

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads", "reminders")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_CONTENT_TYPES = {
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav",
    "audio/ogg", "audio/webm", "audio/mp4", "audio/aac",
}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB — generous for a few seconds of speech,
                                    # small enough to be realistic for ESP32 flash storage


def get_reminder_config(patient_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT reminder_type, reminder_audio_filename FROM patients WHERE id=?", (patient_id,)
        ).fetchone()
    if not row:
        return {"type": "beep", "audio_available": False}
    return {
        "type": row["reminder_type"] or "beep",
        "audio_available": bool(row["reminder_audio_filename"]),
    }


def _patient_audio_path(patient_id: str, filename: str) -> str:
    ext = os.path.splitext(filename)[1] or ".webm"
    safe_ext = ext if len(ext) <= 6 else ".webm"  # guard against a weird/huge "extension"
    return os.path.join(UPLOAD_DIR, f"{patient_id}{safe_ext}")


def save_custom_reminder(patient_id: str, filename: str, content_type: str, file_bytes: bytes) -> dict:
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError(f"Unsupported audio type: {content_type}")
    if len(file_bytes) == 0:
        raise ValueError("Empty file received")
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("File too large — keep voice reminders under 5MB (a few seconds of speech)")

    # Remove any previous file for this patient (it may have a different extension)
    for existing in os.listdir(UPLOAD_DIR):
        if existing.startswith(patient_id + "."):
            os.remove(os.path.join(UPLOAD_DIR, existing))

    dest_path = _patient_audio_path(patient_id, filename or "recording.webm")
    with open(dest_path, "wb") as f:
        f.write(file_bytes)

    stored_filename = os.path.basename(dest_path)
    with get_conn() as conn:
        conn.execute(
            "UPDATE patients SET reminder_type='voice', reminder_audio_filename=? WHERE id=?",
            (stored_filename, patient_id),
        )
        conn.commit()

    return get_reminder_config(patient_id)


def reset_to_beep(patient_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT reminder_audio_filename FROM patients WHERE id=?", (patient_id,)
        ).fetchone()
        if row and row["reminder_audio_filename"]:
            path = os.path.join(UPLOAD_DIR, row["reminder_audio_filename"])
            if os.path.exists(path):
                os.remove(path)
        conn.execute(
            "UPDATE patients SET reminder_type='beep', reminder_audio_filename=NULL WHERE id=?",
            (patient_id,),
        )
        conn.commit()
    return get_reminder_config(patient_id)


def get_audio_file_path(patient_id: str) -> Optional[str]:
    """Used by both the dashboard preview and (once wired up) the ESP32
    to fetch the actual audio bytes to play."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT reminder_audio_filename FROM patients WHERE id=?", (patient_id,)
        ).fetchone()
    if not row or not row["reminder_audio_filename"]:
        return None
    path = os.path.join(UPLOAD_DIR, row["reminder_audio_filename"])
    return path if os.path.exists(path) else None
