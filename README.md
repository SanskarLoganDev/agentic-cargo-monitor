# Agentic Cargo Monitor

An agentic AI system for real-time pharmaceutical cold-chain monitoring.  
The system uses Claude AI to autonomously detect anomalies in shipment telemetry, reason about risks, and coordinate cascading responses — while keeping a human in the loop for high-stakes decisions.

---

## Project Overview

A pharmaceutical distributor ships temperature-sensitive drugs (vaccines, biologics, insulin) using OnAsset SENTRY-equipped containers. This system:

1. **Reads drug labels** (PDFs) to extract monitoring thresholds — handled by **Service A**
2. **Simulates live telemetry** (temperature, humidity, shock) via a web UI dashboard
3. **Monitors for threshold breaches** using a rule engine
4. **Reasons about risks** using a Claude AI agentic loop with tool use
5. **Executes cascading actions** (reroute, notify hospital, escalate customs, log compliance) with human approval gates

---

## Repository Structure

```
agentic-cargo-monitor/          ← repo root — venv and .env live here
├── .env                        ← your secrets (gitignored — never commit)
├── .env.example                ← template — copy this to .env
├── requirements.txt            ← all dependencies for all services
├── README.md
└── services/
    ├── service_a/              ← PDF intake + Claude extraction  (built)
    │   ├── main.py
    │   ├── events.py
    │   ├── agents/
    │   │   └── intake_agent.py
    │   ├── routers/
    │   │   └── upload.py
    │   ├── schemas/
    │   │   └── shipment.py
    │   ├── db/
    │   │   ├── database.py
    │   │   └── models.py
    │   └── uploads/
    ├── service_b/              ← Telemetry ingestion API         (coming)
    ├── service_c/              ← Monitoring & anomaly detection  (coming)
    ├── service_d/              ← Orchestrator agent              (coming)
    └── service_e/              ← Execution agent + HITL          (coming)
```

---

## Service A — Intake Agent

**Responsibility:** Accept a pharmaceutical drug label PDF, use Claude AI to extract structured shipment monitoring parameters, and store them in SQLite. Publishes a `shipment.created` event so downstream services know what thresholds to enforce.

### What it extracts from a PDF

| Field | Example |
|---|---|
| `drug_name` | COMIRNATY (BNT162b2) |
| `temp_min_celsius` | -90.0 |
| `temp_max_celsius` | -60.0 |
| `max_excursion_duration_minutes` | 30 |
| `do_not_freeze` | false |
| `light_sensitive` | true |
| `shake_sensitive` | false |
| `iata_handling_codes` | ["PIL", "ACT", "EMD"] |
| `regulatory_framework` | EU GDP 2013/C 343/01 |

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11+ | `python --version` to check |
| pip | latest | `pip install --upgrade pip` |
| Anthropic API key | — | Get from [console.anthropic.com](https://console.anthropic.com) |

---

## Setup (one-time)

All commands run from the **repo root** (`agentic-cargo-monitor/`).

### Step 1 — Clone the repo

```bash
git clone <your-repo-url>
cd agentic-cargo-monitor
```

### Step 2 — Create a virtual environment at the repo root

**Windows:**
```powershell
python -m venv .venv
.venv\Scripts\activate
```

**macOS / Linux:**
```bash
python -m venv .venv
source .venv/bin/activate
```

### Step 3 — Install dependencies

```powershell
pip install -r requirements.txt
```

### Step 4 — Set up your environment variables

```powershell
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Open `.env` and add your Anthropic API key:

```env
ANTHROPIC_API_KEY=sk-ant-...
```

> `.env` is gitignored. Never commit your API key to git.

---

## Running Service A

From the **repo root**, with your venv activated:

```powershell
uvicorn main:app --reload --port 8001 --app-dir services/service_a
```

Expected output:
```
INFO | Starting Service A — Intake Agent
INFO | SQLite tables ready
INFO | IntakeAgent ready
INFO:     Uvicorn running on http://127.0.0.1:8001 (Press CTRL+C to quit)
```

Then open **http://127.0.0.1:8001/docs** to see the Swagger UI.

> **Common mistake:** Running `uvicorn main:app` from the repo root *without* `--app-dir services/service_a`
> will fail with `Could not import module "main"` because `main.py` is inside `services/service_a/`, not at the root.

---

## Testing the Upload

### Option A — Swagger UI (easiest)

1. Go to **http://127.0.0.1:8001/docs**
2. Click `POST /upload/pdf` → **Try it out**
3. Click **Choose File**, select a drug label PDF
4. Click **Execute**
5. The response body will contain the full extracted `ShipmentRecord` JSON

### Option B — curl

```bash
curl -X POST http://127.0.0.1:8001/upload/pdf \
  -F "file=@path/to/drug_label.pdf" \
  -H "accept: application/json"
```

### Option C — Python requests

```python
import requests

with open("pfizer_storage_summary.pdf", "rb") as f:
    response = requests.post(
        "http://127.0.0.1:8001/upload/pdf",
        files={"file": ("pfizer_storage_summary.pdf", f, "application/pdf")},
    )

print(response.json())
```

---

## Recommended Test PDFs

Download any of these and upload via `/upload/pdf`:

| Drug | Category | PDF URL |
|---|---|---|
| **Pfizer COMIRNATY** (mRNA vaccine) | ultra_cold | [CDC Storage Summary](https://www.cdc.gov/vaccines/covid-19/info-by-product/pfizer/downloads/storage-summary.pdf) |
| **Moderna Spikevax** (mRNA vaccine) | deep_frozen | [CDC Storage Summary](https://www.cdc.gov/vaccines/covid-19/info-by-product/moderna/downloads/storage-summary.pdf) |
| **Keytruda** (pembrolizumab) | refrigerated | [Merck Prescribing Info](https://www.merck.com/product/usa/pi_circulars/k/keytruda/keytruda_pi.pdf) |
| **Herceptin** (trastuzumab) | refrigerated | [Genentech PI](https://www.gene.com/download/pdf/herceptin_prescribing.pdf) |

### Expected response

```json
{
  "id": 1,
  "shipment_id": "3f7a2e1b-...",
  "drug_name": "COMIRNATY (BNT162b2)",
  "manufacturer": "Pfizer-BioNTech",
  "cargo_category": "vaccine",
  "temp_classification": "ultra_cold",
  "temp_min_celsius": -90.0,
  "temp_max_celsius": -60.0,
  "max_excursion_duration_minutes": 30,
  "do_not_freeze": false,
  "light_sensitive": true,
  "iata_handling_codes": ["PIL", "ACT", "EMD"],
  "regulatory_framework": "EU GDP 2013/C 343/01",
  "extraction_model": "claude-sonnet-4-6",
  "created_at": "2025-01-15T10:30:00",
  "status": "active"
}
```

---

## Cloud vs Local Equivalence

| Cloud (original proposal) | Local (this implementation) |
|---|---|
| Google Cloud Storage (GCS) | `/uploads/` folder |
| Eventarc trigger | `POST /upload/pdf` endpoint |
| Firestore | SQLite via SQLAlchemy |
| Pub/Sub topic | `asyncio.Queue` in `events.py` |
| LangChain | Direct `anthropic` SDK |

---

## For Teammates — Adding Services B, C, D, E

1. Create `services/service_b/` with its own `main.py`
2. Add any new dependencies to the root `requirements.txt`
3. Run with: `uvicorn main:app --reload --port 8002 --app-dir services/service_b`
4. To receive events from Service A: `from services.service_a.events import consume`

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Could not import module "main"` | Running uvicorn from wrong directory | Add `--app-dir services/service_a` to the command |
| `ANTHROPIC_API_KEY not set` | Missing or empty `.env` | Check `.env` exists at repo root and key is filled in |
| `422 — scanned image PDF` | pypdf found no text | Use a digitally-created PDF, not a scanned document |
| `422 — Only PDF files accepted` | Wrong file type | Upload a `.pdf` file |
| `422 — AI extraction failed` | Claude could not parse the document | Ensure it's a drug label or prescribing info PDF |
| Port 8001 already in use | Another process on that port | Use `--port 8002` or stop the other process |

---

## Tech Stack

| Component | Technology |
|---|---|
| API framework | FastAPI |
| AI model | Claude claude-sonnet-4-6 (Anthropic) |
| PDF parsing | pypdf |
| Data validation | Pydantic v2 |
| Database | SQLite via SQLAlchemy (swappable to PostgreSQL) |
| Event bus | asyncio.Queue (swappable to Redis) |
| Server | Uvicorn |
