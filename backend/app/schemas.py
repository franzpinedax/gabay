"""schemas.py — request bodies for the ingestion endpoints (data coming
FROM the Arduino / wearable gateway INTO the backend) and for
dashboard-driven actions (adding a patient or medication schedule)."""
from pydantic import BaseModel, Field
from typing import Optional


class VitalIn(BaseModel):
    hr: float = Field(..., ge=0, le=250, description="Heart rate, bpm")
    spo2: float = Field(..., ge=0, le=100, description="Blood oxygen saturation, %")
    temp: float = Field(..., ge=25, le=45, description="Body temperature, °C")


class ConfirmIn(BaseModel):
    source: Optional[str] = "wearable_gesture"


class ScheduleIn(BaseModel):
    med_name: str
    dosage: str
    scheduled_time: str  # "HH:MM" 24h


class PatientIn(BaseModel):
    name: str = Field(..., min_length=1)
    nickname: Optional[str] = None
    age: int = Field(..., ge=0, le=130)
    condition: str = Field(..., min_length=1)
    caregiver_name: Optional[str] = None
