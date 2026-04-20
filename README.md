# AgenticTerps — AI-Powered Pharmaceutical Cold-Chain Monitoring

[![GCP](https://img.shields.io/badge/Platform-Google_Cloud-4285F4?logo=google-cloud)](https://cloud.google.com)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python)](https://python.org)
[![Node.js](https://img.shields.io/badge/Node.js-20+-339933?logo=node.js)](https://nodejs.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi)](https://fastapi.tiangolo.com)

A multi-agent AI system for real-time pharmaceutical cold-chain monitoring that uses Claude AI to autonomously detect shipment anomalies, reason about compound risks, and coordinate cascading recovery responses — with human-in-the-loop approval for high-stakes decisions.

---

## 🎯 System Overview

**The Problem:** Temperature-sensitive pharmaceutical shipments (vaccines, biologics) require continuous monitoring. Traditional threshold alerts are insufficient — compound risks (e.g., temperature breach + shock + flight delay) exponentially increase spoilage probability.

**The Solution:** An agentic AI system that:
- Monitors live telemetry from shipment sensors
- Evaluates compound risks using Claude AI
- Generates context-aware recovery plans with LangChain tool orchestration
- Executes multi-channel notifications after human approval
- Maintains FDA-compliant audit trails

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         AGENTICTERPS ARCHITECTURE                       │
└─────────────────────────────────────────────────────────────────────────┘

┌──────────────┐
│ UI Simulator │ (Browser sends temp, humidity, shock, delay data)
└──────┬───────┘
       │ HTTP POST
       ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  SERVICE B: Telemetry Ingestion (Cloud Function)                        │
│  • Validates sensor data                                                │
│  • Writes to Firestore /shipments/{drug_id}/live_telemetry              │
│  • Publishes to telemetry-stream Pub/Sub topic                          │
└──────┬──────────────────────────────────────────────────────────────────┘
       │ Pub/Sub: telemetry-stream
       ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  SERVICE C: Anomaly Detection Agent (Cloud Run + FastAPI + Claude)      │
│  • Fetches thresholds from Firestore                                    │
│  • Claude evaluates compound risks                                      │
│  • Calculates spoilage probability                                      │
└──────┬──────────────────────────────────────────────────────────────────┘
       │ Pub/Sub: risk-detected (if CRITICAL)
       ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  SERVICE D: Orchestration Agent (Cloud Run + LangChain + Claude)        │
│  • Tool 1: Check live flight data (Airlabs API)                         │
│  • Tool 2: Calculate spoilage (how many doses salvageable)              │
│  • Tool 3: Draft emails for hospital/manufacturer/carrier               │
│  • Generates recovery plan with reasoning                               │
│  • Writes to Firestore /pending-approvals                               │
└──────┬──────────────────────────────────────────────────────────────────┘
       │
       ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  FIRESTORE: /pending-approvals/{approval_id}                            │
│  status: "pending" → waiting for human operator                         │
└──────┬──────────────────────────────────────────────────────────────────┘
       │
       ↓
┌──────────────┐
│ HUMAN HITL   │ (Operator reviews plan in dashboard)
│ [APPROVE]    │ ← Human clicks approve button
└──────┬───────┘
       │ Updates Firestore: status = "approved"
       │ Pub/Sub: execute-actions
       ↓
┌─────────────────────────────────────────────────────────────────────────┐
│  SERVICE E: Execution Agent (Cloud Run)                                 │
│  • Sends emails (Gmail SMTP) to stakeholders                            │
│  • Makes voice call (ElevenLabs TTS → Twilio)                           │
│  • Writes audit log to BigQuery                                         │
│  • Updates Firestore: status = "completed"                              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 📦 Services

### Service A — Bootstrap Seed (One-Time Setup)
**Purpose:** Extract pharmaceutical constraints from FDA drug labels and seed Firestore

**Technology:** Python script (not a web service)

**What it does:**
1. Reads 3 pharmaceutical drug label PDFs
2. Uses Claude AI to extract structured monitoring thresholds
3. Validates with Pydantic schemas
4. Writes to Firestore `/shipments/{drug_id}`

**Runs:** Once before any other service

---

### Service B — Telemetry Ingestion API
**Purpose:** Receive and validate sensor readings

**Technology:** Cloud Function (Python 3.12) + HTTP trigger

**What it does:**
1. Receives HTTP POST from UI simulator with telemetry payload
2. Validates schema (drug_id, temperature, humidity, shock, flight delay)
3. Confirms shipment exists in Firestore (prevents phantom drug IDs)
4. Writes `live_telemetry` to Firestore (dashboard real-time update)
5. Publishes validated payload to `telemetry-stream` Pub/Sub topic

**Triggers:** HTTP POST from browser UI
**Publishes to:** `telemetry-stream`

---

### Service C — Anomaly Detection Agent
**Purpose:** Evaluate compound risks using AI

**Technology:** Cloud Run + FastAPI + Claude Haiku 4.5

**What it does:**
1. Receives Pub/Sub push from `telemetry-stream`
2. Fetches shipment metadata from Firestore
3. Calls Claude AI with comprehensive prompt containing:
   - All thresholds (temp, humidity, shock, excursion, freeze)
   - Live telemetry reading
   - Stability profile and regulatory context
4. Claude returns structured JSON risk assessment:
   - Risk level (CRITICAL/HIGH/MEDIUM/LOW/NONE)
   - Breach-by-breach analysis with deviation calculations
   - Compound risk explanation
   - Spoilage likelihood and viable units estimate
   - Recommended actions
5. If risk detected: publishes rich payload to `risk-detected`

**Subscribes to:** `telemetry-stream`
**Publishes to:** `risk-detected`

---

### Service D — Orchestration Agent
**Purpose:** Generate context-aware recovery plans

**Technology:** Cloud Run + FastAPI + LangChain + Claude Sonnet 4.6

**What it does:**
1. Receives Pub/Sub push from `risk-detected`
2. Runs LangChain agent with 3 tools:
   - **Flight Tracker:** Checks live flight status via Airlabs API
   - **Spoilage Calculator:** Estimates viable units and financial impact
   - **Email Drafter:** Generates stakeholder-specific emails
3. Agent reasons autonomously, calling tools as needed
4. Produces structured recovery plan with:
   - Agent reasoning (WHY this plan)
   - UI summary (6 critical bullets)
   - Spoilage assessment
   - 7+ step recovery actions with pre-written emails
5. Writes to Firestore `/pending-approvals` (status: "pending")

**Subscribes to:** `risk-detected`
**Writes to:** Firestore `/pending-approvals`

---

### Service E — Execution Agent
**Purpose:** Execute approved recovery plans

**Technology:** Cloud Run + FastAPI

**What it does:**
1. Receives Pub/Sub push from `execute-actions` (published after human approval)
2. Iterates over recovery actions:
   - Sends pre-written emails via Gmail SMTP
   - Logs non-email actions
3. Delivers voice notification:
   - Generates TTS audio with ElevenLabs
   - Uploads to GCS bucket
   - Initiates Twilio call to primary contact
4. Writes immutable audit log to BigQuery `compliance_trail.audit_log`
5. Marks Firestore `/pending-approvals/{id}` as status: "completed"

**Subscribes to:** `execute-actions`
**Writes to:** BigQuery, Firestore, GCS
**Integrations:** Gmail, Twilio, ElevenLabs

---

### Frontend — Dashboard & Approval UI
**Purpose:** Operator interface for monitoring and approval

**Technology:** Node.js + Express + EJS templates + Firebase Admin SDK

**What it does:**
1. Displays all shipments with live telemetry (Firestore real-time listeners)
2. Allows operators to simulate telemetry changes (sends to Service B)
3. Shows pending approvals queue
4. Provides approval workflow:
   - Review recovery plan
   - Click "Approve"
   - Publishes to `execute-actions` Pub/Sub topic

**Deployment:** Cloud Run (containerized)

---

## 🔄 Data Flow

### Fixed Drug IDs (System-Wide)
```
pfizer-001   → Pfizer COMIRNATY (mRNA vaccine, -90°C to -60°C)
moderna-001  → Moderna Spikevax (mRNA vaccine, -50°C to -15°C)
jynneos-001  → JYNNEOS (smallpox/monkeypox vaccine, +2°C to +8°C)
```

### Firestore Collections
```
/shipments/{drug_id}                  ← Written by Service A (thresholds)
/shipments/{drug_id}/live_telemetry   ← Updated by Service B (real-time)
/pending-approvals/{approval_id}      ← Written by Service D (recovery plans)
```

### Pub/Sub Topics
```
telemetry-stream   → Service B publishes, Service C subscribes
risk-detected      → Service C publishes, Service D subscribes
execute-actions    → Frontend publishes, Service E subscribes
dead-letter        → Failed message graveyard (5 retry attempts)
```

---

## 🚀 Reproducibility Guide

### Prerequisites

**Software:**
- Python 3.11+
- Node.js 20+
- Docker
- Google Cloud SDK (`gcloud`)
- Terraform 1.5+

**External Accounts:**
- Google Cloud project with billing enabled
- [Anthropic API key](https://console.anthropic.com)
- [Airlabs API key](https://airlabs.co) (for Service D flight tracking)
- Gmail account + [app password](https://support.google.com/accounts/answer/185833)
- [Twilio account](https://www.twilio.com) (Account SID, Auth Token, Phone Number)
- [ElevenLabs API key](https://elevenlabs.io) (for TTS voice generation)

---

### Step 1: Infrastructure Provisioning (Terraform)

**1.1. Update Terraform variables**

Edit `IaC/terraform.tfvars`:
```hcl
project_id         = "YOUR-GCP-PROJECT-ID"
region             = "us-central1"
firestore_location = "nam5"
```

**1.2. Initialize and apply Terraform**
```bash
cd IaC
terraform init
terraform apply
```

**Provisions:**
- Firestore database `cargo-monitor`
- Pub/Sub topics: `telemetry-stream`, `risk-detected`, `execute-actions`, `dead-letter`
- BigQuery dataset `compliance_trail` with `audit_log` table
- GCS buckets for PDFs and voice notes
- Artifact Registry repository `agenticterps`
- Service accounts with IAM bindings for all services

---

### Step 2: Service A — Bootstrap Seed (One-Time)

**2.1. Download pharmaceutical PDFs**

Create `services/service_a/pdfs/` directory and download:

| File | Download Link |
|------|---------------|
| `pfizer-comirnaty.pdf` | [CDC Pfizer Storage Summary](https://www.cdc.gov/vaccines/covid-19/info-by-product/pfizer/downloads/storage-summary.pdf) |
| `moderna-spikevax.pdf` | [CDC Moderna Storage Summary](https://www.cdc.gov/vaccines/covid-19/info-by-product/moderna/downloads/storage-summary.pdf) |
| `jynneos-mpox.pdf` | [FDA JYNNEOS Label](https://www.fda.gov/media/131078/download) |

**2.2. Configure environment**

Create `.env` in repository root:
```env
ANTHROPIC_API_KEY=sk-ant-...
SERVICE_ACCOUNT_EMAIL=service-a-seed@YOUR-PROJECT-ID.iam.gserviceaccount.com
GOOGLE_CLOUD_PROJECT=YOUR-PROJECT-ID
FIRESTORE_DATABASE=cargo-monitor
```

**2.3. Run seed script**
```bash
cd services/service_a
pip install -r requirements.txt
python seed.py
```

**Expected output:**
```
INFO | Seeding 3 shipments into Firestore
INFO | Processing: Pfizer COMIRNATY (pfizer-001)
INFO | Written to Firestore: /shipments/pfizer-001
INFO | Processing: Moderna Spikevax (moderna-001)
INFO | Written to Firestore: /shipments/moderna-001
INFO | Processing: JYNNEOS (jynneos-001)
INFO | Written to Firestore: /shipments/jynneos-001
INFO | Seeding complete — 3 succeeded, 0 failed
```

**2.4. Verify in Firestore Console**

Navigate to: GCP Console → Firestore → Data

You should see collection `shipments` with 3 documents containing extracted thresholds.

---

### Step 3: Deploy Services B–E

**3.1. Authenticate Docker for Artifact Registry**
```bash
gcloud auth login
gcloud config set project YOUR-PROJECT-ID
gcloud auth configure-docker us-central1-docker.pkg.dev
```

---

#### Service B — Telemetry Ingestion (Cloud Function)

```bash
gcloud functions deploy ingest-telemetry \
  --gen2 --runtime python312 --region us-central1 \
  --source services/service_b \
  --entry-point ingest_telemetry \
  --trigger-http --allow-unauthenticated \
  --service-account service-b-telemetry@YOUR-PROJECT-ID.iam.gserviceaccount.com \
  --set-env-vars GOOGLE_CLOUD_PROJECT=YOUR-PROJECT-ID,FIRESTORE_DATABASE=cargo-monitor,TELEMETRY_TOPIC=telemetry-stream
```

**Note the deployed URL** — you'll need it for the frontend configuration.

---

#### Service C — Anomaly Detection (Cloud Run)

**Create `.env.yaml`:**
```yaml
# services/service_c/.env.yaml
GOOGLE_CLOUD_PROJECT: "YOUR-PROJECT-ID"
FIRESTORE_DATABASE: "cargo-monitor"
RISK_TOPIC: "risk-detected"
ANTHROPIC_API_KEY: "sk-ant-..."
```

**Build and deploy:**
```bash
IMAGE="us-central1-docker.pkg.dev/YOUR-PROJECT-ID/agenticterps/service-c:latest"
docker build -t $IMAGE ./services/service_c
docker push $IMAGE

gcloud run deploy service-c-monitoring \
  --image $IMAGE --region us-central1 \
  --service-account service-c-monitoring@YOUR-PROJECT-ID.iam.gserviceaccount.com \
  --env-vars-file services/service_c/.env.yaml \
  --allow-unauthenticated
```

**Copy the Service URL** and update Terraform:

Edit `IaC/terraform.tfvars`:
```hcl
service_c_url = "https://service-c-monitoring-XXXXX-uc.a.run.app"
```

Then reapply Terraform to update Pub/Sub subscription:
```bash
cd IaC
terraform apply
```

---

#### Service D — Orchestration (Cloud Run)

**Create `.env`:**
```env
# services/service_d/.env
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_CLOUD_PROJECT=YOUR-PROJECT-ID
FIRESTORE_DATABASE=cargo-monitor
AIRLABS_API_KEY=YOUR-AIRLABS-KEY
```

**Build and deploy:**
```bash
IMAGE="us-central1-docker.pkg.dev/YOUR-PROJECT-ID/agenticterps/service-d:latest"
docker build -t $IMAGE ./services/service_d
docker push $IMAGE

gcloud run deploy service-d-orchestrator \
  --image $IMAGE --region us-central1 \
  --service-account service-d-orchestrator@YOUR-PROJECT-ID.iam.gserviceaccount.com \
  --set-env-vars GOOGLE_CLOUD_PROJECT=YOUR-PROJECT-ID,FIRESTORE_DATABASE=cargo-monitor,ANTHROPIC_API_KEY=sk-ant-...,AIRLABS_API_KEY=... \
  --allow-unauthenticated
```

**Update Terraform with Service URL:**
```hcl
service_d_url = "https://service-d-orchestrator-XXXXX-uc.a.run.app"
```

```bash
cd IaC && terraform apply
```

---

#### Service E — Execution (Cloud Run)

**Create `.env.yaml`:**
```yaml
# services/service_e/.env.yaml
GOOGLE_CLOUD_PROJECT: "YOUR-PROJECT-ID"
FIRESTORE_DATABASE: "cargo-monitor"
GMAIL_USER: "your-email@gmail.com"
GMAIL_APP_PASSWORD: "your-app-password"
TWILIO_ACCOUNT_SID: "ACxxxxx"
TWILIO_AUTH_TOKEN: "your-auth-token"
TWILIO_PHONE_NUMBER: "+1234567890"
ELEVENLABS_API_KEY: "your-elevenlabs-key"
ELEVENLABS_VOICE_ID: "21m00Tcm4TlvDq8ikWAM"
VOICE_NOTES_BUCKET: "YOUR-PROJECT-ID-voice-notes"
```

**Build and deploy:**
```bash
IMAGE="us-central1-docker.pkg.dev/YOUR-PROJECT-ID/agenticterps/service-e:latest"
docker build -t $IMAGE ./services/service_e
docker push $IMAGE

gcloud run deploy service-e-execution \
  --image $IMAGE --region us-central1 \
  --service-account service-e-execution@YOUR-PROJECT-ID.iam.gserviceaccount.com \
  --env-vars-file services/service_e/.env.yaml \
  --allow-unauthenticated
```

**Update Terraform:**
```hcl
service_e_url = "https://service-e-execution-XXXXX-uc.a.run.app"
```

```bash
cd IaC && terraform apply
```

---

#### Frontend — Dashboard UI (Cloud Run)

**Update frontend configuration:**

Edit `frontend/server.js` and update:
- Project ID
- Firestore database name
- Service B URL (from Cloud Function deployment)

**Build and deploy:**
```bash
IMAGE="us-central1-docker.pkg.dev/YOUR-PROJECT-ID/agenticterps/frontend:latest"
docker build -t $IMAGE ./frontend
docker push $IMAGE

gcloud run deploy frontend-ui \
  --image $IMAGE --region us-central1 \
  --service-account frontend-ui@YOUR-PROJECT-ID.iam.gserviceaccount.com \
  --port 8080 --allow-unauthenticated
```

---

### Step 4: Validation

**4.1. Open the frontend**

Navigate to the deployed frontend URL. You should see:
- 3 shipments loaded (pfizer-001, moderna-001, jynneos-001)
- Live telemetry controls (temperature, humidity, shock sliders)

**4.2. Trigger an anomaly**

1. Select a shipment
2. Adjust temperature outside threshold range
3. Click "Save Updates"
4. Check Cloud Run logs for Service C — should see risk assessment
5. Check Firestore `/pending-approvals` — new document should appear

**4.3. Test approval workflow**

1. Refresh frontend
2. Navigate to "Pending Approvals" section
3. Review generated recovery plan
4. Click "Approve"
5. Check Service E logs — should see email sends and audit log writes
6. Check BigQuery `compliance_trail.audit_log` — new row should appear

---

## 🔍 Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `ANTHROPIC_API_KEY is not set` | Missing environment variable | Add to `.env` or `.env.yaml` |
| `Shipment not found in Firestore` | Service A not run or failed | Run `python seed.py` again |
| `403 PERMISSION_DENIED` | Service account missing IAM role | Check Terraform service account bindings |
| `Pub/Sub message not delivered` | Subscription endpoint mismatch | Update Terraform vars with correct Cloud Run URLs |
| `Cloud Run invocation failed` | Missing `run.invoker` permission | Terraform should grant this — verify with `gcloud iam service-accounts describe` |
| `Email send failed` | Invalid Gmail app password | Generate new app password from Google Account Security |
| `Twilio call failed` | Invalid phone number format | Use E.164 format: `+1234567890` |

---

## 📊 Technology Stack

| Component | Technology |
|-----------|------------|
| **Cloud Platform** | Google Cloud Platform (GCP) |
| **AI/ML** | Claude AI (Anthropic) — Haiku 4.5 & Sonnet 4.6 |
| **Orchestration** | LangChain (tool-augmented reasoning) |
| **Backend Services** | FastAPI (Python) + Cloud Run |
| **Telemetry API** | Cloud Functions Gen2 (Python 3.12) |
| **Frontend** | Node.js + Express + EJS |
| **Database** | Firestore (Native mode) |
| **Messaging** | Pub/Sub (event-driven architecture) |
| **Analytics** | BigQuery (compliance audit trail) |
| **Storage** | Cloud Storage (voice notes) |
| **Infrastructure** | Terraform (IaC) |
| **Notifications** | Gmail SMTP, Twilio Voice, ElevenLabs TTS |

---

## 🙏 Acknowledgments

- **Anthropic** for Claude AI API
- **Google Cloud Platform** for cloud infrastructure
- **LangChain** for agent orchestration framework
- **Airlabs** for real-time flight data API
- **Twilio** and **ElevenLabs** for voice notification stack