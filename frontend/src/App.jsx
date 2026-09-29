import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import {
  Pill, HeartPulse, Thermometer, Activity, ShieldCheck, AlertTriangle,
  Wifi, Users, Stethoscope, Clock, CheckCircle2, XCircle, Link2,
  ChevronRight, Bell, User, Download, Radio, Plus, X, Trash2, Wrench,
  Mic, Volume2, Play, Square, Upload, RotateCcw,
} from "lucide-react";

/* ============================================================
   DESIGN TOKENS
   Grounded in the subject: amber pill-bottle glass, blister-pack
   geometry, and the calm sage of a home-care setting rather than
   a generic clinical white or dashboard-template palette.
============================================================ */
const C = {
  bg: "#EEF3EA",
  bgAlt: "#E1EADD",
  surface: "#FFFFFF",
  surfaceAlt: "#F6F9F4",
  ink: "#1E332C",
  inkSoft: "#55695F",
  inkFaint: "#8A9C92",
  border: "rgba(30,51,44,0.12)",
  borderStrong: "rgba(30,51,44,0.22)",
  teal: "#2E7A67",
  tealDeep: "#1F5B4C",
  tealSoft: "#DCEDE6",
  amber: "#C68A2E",
  amberSoft: "#F4E7C9",
  danger: "#B8452F",
  dangerSoft: "#F5DCD3",
};

const FONT_DISPLAY = "'Space Grotesk', ui-sans-serif, system-ui, sans-serif";
const FONT_BODY = "'IBM Plex Sans', ui-sans-serif, system-ui, sans-serif";
const FONT_MONO = "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace";

/* ============================================================
   DATA SERVICE
   This is the seam between UI and reality — connected here to the
   real Gabay backend (see /backend). Start the API with
   `uvicorn main:app --reload` before running this dashboard; see
   the top-level README for the full run order.
============================================================ */
const API_BASE = "http://localhost:8000";

async function getJSON(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

const dataService = {
  async getPatients() {
    return getJSON("/patients");
  },
  async getVitalsSnapshot(patientId) {
    try {
      return await getJSON(`/patients/${patientId}/vitals/latest`);
    } catch {
      return null; // no vitals recorded yet for a freshly-seeded patient
    }
  },
  async getTodaySchedule(patientId) {
    return getJSON(`/patients/${patientId}/schedule/today`);
  },
  async getAlerts(patientId) {
    return getJSON(`/patients/${patientId}/alerts?limit=10`);
  },
  async getAdherenceHistory(patientId, days) {
    const { history } = await getJSON(`/patients/${patientId}/adherence?days=${days}`);
    return history;
  },
  async getMissedByTimeSlot(patientId) {
    return getJSON(`/patients/${patientId}/missed-by-timeslot`);
  },
  async getVitalsTrend(patientId) {
    return getJSON(`/patients/${patientId}/vitals/trend`);
  },
  async getBlockchainLog(patientId) {
    const rows = await getJSON(`/patients/${patientId}/ledger?limit=50`);
    // Backend fields (event_type, created_at, full sha256 hash) are
    // reshaped here to match what the table below expects.
    return rows.map((r) => ({
      id: r.id,
      time: new Date(r.created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }),
      event: r.event_type.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
      hash: `${r.hash.slice(0, 10)}…${r.hash.slice(-6)}`,
    }));
  },
  async createPatient(payload) {
    const res = await fetch(`${API_BASE}/patients`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`create patient → ${res.status}`);
    return res.json();
  },
  async addSchedule(patientId, payload) {
    const res = await fetch(`${API_BASE}/patients/${patientId}/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`add schedule → ${res.status}`);
    return res.json();
  },
  async dispenseDose(eventId) {
    // Ask Gabay to publish the MQTT command to the ESP32. The ESP32
    // confirms the actual actuation through the /dispensed endpoint.
    const res = await fetch(`${API_BASE}/dispense-events/${eventId}/command`, { method: "POST" });
    if (!res.ok) throw new Error(`dispense → ${res.status}`);
    return res.json();
  },
  async confirmDoseManual(eventId) {
    // Stands in for the wearable's gesture-detection confirming intake.
    // Tagged "manual" (not "wearable_gesture") so the audit log
    // correctly distinguishes a test click from a real sensor event.
    const res = await fetch(`${API_BASE}/dispense-events/${eventId}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "manual" }),
    });
    if (!res.ok) throw new Error(`confirm → ${res.status}`);
    return res.json();
  },
  async getReminderConfig(patientId) {
    return getJSON(`/patients/${patientId}/reminder`);
  },
};

/* ============================================================
   HELPERS
============================================================ */
function assessVital(kind, value) {
  if (kind === "hr") {
    if (value >= 60 && value <= 100) return "good";
    if (value > 100 && value <= 115) return "attention";
    return value < 50 ? "critical" : "attention";
  }
  if (kind === "spo2") {
    if (value >= 95) return "good";
    if (value >= 90) return "attention";
    return "critical";
  }
  if (kind === "temp") {
    if (value >= 36.1 && value <= 37.4) return "good";
    if (value <= 38.2) return "attention";
    return "critical";
  }
  return "good";
}

function statusColor(status) {
  if (status === "good") return C.teal;
  if (status === "attention") return C.amber;
  if (status === "critical") return C.danger;
  return C.inkFaint;
}
function statusSoft(status) {
  if (status === "good") return C.tealSoft;
  if (status === "attention") return C.amberSoft;
  if (status === "critical") return C.dangerSoft;
  return C.surfaceAlt;
}

/* ============================================================
   SMALL PRESENTATIONAL PIECES
============================================================ */
function ConnectionPill({ icon: Icon, label, ok = true }) {
  return (
    <div
      className="flex items-center gap-2 rounded-full px-3 py-1.5"
      style={{ background: C.surface, border: `1px solid ${C.border}` }}
    >
      <span
        className="pulse-dot"
        style={{
          width: 7, height: 7, borderRadius: 999,
          background: ok ? C.teal : C.danger, flexShrink: 0,
        }}
      />
      <Icon size={14} style={{ color: C.inkSoft }} />
      <span style={{ fontFamily: FONT_BODY, fontSize: 12.5, color: C.inkSoft }}>{label}</span>
    </div>
  );
}

function RoleSwitch({ role, setRole }) {
  const opts = [
    { key: "caregiver", label: "Caregiver", icon: Users },
    { key: "provider", label: "Healthcare Provider", icon: Stethoscope },
  ];
  return (
    <div
      className="flex items-center rounded-full p-1"
      style={{ background: C.bgAlt, border: `1px solid ${C.border}` }}
    >
      {opts.map((o) => {
        const active = role === o.key;
        const Icon = o.icon;
        return (
          <button
            key={o.key}
            onClick={() => setRole(o.key)}
            className="flex items-center gap-1.5 rounded-full px-3.5 py-1.5 transition-colors"
            style={{
              background: active ? C.tealDeep : "transparent",
              color: active ? "#fff" : C.inkSoft,
              fontFamily: FONT_BODY, fontSize: 13, fontWeight: 500, border: "none",
              cursor: "pointer",
            }}
          >
            <Icon size={14} />
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

function SectionCard({ icon: Icon, title, subtitle, children, right }) {
  return (
    <div
      className="rounded-2xl p-5"
      style={{ background: C.surface, border: `1px solid ${C.border}` }}
    >
      <div className="flex items-start justify-between mb-4 gap-3 flex-wrap">
        <div className="flex items-center gap-2.5">
          {Icon && (
            <span
              className="flex items-center justify-center rounded-xl"
              style={{ width: 32, height: 32, background: C.tealSoft, color: C.tealDeep }}
            >
              <Icon size={16} />
            </span>
          )}
          <div>
            <h3 style={{ fontFamily: FONT_DISPLAY, fontSize: 15.5, fontWeight: 600, color: C.ink, margin: 0 }}>
              {title}
            </h3>
            {subtitle && (
              <p style={{ fontFamily: FONT_BODY, fontSize: 12, color: C.inkFaint, margin: 0 }}>{subtitle}</p>
            )}
          </div>
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

function DoseDot({ status }) {
  const map = {
    taken: { color: C.teal, soft: C.tealSoft, Icon: CheckCircle2 },
    missed: { color: C.danger, soft: C.dangerSoft, Icon: XCircle },
    next: { color: C.amber, soft: C.amberSoft, Icon: Clock },
    upcoming: { color: C.inkFaint, soft: C.surfaceAlt, Icon: Clock },
  };
  const cfg = map[status] || map.upcoming;
  const { Icon } = cfg;
  return (
    <span
      className={status === "next" ? "pulse-ring" : ""}
      style={{
        width: 44, height: 44, borderRadius: 999, background: cfg.soft,
        display: "flex", alignItems: "center", justifyContent: "center",
        border: `2px solid ${cfg.color}`, flexShrink: 0,
      }}
    >
      <Icon size={18} style={{ color: cfg.color }} />
    </span>
  );
}

function MedicationDayStrip({ schedule }) {
  return (
    <div className="relative flex justify-between items-start pt-2 pb-1 overflow-x-auto gap-2">
      <div
        style={{
          position: "absolute", left: "3rem", right: "3rem", top: "3.15rem",
          height: 2, background: C.border, zIndex: 0,
        }}
      />
      {schedule.map((dose) => (
        <div key={dose.id} className="relative flex flex-col items-center gap-2" style={{ zIndex: 1, minWidth: 84 }}>
          <span style={{ fontFamily: FONT_MONO, fontSize: 11.5, color: C.inkSoft }}>{dose.time}</span>
          <DoseDot status={dose.status} />
          <span style={{ fontFamily: FONT_BODY, fontSize: 12, color: C.ink, textAlign: "center", maxWidth: 90 }}>
            {dose.label}
          </span>
        </div>
      ))}
    </div>
  );
}

function VitalCard({ icon: Icon, label, value, unit, kind, range }) {
  const status = assessVital(kind, value);
  const color = statusColor(status);
  const soft = statusSoft(status);
  return (
    <div className="rounded-xl p-4 flex-1 min-w-[140px]" style={{ background: soft, border: `1px solid ${color}22` }}>
      <div className="flex items-center gap-2 mb-2">
        <Icon size={15} style={{ color }} />
        <span style={{ fontFamily: FONT_BODY, fontSize: 12, color: C.inkSoft, fontWeight: 500 }}>{label}</span>
      </div>
      <div className="flex items-baseline gap-1">
        <span style={{ fontFamily: FONT_MONO, fontSize: 28, fontWeight: 600, color: C.ink, lineHeight: 1 }}>
          {value}
        </span>
        <span style={{ fontFamily: FONT_MONO, fontSize: 13, color: C.inkFaint }}>{unit}</span>
      </div>
      <p style={{ fontFamily: FONT_BODY, fontSize: 11, color: C.inkFaint, margin: "6px 0 0" }}>Normal: {range}</p>
    </div>
  );
}

function AlertRow({ alert }) {
  const map = {
    missed: { color: C.danger, Icon: AlertTriangle },
    warning: { color: C.amber, Icon: AlertTriangle },
    critical: { color: C.danger, Icon: AlertTriangle },
    info: { color: C.teal, Icon: CheckCircle2 },
  };
  const cfg = map[alert.type] || map.info;
  const { Icon } = cfg;
  return (
    <div className="flex items-start gap-3 py-2.5" style={{ borderTop: `1px solid ${C.border}` }}>
      <Icon size={16} style={{ color: cfg.color, marginTop: 2, flexShrink: 0 }} />
      <div className="flex-1">
        <p style={{ fontFamily: FONT_BODY, fontSize: 13, color: C.ink, margin: 0 }}>{alert.message}</p>
        <p style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.inkFaint, margin: "2px 0 0" }}>
          {alert.time}{alert.recipients === "caregiver,provider" ? " · Caregiver + healthcare provider" : ""}
        </p>
      </div>
    </div>
  );
}

function ChartTooltip({ active, payload, label, unit = "" }) {
  if (!active || !payload?.length) return null;
  return (
    <div
      className="rounded-lg px-3 py-2"
      style={{ background: C.tealDeep, border: `1px solid ${C.border}` }}
    >
      <p style={{ fontFamily: FONT_MONO, fontSize: 11, color: "#DCEDE6", margin: 0 }}>{label}</p>
      {payload.map((p, i) => (
        <p key={i} style={{ fontFamily: FONT_MONO, fontSize: 12, color: "#fff", margin: 0, fontWeight: 600 }}>
          {p.value}{unit}
        </p>
      ))}
    </div>
  );
}

/* ============================================================
   CAREGIVER VIEW
============================================================ */
function PatientSwitcher({ patients, selectedPatientId, onSelect }) {
  if (patients.length <= 1) return null;
  return (
    <div className="flex items-center gap-1.5 flex-wrap mb-1">
      <span style={{ fontFamily: FONT_BODY, fontSize: 11.5, color: C.inkFaint, marginRight: 2 }}>
        Viewing:
      </span>
      {patients.map((p) => {
        const active = p.id === selectedPatientId;
        return (
          <button
            key={p.id}
            onClick={() => onSelect(p.id)}
            style={{
              display: "flex", alignItems: "center", gap: 6,
              background: active ? C.tealDeep : C.surface,
              color: active ? "#fff" : C.inkSoft,
              border: `1px solid ${active ? C.tealDeep : C.border}`,
              borderRadius: 999, padding: "5px 12px", cursor: "pointer",
              fontFamily: FONT_BODY, fontSize: 12.5, fontWeight: 500,
            }}
          >
            <span style={{ width: 6, height: 6, borderRadius: 999, background: active ? "#fff" : statusColor(p.status), flexShrink: 0 }} />
            {p.nickname || p.name}
          </button>
        );
      })}
    </div>
  );
}

function DispenserTestPanel({ schedule, onDispense, onConfirm, busy, error }) {
  const nextDose = schedule.find((d) => d.status === "next");
  return (
    <div
      className="rounded-xl p-3 mt-3"
      style={{ border: `1.5px dashed ${C.borderStrong}`, background: C.surfaceAlt }}
    >
      <div className="flex items-center gap-2 mb-2">
        <Wrench size={13} style={{ color: C.inkFaint }} />
        <span style={{ fontFamily: FONT_BODY, fontSize: 11.5, color: C.inkFaint, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5 }}>
          Testing tools — simulate the dispenser
        </span>
      </div>

      {!nextDose ? (
        <p style={{ fontFamily: FONT_BODY, fontSize: 12.5, color: C.inkFaint, margin: 0 }}>
          No dose currently due to test — all of today's doses are resolved.
        </p>
      ) : (
        <>
          <p style={{ fontFamily: FONT_BODY, fontSize: 12.5, color: C.ink, margin: "0 0 8px" }}>
            Next due: <strong>{nextDose.time} · {nextDose.label}</strong>
          </p>
          <div className="flex gap-2 flex-wrap">
            <button
              onClick={() => onDispense(nextDose.id)}
              disabled={busy}
              className="flex items-center gap-1.5"
              style={{
                background: C.tealDeep, color: "#fff", border: "none", borderRadius: 999,
                padding: "6px 14px", cursor: busy ? "default" : "pointer", opacity: busy ? 0.6 : 1,
                fontFamily: FONT_BODY, fontSize: 12.5, fontWeight: 600,
              }}
            >
              <Pill size={13} /> Simulate Dispense
            </button>
            <button
              onClick={() => onConfirm(nextDose.id)}
              disabled={busy}
              className="flex items-center gap-1.5"
              style={{
                background: "transparent", color: C.tealDeep, border: `1.5px solid ${C.teal}`, borderRadius: 999,
                padding: "6px 14px", cursor: busy ? "default" : "pointer", opacity: busy ? 0.6 : 1,
                fontFamily: FONT_BODY, fontSize: 12.5, fontWeight: 600,
              }}
            >
              <CheckCircle2 size={13} /> Simulate Confirm (Taken)
            </button>
          </div>
        </>
      )}
      {error && <p style={{ fontFamily: FONT_BODY, fontSize: 12, color: C.danger, margin: "8px 0 0" }}>{error}</p>}
      <p style={{ fontFamily: FONT_BODY, fontSize: 11, color: C.inkFaint, margin: "8px 0 0" }}>
        These call the exact same endpoints your ESP32 will call — useful for testing the full pipeline before hardware is wired up.
      </p>
    </div>
  );
}

function pillButtonStyle(variant, disabled) {
  const base = {
    display: "flex", alignItems: "center", gap: 6, borderRadius: 999,
    padding: "6px 14px", cursor: disabled ? "default" : "pointer", opacity: disabled ? 0.55 : 1,
    fontFamily: FONT_BODY, fontSize: 12.5, fontWeight: 600, border: "none",
  };
  if (variant === "solid") return { ...base, background: C.tealDeep, color: "#fff" };
  if (variant === "outline") return { ...base, background: "transparent", color: C.tealDeep, border: `1.5px solid ${C.teal}` };
  if (variant === "danger-outline") return { ...base, background: "transparent", color: C.danger, border: `1.5px solid ${C.danger}` };
  return { ...base, background: C.bgAlt, color: C.inkSoft };
}

function ReminderSettings({ patientId, reminderConfig, onUpdate }) {
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const mediaRecorderRef = useRef(null);
  const chunksRef = useRef([]);
  const fileInputRef = useRef(null);
  const audioCtxRef = useRef(null);

  const isVoice = reminderConfig?.type === "voice";

  const playBeepPreview = () => {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) { setError("This browser doesn't support audio preview."); return; }
    const ctx = audioCtxRef.current || new Ctx();
    audioCtxRef.current = ctx;
    [0, 400].forEach((delay) => {
      setTimeout(() => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "square";
        osc.frequency.value = 880;
        gain.gain.setValueAtTime(0.18, ctx.currentTime);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + 0.28);
      }, delay);
    });
  };

  const playVoicePreview = () => {
    const audio = new Audio(`${API_BASE}/patients/${patientId}/reminder/audio?t=${Date.now()}`);
    audio.play().catch(() => setError("Couldn't play the saved recording."));
  };

  const uploadBlob = async (blob, filename, contentType) => {
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", blob, filename);
      const res = await fetch(`${API_BASE}/patients/${patientId}/reminder/voice`, { method: "POST", body: form });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail || `Upload failed (${res.status})`);
      }
      onUpdate(await res.json());
    } catch (e) {
      setError(e.message || "Upload failed — is the backend running?");
    } finally {
      setBusy(false);
    }
  };

  const startRecording = async () => {
    setError(null);
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("This browser doesn't support recording — try uploading a file instead.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
      recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: "audio/webm" });
        await uploadBlob(blob, "recording.webm", "audio/webm");
      };
      recorder.start();
      mediaRecorderRef.current = recorder;
      setRecording(true);
    } catch {
      setError("Microphone access denied or unavailable.");
    }
  };

  const stopRecording = () => {
    mediaRecorderRef.current?.stop();
    setRecording(false);
  };

  const handleFileChange = (e) => {
    const file = e.target.files?.[0];
    if (file) uploadBlob(file, file.name, file.type || "audio/mpeg");
    e.target.value = "";
  };

  const handleReset = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/patients/${patientId}/reminder/reset`, { method: "POST" });
      if (!res.ok) throw new Error();
      onUpdate(await res.json());
    } catch {
      setError("Couldn't reset — is the backend running?");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SectionCard icon={isVoice ? Mic : Volume2} title="Reminder & alarm sound" subtitle="Plays through the dispenser's speaker at dose time">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <span
          className="rounded-full px-3 py-1"
          style={{ background: isVoice ? C.tealSoft : C.bgAlt, color: isVoice ? C.tealDeep : C.inkSoft, fontFamily: FONT_BODY, fontSize: 12, fontWeight: 600 }}
        >
          {isVoice ? "Custom voice reminder set" : "Default beep (no recording set)"}
        </span>
        <button onClick={isVoice ? playVoicePreview : playBeepPreview} style={pillButtonStyle("neutral")}>
          <Play size={12} /> Preview
        </button>
      </div>

      <div className="flex gap-2 flex-wrap">
        {!recording ? (
          <button onClick={startRecording} disabled={busy} style={pillButtonStyle("solid", busy)}>
            <Mic size={13} /> Record voice
          </button>
        ) : (
          <button onClick={stopRecording} style={{ ...pillButtonStyle("danger-outline"), background: C.dangerSoft }}>
            <Square size={13} /> Stop recording…
          </button>
        )}
        <button onClick={() => fileInputRef.current?.click()} disabled={busy} style={pillButtonStyle("outline", busy)}>
          <Upload size={13} /> Upload file
        </button>
        <input ref={fileInputRef} type="file" accept="audio/*" onChange={handleFileChange} style={{ display: "none" }} />
        {isVoice && (
          <button onClick={handleReset} disabled={busy} style={pillButtonStyle("neutral", busy)}>
            <RotateCcw size={13} /> Reset to beep
          </button>
        )}
      </div>

      {error && <p style={{ fontFamily: FONT_BODY, fontSize: 12, color: C.danger, margin: "10px 0 0" }}>{error}</p>}

      <p style={{ fontFamily: FONT_BODY, fontSize: 11, color: C.inkFaint, margin: "10px 0 0" }}>
        A familiar voice can be easier for someone with MCI to recognize and respond to than a generic beep. Keep clips short — a few seconds is plenty.
      </p>
    </SectionCard>
  );
}

function CaregiverView({
  patients, selectedPatientId, onSelectPatient,
  patient, vitals, schedule, alerts, adherence7, loading,
  onDispense, onConfirm, dispenseBusy, dispenseError,
  reminderConfig, onReminderUpdate,
}) {
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "1fr" }}>
      <PatientSwitcher patients={patients} selectedPatientId={selectedPatientId} onSelect={onSelectPatient} />

      <SectionCard
        icon={User}
        title={patient?.nickname || patient?.name}
        subtitle={patient ? `${patient.age} yrs · ${patient.condition}` : ""}
        right={
          <span
            className="rounded-full px-3 py-1"
            style={{
              background: statusSoft(patient?.status), color: statusColor(patient?.status),
              fontFamily: FONT_BODY, fontSize: 12, fontWeight: 600,
            }}
          >
            {patient?.status === "good" ? "Doing well" : patient?.status === "attention" ? "Needs attention" : "Needs attention now"}
          </span>
        }
      >
        {loading ? (
          <SkeletonLine />
        ) : (
          <>
            <MedicationDayStrip schedule={schedule} />
            <DispenserTestPanel
              schedule={schedule}
              onDispense={onDispense}
              onConfirm={onConfirm}
              busy={dispenseBusy}
              error={dispenseError}
            />
          </>
        )}
      </SectionCard>

      {!loading && (
        <ReminderSettings
          patientId={patient?.id}
          reminderConfig={reminderConfig}
          onUpdate={onReminderUpdate}
        />
      )}

      <SectionCard
        icon={HeartPulse}
        title="Vitals right now"
        subtitle="From the wearable — refreshing automatically"
      >
        {loading ? (
          <SkeletonLine />
        ) : !vitals ? (
          <p style={{ fontFamily: FONT_BODY, fontSize: 13, color: C.inkFaint }}>
            No vitals recorded yet for this patient.
          </p>
        ) : (
          <div className="flex gap-3 flex-wrap">
            <VitalCard icon={HeartPulse} label="Heart Rate" value={vitals.hr} unit="bpm" kind="hr" range="60–100 bpm" />
            <VitalCard icon={Activity} label="Oxygen (SpO2)" value={vitals.spo2} unit="%" kind="spo2" range="≥ 95%" />
            <VitalCard icon={Thermometer} label="Temperature" value={vitals.temp} unit="°C" kind="temp" range="36.1–37.4°C" />
          </div>
        )}
      </SectionCard>

      <div className="grid gap-4 grid-cols-1 md:grid-cols-2">
        <SectionCard icon={Bell} title="Recent alerts" subtitle="Newest first">
          {loading ? <SkeletonLine /> : alerts.length ? alerts.map((a) => <AlertRow key={a.id} alert={a} />) : (
            <p style={{ fontFamily: FONT_BODY, fontSize: 13, color: C.inkFaint }}>No alerts today. All clear.</p>
          )}
        </SectionCard>

        <SectionCard icon={Pill} title="This week's adherence" subtitle="Percent of scheduled doses taken">
          {loading ? <SkeletonLine /> : (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={adherence7} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke={C.border} />
                <XAxis dataKey="day" tick={{ fontFamily: FONT_MONO, fontSize: 11, fill: C.inkSoft }} axisLine={{ stroke: C.border }} tickLine={false} />
                <YAxis domain={[0, 100]} tick={{ fontFamily: FONT_MONO, fontSize: 11, fill: C.inkSoft }} axisLine={false} tickLine={false} />
                <Tooltip content={<ChartTooltip unit="%" />} cursor={{ fill: C.bgAlt }} />
                <ReferenceLine y={80} stroke={C.amber} strokeDasharray="4 4" />
                <Bar dataKey="pct" radius={[6, 6, 0, 0]} fill={C.teal} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </SectionCard>
      </div>
    </div>
  );
}

/* ============================================================
   ADD PATIENT MODAL
============================================================ */
const inputStyle = {
  width: "100%", fontFamily: FONT_BODY, fontSize: 13.5, color: C.ink,
  background: C.surfaceAlt, border: `1px solid ${C.border}`, borderRadius: 8,
  padding: "8px 10px", outline: "none",
};
const labelStyle = { fontFamily: FONT_BODY, fontSize: 12, fontWeight: 500, color: C.inkSoft, display: "block", marginBottom: 4 };

function AddPatientModal({ onClose, onCreated }) {
  const [form, setForm] = useState({ name: "", nickname: "", age: "", condition: "", caregiver_name: "" });
  const [meds, setMeds] = useState([{ med_name: "", dosage: "", scheduled_time: "" }]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const set = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));
  const updateMed = (i, field, value) =>
    setMeds((prev) => prev.map((m, idx) => (idx === i ? { ...m, [field]: value } : m)));
  const addMedRow = () => setMeds((prev) => [...prev, { med_name: "", dosage: "", scheduled_time: "" }]);
  const removeMedRow = (i) => setMeds((prev) => prev.filter((_, idx) => idx !== i));

  const handleSubmit = async () => {
    if (!form.name.trim() || !form.age || !form.condition.trim()) {
      setError("Name, age, and condition are required.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const patient = await dataService.createPatient({
        name: form.name.trim(),
        nickname: form.nickname.trim() || null,
        age: parseInt(form.age, 10),
        condition: form.condition.trim(),
        caregiver_name: form.caregiver_name.trim() || null,
      });
      const validMeds = meds.filter((m) => m.med_name.trim() && m.dosage.trim() && m.scheduled_time);
      for (const m of validMeds) {
        await dataService.addSchedule(patient.id, m);
      }
      onCreated(patient.id);
    } catch (e) {
      setError("Couldn't reach the backend — make sure uvicorn is still running.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(30,51,44,0.45)",
        display: "flex", alignItems: "center", justifyContent: "center",
        zIndex: 50, padding: 16,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: C.surface, borderRadius: 18, padding: 24, width: "100%",
          maxWidth: 480, maxHeight: "88vh", overflowY: "auto",
        }}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 style={{ fontFamily: FONT_DISPLAY, fontSize: 17, fontWeight: 600, color: C.ink, margin: 0 }}>
            Add a patient
          </h3>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: C.inkFaint, padding: 4 }}>
            <X size={18} />
          </button>
        </div>

        <div className="flex flex-col gap-3">
          <div>
            <label style={labelStyle}>Full name *</label>
            <input style={inputStyle} value={form.name} onChange={set("name")} placeholder="Juan Dela Cruz" />
          </div>
          <div className="flex gap-3">
            <div style={{ flex: 1 }}>
              <label style={labelStyle}>Nickname</label>
              <input style={inputStyle} value={form.nickname} onChange={set("nickname")} placeholder="Tatay Juan" />
            </div>
            <div style={{ width: 90 }}>
              <label style={labelStyle}>Age *</label>
              <input style={inputStyle} type="number" min="0" max="130" value={form.age} onChange={set("age")} placeholder="74" />
            </div>
          </div>
          <div>
            <label style={labelStyle}>Condition *</label>
            <input style={inputStyle} value={form.condition} onChange={set("condition")} placeholder="Mild Cognitive Impairment" />
          </div>
          <div>
            <label style={labelStyle}>Caregiver name</label>
            <input style={inputStyle} value={form.caregiver_name} onChange={set("caregiver_name")} placeholder="Maria Dela Cruz" />
          </div>

          <div style={{ borderTop: `1px solid ${C.border}`, marginTop: 6, paddingTop: 14 }}>
            <label style={labelStyle}>Medication schedule (optional — can add later)</label>
            <div className="flex flex-col gap-2">
              {meds.map((m, i) => (
                <div key={i} className="flex gap-2 items-center">
                  <input style={{ ...inputStyle, flex: 2 }} placeholder="Medication" value={m.med_name} onChange={(e) => updateMed(i, "med_name", e.target.value)} />
                  <input style={{ ...inputStyle, flex: 1 }} placeholder="Dosage" value={m.dosage} onChange={(e) => updateMed(i, "dosage", e.target.value)} />
                  <input style={{ ...inputStyle, flex: 1 }} type="time" value={m.scheduled_time} onChange={(e) => updateMed(i, "scheduled_time", e.target.value)} />
                  {meds.length > 1 && (
                    <button onClick={() => removeMedRow(i)} style={{ background: "none", border: "none", cursor: "pointer", color: C.inkFaint, flexShrink: 0 }}>
                      <Trash2 size={15} />
                    </button>
                  )}
                </div>
              ))}
            </div>
            <button
              onClick={addMedRow}
              className="flex items-center gap-1 mt-2"
              style={{ background: "none", border: "none", cursor: "pointer", color: C.teal, fontFamily: FONT_BODY, fontSize: 12.5, fontWeight: 500, padding: 0 }}
            >
              <Plus size={13} /> Add another medication
            </button>
          </div>

          {error && <p style={{ fontFamily: FONT_BODY, fontSize: 12.5, color: C.danger, margin: 0 }}>{error}</p>}

          <div className="flex gap-2 mt-2">
            <button
              onClick={onClose}
              className="flex-1"
              style={{ ...inputStyle, background: C.bgAlt, textAlign: "center", cursor: "pointer", fontWeight: 500 }}
            >
              Cancel
            </button>
            <button
              onClick={handleSubmit}
              disabled={submitting}
              className="flex-1"
              style={{
                ...inputStyle, background: C.tealDeep, color: "#fff", textAlign: "center",
                cursor: submitting ? "default" : "pointer", fontWeight: 600, opacity: submitting ? 0.7 : 1,
              }}
            >
              {submitting ? "Adding…" : "Add patient"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ============================================================
   PROVIDER VIEW
============================================================ */
function PatientListItem({ patient, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className="w-full flex items-center justify-between gap-2 rounded-xl px-3 py-2.5 text-left transition-colors"
      style={{
        background: active ? C.tealSoft : "transparent",
        border: `1px solid ${active ? C.teal : "transparent"}`,
        cursor: "pointer",
      }}
    >
      <div className="flex items-center gap-2.5 min-w-0">
        <span style={{ width: 8, height: 8, borderRadius: 999, background: statusColor(patient.status), flexShrink: 0 }} />
        <div className="min-w-0">
          <p style={{ fontFamily: FONT_BODY, fontSize: 13, fontWeight: 600, color: C.ink, margin: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {patient.name}
          </p>
          <p style={{ fontFamily: FONT_BODY, fontSize: 11, color: C.inkFaint, margin: 0 }}>{patient.condition}</p>
        </div>
      </div>
      <ChevronRight size={14} style={{ color: C.inkFaint, flexShrink: 0 }} />
    </button>
  );
}

function SkeletonLine() {
  return <div className="skeleton" style={{ height: 90, borderRadius: 10 }} />;
}

function ProviderView({ patients, selectedPatientId, onSelect, onPatientCreated, patient, vitals, adherence30, missedByTime, vitalsTrend, blockchainLog, loading }) {
  const [exportNote, setExportNote] = useState(false);
  const [showAddModal, setShowAddModal] = useState(false);
  return (
    <div className="flex flex-col md:flex-row gap-4">
      <div className="w-full md:w-56 flex-shrink-0 rounded-2xl p-3" style={{ background: C.surface, border: `1px solid ${C.border}`, height: "fit-content" }}>
        <div className="flex items-center justify-between px-1 pb-2">
          <p style={{ fontFamily: FONT_BODY, fontSize: 11, color: C.inkFaint, textTransform: "uppercase", letterSpacing: 0.6, margin: 0 }}>
            Patients
          </p>
          <button
            onClick={() => setShowAddModal(true)}
            title="Add patient"
            style={{
              display: "flex", alignItems: "center", justifyContent: "center",
              width: 22, height: 22, borderRadius: 999, background: C.tealSoft, color: C.tealDeep,
              border: "none", cursor: "pointer",
            }}
          >
            <Plus size={13} />
          </button>
        </div>
        <div className="flex flex-col gap-1">
          {patients.map((p) => (
            <PatientListItem key={p.id} patient={p} active={p.id === selectedPatientId} onClick={() => onSelect(p.id)} />
          ))}
        </div>
      </div>

      {showAddModal && (
        <AddPatientModal
          onClose={() => setShowAddModal(false)}
          onCreated={(newId) => {
            setShowAddModal(false);
            onPatientCreated(newId);
          }}
        />
      )}

      <div className="grid gap-4 flex-1 min-w-0">
        <SectionCard
          icon={Stethoscope}
          title={patient?.name}
          subtitle={patient ? `${patient.age} yrs · ${patient.condition}` : ""}
          right={
            <button
              onClick={() => setExportNote(true)}
              className="flex items-center gap-1.5 rounded-full px-3 py-1.5"
              style={{ background: C.bgAlt, border: `1px solid ${C.border}`, fontFamily: FONT_BODY, fontSize: 12, color: C.inkSoft, cursor: "pointer" }}
            >
              <Download size={13} /> Export report
            </button>
          }
        >
          {exportNote && (
            <p style={{ fontFamily: FONT_BODY, fontSize: 12, color: C.inkFaint, marginBottom: 10 }}>
              Report export will be enabled once this dashboard is connected to the backend.
            </p>
          )}
          {loading ? <SkeletonLine /> : !vitals ? (
            <p style={{ fontFamily: FONT_BODY, fontSize: 13, color: C.inkFaint }}>
              No vitals recorded yet for this patient.
            </p>
          ) : (
            <div className="flex gap-3 flex-wrap">
              <VitalCard icon={HeartPulse} label="Heart Rate" value={vitals.hr} unit="bpm" kind="hr" range="60–100 bpm" />
              <VitalCard icon={Activity} label="Oxygen (SpO2)" value={vitals.spo2} unit="%" kind="spo2" range="≥ 95%" />
              <VitalCard icon={Thermometer} label="Temperature" value={vitals.temp} unit="°C" kind="temp" range="36.1–37.4°C" />
            </div>
          )}
        </SectionCard>

        <div className="grid gap-4 grid-cols-1 md:grid-cols-2">
          <SectionCard icon={Pill} title="30-day adherence trend" subtitle="Target line at 80%">
            {loading ? <SkeletonLine /> : (
              <ResponsiveContainer width="100%" height={190}>
                <LineChart data={adherence30} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                  <CartesianGrid vertical={false} stroke={C.border} />
                  <XAxis dataKey="day" tick={{ fontFamily: FONT_MONO, fontSize: 10, fill: C.inkSoft }} axisLine={{ stroke: C.border }} tickLine={false} interval={4} />
                  <YAxis domain={[0, 100]} tick={{ fontFamily: FONT_MONO, fontSize: 11, fill: C.inkSoft }} axisLine={false} tickLine={false} />
                  <Tooltip content={<ChartTooltip unit="%" />} />
                  <ReferenceLine y={80} stroke={C.amber} strokeDasharray="4 4" />
                  <Line type="monotone" dataKey="pct" stroke={C.tealDeep} strokeWidth={2.5} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            )}
          </SectionCard>

          <SectionCard icon={Clock} title="Missed doses by time of day" subtitle="Last 30 days">
            {loading ? <SkeletonLine /> : (
              <ResponsiveContainer width="100%" height={190}>
                <BarChart data={missedByTime} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                  <CartesianGrid vertical={false} stroke={C.border} />
                  <XAxis dataKey="slot" tick={{ fontFamily: FONT_MONO, fontSize: 11, fill: C.inkSoft }} axisLine={{ stroke: C.border }} tickLine={false} />
                  <YAxis allowDecimals={false} tick={{ fontFamily: FONT_MONO, fontSize: 11, fill: C.inkSoft }} axisLine={false} tickLine={false} />
                  <Tooltip content={<ChartTooltip unit=" missed" />} cursor={{ fill: C.bgAlt }} />
                  <Bar dataKey="missed" radius={[6, 6, 0, 0]} fill={C.danger} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </SectionCard>
        </div>

        <SectionCard icon={HeartPulse} title="Vitals trend" subtitle="Daily average, last 7 days">
          {loading ? <SkeletonLine /> : (
            <>
              <div className="flex gap-4 mb-2">
                <LegendDot color={C.tealDeep} label="Heart rate (bpm)" />
                <LegendDot color={C.amber} label="SpO2 (%)" />
              </div>
              <ResponsiveContainer width="100%" height={190}>
                <LineChart data={vitalsTrend} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                  <CartesianGrid vertical={false} stroke={C.border} />
                  <XAxis dataKey="day" tick={{ fontFamily: FONT_MONO, fontSize: 11, fill: C.inkSoft }} axisLine={{ stroke: C.border }} tickLine={false} />
                  <YAxis tick={{ fontFamily: FONT_MONO, fontSize: 11, fill: C.inkSoft }} axisLine={false} tickLine={false} />
                  <Tooltip content={<ChartTooltip />} />
                  <Line type="monotone" dataKey="hr" stroke={C.tealDeep} strokeWidth={2.5} dot={false} />
                  <Line type="monotone" dataKey="spo2" stroke={C.amber} strokeWidth={2.5} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </>
          )}
        </SectionCard>

        <SectionCard icon={ShieldCheck} title="Blockchain audit log" subtitle="Tamper-proof dispensing & alert ledger">
          {loading ? <SkeletonLine /> : (
            <div className="overflow-x-auto">
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    {["Time", "Event", "Hash", "Status"].map((h) => (
                      <th key={h} style={{ fontFamily: FONT_BODY, fontSize: 11, color: C.inkFaint, textAlign: "left", padding: "0 8px 8px", fontWeight: 500, textTransform: "uppercase", letterSpacing: 0.5 }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {blockchainLog.map((row) => (
                    <tr key={row.id} style={{ borderTop: `1px solid ${C.border}` }}>
                      <td style={{ fontFamily: FONT_MONO, fontSize: 12, color: C.inkSoft, padding: "8px" }}>{row.time}</td>
                      <td style={{ fontFamily: FONT_BODY, fontSize: 13, color: C.ink, padding: "8px" }}>{row.event}</td>
                      <td style={{ fontFamily: FONT_MONO, fontSize: 12, color: C.inkFaint, padding: "8px" }}>{row.hash}</td>
                      <td style={{ padding: "8px" }}>
                        <span className="flex items-center gap-1" style={{ color: C.teal, fontFamily: FONT_BODY, fontSize: 12 }}>
                          <ShieldCheck size={13} /> Verified
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </SectionCard>
      </div>
    </div>
  );
}

function LegendDot({ color, label }) {
  return (
    <div className="flex items-center gap-1.5">
      <span style={{ width: 8, height: 8, borderRadius: 999, background: color }} />
      <span style={{ fontFamily: FONT_BODY, fontSize: 11.5, color: C.inkSoft }}>{label}</span>
    </div>
  );
}

/* ============================================================
   APP
============================================================ */
export default function App() {
  const [role, setRole] = useState("caregiver");
  const [patients, setPatients] = useState([]);
  const [selectedPatientId, setSelectedPatientId] = useState("p1");
  const [vitals, setVitals] = useState(null);
  const [schedule, setSchedule] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [adherence7, setAdherence7] = useState([]);
  const [adherence30, setAdherence30] = useState([]);
  const [missedByTime, setMissedByTime] = useState([]);
  const [vitalsTrend, setVitalsTrend] = useState([]);
  const [blockchainLog, setBlockchainLog] = useState([]);
  const [reminderConfig, setReminderConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const pollRef = useRef(null);

  useEffect(() => {
    dataService.getPatients().then(setPatients);
  }, []);

  const refreshPatients = useCallback(async () => {
    const list = await dataService.getPatients();
    setPatients(list);
    return list;
  }, []);

  const handlePatientCreated = useCallback(async (newId) => {
    await refreshPatients();
    setSelectedPatientId(newId); // triggers loadPatientData via the effect below
  }, [refreshPatients]);

  const fetchPatientData = useCallback(async (id) => {
    const [v, s, a, h7, h30, mbt, vt, bl, rc] = await Promise.all([
      dataService.getVitalsSnapshot(id),
      dataService.getTodaySchedule(id),
      dataService.getAlerts(id),
      dataService.getAdherenceHistory(id, 7),
      dataService.getAdherenceHistory(id, 30),
      dataService.getMissedByTimeSlot(id),
      dataService.getVitalsTrend(id),
      dataService.getBlockchainLog(id),
      dataService.getReminderConfig(id),
    ]);
    setVitals(v); setSchedule(s); setAlerts(a);
    setAdherence7(h7); setAdherence30(h30); setMissedByTime(mbt);
    setVitalsTrend(vt); setBlockchainLog(bl); setReminderConfig(rc);
  }, []);

  const loadPatientData = useCallback(async (id) => {
    setLoading(true);
    await fetchPatientData(id);
    setLoading(false);
  }, [fetchPatientData]);

  useEffect(() => {
    loadPatientData(selectedPatientId);
  }, [selectedPatientId, loadPatientData]);

  // Simulated real-time vitals push (demo only).
  // Swap this interval for a WebSocket/MQTT subscription to the
  // wearable's cloud gateway once the Communication Layer is live.
  useEffect(() => {
    pollRef.current = setInterval(() => {
      dataService.getVitalsSnapshot(selectedPatientId).then(setVitals);
      dataService.getAlerts(selectedPatientId).then(setAlerts);
    }, 5000);
    return () => clearInterval(pollRef.current);
  }, [selectedPatientId]);

  // Manual dispenser test controls (see DispenserTestPanel). These call
  // the exact same endpoints your ESP32 firmware will call once it's
  // wired up — this is a stand-in for the hardware, not a separate
  // code path, so testing here exercises the real pipeline end to end
  // (dispense/confirm → ledger entry → adherence recalculation).
  const [dispenseBusy, setDispenseBusy] = useState(false);
  const [dispenseError, setDispenseError] = useState(null);

  const handleManualDispense = useCallback(async (eventId) => {
    setDispenseBusy(true);
    setDispenseError(null);
    try {
      await dataService.dispenseDose(eventId);
      await fetchPatientData(selectedPatientId);
    } catch (e) {
      setDispenseError("Couldn't reach the backend — is uvicorn still running?");
    } finally {
      setDispenseBusy(false);
    }
  }, [selectedPatientId, fetchPatientData]);

  const handleManualConfirm = useCallback(async (eventId) => {
    setDispenseBusy(true);
    setDispenseError(null);
    try {
      await dataService.confirmDoseManual(eventId);
      await fetchPatientData(selectedPatientId);
    } catch (e) {
      setDispenseError("Couldn't reach the backend — is uvicorn still running?");
    } finally {
      setDispenseBusy(false);
    }
  }, [selectedPatientId, fetchPatientData]);

  const selectedPatient = patients.find((p) => p.id === selectedPatientId);

  return (
    <div style={{ background: C.bg, minHeight: "100%", fontFamily: FONT_BODY, color: C.ink }} className="p-4 md:p-6">
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
        @keyframes pulseRing { 0% { box-shadow: 0 0 0 0 rgba(198,138,46,0.45);} 70% { box-shadow: 0 0 0 9px rgba(198,138,46,0);} 100% { box-shadow: 0 0 0 0 rgba(198,138,46,0);} }
        @keyframes pulseDot { 0%,100% { opacity:1; } 50% { opacity:.35; } }
        .pulse-ring { animation: pulseRing 2.2s infinite; }
        .pulse-dot { animation: pulseDot 2s infinite; }
        @media (prefers-reduced-motion: reduce) { .pulse-ring, .pulse-dot { animation: none; } }
        .skeleton { background: linear-gradient(90deg, ${C.bgAlt} 25%, ${C.surfaceAlt} 37%, ${C.bgAlt} 63%); background-size: 400% 100%; animation: shimmer 1.4s ease infinite; }
        @keyframes shimmer { 0% { background-position: 100% 50%; } 100% { background-position: 0 50%; } }
        button:focus-visible, [tabindex]:focus-visible { outline: 2px solid ${C.teal}; outline-offset: 2px; }
      `}</style>

      <div className="max-w-[1080px] mx-auto flex flex-col gap-5">
        {/* HEADER */}
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-3">
            <span
              className="flex items-center justify-center rounded-2xl"
              style={{ width: 40, height: 40, background: C.tealDeep, color: C.amberSoft }}
            >
              <Pill size={20} />
            </span>
            <div>
              <h1 style={{ fontFamily: FONT_DISPLAY, fontSize: 20, fontWeight: 700, margin: 0, color: C.ink }}>
                Gabay
              </h1>
              <p style={{ fontFamily: FONT_BODY, fontSize: 12, color: C.inkFaint, margin: 0 }}>
                Medication & vitals monitoring
              </p>
            </div>
          </div>
          <RoleSwitch role={role} setRole={setRole} />
        </div>

        {/* CONNECTION STATUS */}
        <div className="flex items-center gap-2 flex-wrap">
          <ConnectionPill icon={Wifi} label="Dispenser online" />
          <ConnectionPill icon={Radio} label="Wearable paired" />
          <ConnectionPill icon={Link2} label="Blockchain synced" />
          <span style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.inkFaint, marginLeft: 4 }}>
            Live vitals refresh every 5s (demo)
          </span>
        </div>

        {/* MAIN CONTENT */}
        {role === "caregiver" ? (
          <CaregiverView
            patients={patients}
            selectedPatientId={selectedPatientId}
            onSelectPatient={setSelectedPatientId}
            patient={selectedPatient}
            vitals={vitals}
            schedule={schedule}
            alerts={alerts}
            adherence7={adherence7}
            loading={loading}
            onDispense={handleManualDispense}
            onConfirm={handleManualConfirm}
            dispenseBusy={dispenseBusy}
            dispenseError={dispenseError}
            reminderConfig={reminderConfig}
            onReminderUpdate={setReminderConfig}
          />
        ) : (
          <ProviderView
            patients={patients}
            selectedPatientId={selectedPatientId}
            onSelect={setSelectedPatientId}
            onPatientCreated={handlePatientCreated}
            patient={selectedPatient}
            vitals={vitals}
            adherence30={adherence30}
            missedByTime={missedByTime}
            vitalsTrend={vitalsTrend}
            blockchainLog={blockchainLog}
            loading={loading}
          />
        )}
      </div>
    </div>
  );
}
