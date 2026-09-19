# Running Gabay in VS Code

This folder has two independent projects that need to run **at the
same time**, in two separate terminals:

```
gabay-fullstack/
├── backend/     — FastAPI + SQLite + analytics (Python)
└── frontend/    — the dashboard (React + Vite)
```

## 0. One-time prerequisites

Install these if you don't already have them:

- **Python 3.10+** — check with `python3 --version` (Windows: `python --version`)
- **Node.js 18+** (includes npm) — check with `node --version`
- **VS Code** with the **Python** extension (ms-python.python) installed — search it in the Extensions panel (`Ctrl+Shift+X` / `Cmd+Shift+X`)

## 1. Open the project

`File → Open Folder…` → select the `gabay-fullstack` folder. VS Code will show both `backend/` and `frontend/` in the sidebar.

## 2. Open two terminals

`` Terminal → New Terminal `` (repeat once more, or click the split-terminal icon) so you have two side by side — one per project. Everything below assumes Terminal A = backend, Terminal B = frontend.

## 3. Terminal A — start the backend

```bash
cd backend
python3 -m venv venv
```

Activate the virtual environment:
```bash
# macOS/Linux
source venv/bin/activate
# Windows (PowerShell)
venv\Scripts\Activate.ps1
# Windows (cmd.exe)
venv\Scripts\activate.bat
```
Your terminal prompt should now show `(venv)` at the start of the line.

```bash
pip install -r requirements.txt
cd app
python seed.py
uvicorn main:app --reload --port 8000
```

Leave this running. Open **http://localhost:8000/docs** in a browser — if you see the FastAPI interactive docs page, the backend is up.

> `python seed.py` resets the database and reloads 3 sample patients with 90 days of history. Re-run it any time you want to reset back to a clean demo state — it's safe to run repeatedly.

## 4. Terminal B — start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open the URL it prints (**http://localhost:5173**). You should see the Gabay dashboard, now loading real data from the backend instead of mock data — patient names, adherence charts, and the blockchain log are all coming from `backend/gabay.db`.

## 5. Confirm it's actually connected (not just running)

- Switch the role toggle to **Healthcare Provider** and click between the 3 patients in the sidebar — each should show different adherence numbers.
- Open the **Blockchain audit log** section — hashes should be long hex strings (not the placeholder `0x8f3a...` style text), confirming this is real data from `blockchain.py`.
- If anything shows blank/loading forever, see Troubleshooting below.

## Stopping everything

`Ctrl+C` in each terminal. Next time, you only need to repeat the **last command** in each (`uvicorn main:app --reload --port 8000` and `npm run dev`) — the `pip install`/`npm install`/`venv` setup is one-time, and you only need to activate the venv again (step 3's activation command) each new terminal session.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `uvicorn: command not found` | The venv isn't activated — repeat the activation command in step 3 (you should see `(venv)` in the prompt). |
| PowerShell says running scripts is disabled | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that same PowerShell window, then retry activation. |
| Dashboard shows blank patient list / stuck loading | Backend probably isn't running, or `seed.py` was never run. Check Terminal A for errors, confirm `http://localhost:8000/docs` loads. |
| Browser console shows "Failed to fetch" | Same as above — the frontend can't reach `http://localhost:8000`. Confirm Terminal A is still running. |
| Port 8000 or 5173 already in use | Something else is using it. Either stop that process, or change the port (`uvicorn main:app --reload --port 8001` and update `API_BASE` in `frontend/src/App.jsx` to match; for the frontend, `npm run dev -- --port 5174`). |
| Want a completely fresh demo | Delete `backend/gabay.db`, then re-run `python seed.py` in Terminal A. |

## Optional but useful VS Code extensions

- **ES7+ React/Redux/React-Native snippets** — handy shortcuts while editing `App.jsx`
- **Thunder Client** or **REST Client** — lets you hit backend endpoints (e.g. `GET http://localhost:8000/patients/p2/risk/upcoming`) directly inside VS Code instead of a browser, useful once you start wiring up the ESP32.

## Where things stand after this

- ✅ Dashboard and backend are connected — no more mock data
- ❌ Still nothing connecting to real hardware — `POST /patients/{id}/vitals` and `POST /dispense-events/{id}/confirm` are waiting for your ESP32 firmware to actually call them (see `backend/README.md` for the exact request format)
