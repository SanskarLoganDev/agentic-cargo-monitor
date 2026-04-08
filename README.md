# Agentic Cargo Monitor

An agentic AI system for real-time pharmaceutical cold-chain monitoring.
Uses Claude AI to autonomously detect shipment anomalies, reason about risk,
and coordinate cascading responses — with a human in the loop for high-stakes decisions.

---

## Architecture Overview

```
services/
├── service_a/   ← Bootstrap seed script — runs once        (built)
├── service_b/   ← Telemetry ingestion Cloud Function       (coming)
├── service_c/   ← Monitoring agent Cloud Function          (coming)
├── service_d/   ← Orchestrator agent Cloud Function        (coming)
└── service_e/   ← Execution agent Cloud Function           (coming)

IaC/             ← Terraform — provisions GCP infrastructure
```

**Platform:** Google Cloud Platform (GCP)
**Services B–E:** Cloud Functions triggered by Pub/Sub push subscriptions
**Database:** Firestore (real-time listeners feed the dashboard directly)
**AI model:** Claude claude-sonnet-4-6 via Anthropic API (direct SDK — no LangChain)

### The three monitored shipments (fixed)

| Drug ID | Drug | Category | Temp range |
|---|---|---|---|
| `pfizer-001` | Pfizer COMIRNATY (BNT162b2) | mRNA vaccine | −90°C to −60°C |
| `moderna-001` | Moderna Spikevax (mRNA-1273) | mRNA vaccine | −50°C to −15°C |
| `herceptin-001` | Herceptin (trastuzumab) | Oncology biologic | +2°C to +8°C |

---

## Service A — Bootstrap Seed Script

Service A is **not** a web service. It is a one-time script that:

1. Reads three pharmaceutical drug label PDFs from `pdfs/`
2. Extracts text from each using `pypdf`
3. Calls Claude API to extract structured monitoring thresholds
4. Validates the result with Pydantic
5. Writes one Firestore document per drug to `/shipments/{drug_id}`

Run it once before any other service. After it succeeds, the Firestore
documents are the ground truth for the entire system.

### Folder structure

```
services/service_a/
├── seed.py              ← entry point — run this
├── requirements.txt
├── agents/
│   └── intake_agent.py  ← Claude extraction logic
├── schemas/
│   └── shipment.py      ← Pydantic validation model
└── pdfs/                ← place your PDFs here (gitignored)
    ├── pfizer-comirnaty.pdf
    ├── moderna-spikevax.pdf
    └── herceptin-trastuzumab.pdf
```

### Prerequisites

| Requirement | Details |
|---|---|
| Python 3.11+ | `python --version` |
| GCP project | Must exist and have billing enabled |
| Terraform applied | Firestore must be provisioned — see IaC/ |
| Anthropic API key | From [console.anthropic.com](https://console.anthropic.com) |
| GCP service account key | See setup steps below |

### Step-by-step setup

**Step 1 — Apply Terraform (if not done yet)**

Your teammate handles this. Firestore must exist before `seed.py` runs.

```bash
cd IaC
# Fill in your project_id in terraform.tfvars first
terraform init
terraform apply
```

**Step 2 — Create a GCP Service Account**

In GCP Console:
```
IAM & Admin → Service Accounts → Create Service Account
  Name: service-a-seed
  Role: Cloud Datastore User
  → Create key → JSON → download
```

Save the downloaded file as `gcp-credentials.json` in the repo root.
It is gitignored — never commit it.

**Step 3 — Fill in `.env`**

```env
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_APPLICATION_CREDENTIALS=./gcp-credentials.json
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
```

**Step 4 — Download the three PDFs into `services/service_a/pdfs/`**

| File | Download |
|---|---|
| `pfizer-comirnaty.pdf` | [CDC Storage Summary](https://www.cdc.gov/vaccines/covid-19/info-by-product/pfizer/downloads/storage-summary.pdf) |
| `moderna-spikevax.pdf` | [CDC Storage Summary](https://www.cdc.gov/vaccines/covid-19/info-by-product/moderna/downloads/storage-summary.pdf) |
| `herceptin-trastuzumab.pdf` | [Genentech Prescribing Info](https://www.gene.com/download/pdf/herceptin_prescribing.pdf) |

**Step 5 — Install dependencies**

```bash
# From repo root, with .venv activated
pip install -r services/service_a/requirements.txt
```

**Step 6 — Run the seed script**

```bash
cd services/service_a
python seed.py
```

Expected output:
```
INFO | Service A — Seed Script
INFO | Seeding 3 shipments into Firestore
INFO | Processing: Pfizer COMIRNATY (pfizer-001)
INFO | Calling Claude API for 'pfizer-comirnaty.pdf'
INFO | Written to Firestore: /shipments/pfizer-001
INFO |   temp range : -90.0°C to -60.0°C
INFO |   excursion  : 30 minutes max
INFO | Processing: Moderna Spikevax (moderna-001)
INFO | Written to Firestore: /shipments/moderna-001
INFO | Processing: Herceptin (herceptin-001)
INFO | Written to Firestore: /shipments/herceptin-001
INFO | Seeding complete — 3 succeeded, 0 failed
```

**Step 7 — Verify in Firestore Console**

GCP Console → Firestore → Data

You should see a `shipments` collection with three documents:
`pfizer-001`, `moderna-001`, `herceptin-001` — each containing all
the extracted threshold fields.

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `ANTHROPIC_API_KEY is not set` | Missing .env entry | Add key to .env |
| `GOOGLE_APPLICATION_CREDENTIALS is not set` | Missing .env entry | Add path to .env |
| `Credentials file not found` | Wrong path or file not downloaded | Download SA key and place at path in .env |
| `GOOGLE_CLOUD_PROJECT is not set` | Missing .env entry | Add your GCP project ID |
| `PDF not found` | PDFs not downloaded into pdfs/ | Download all three PDFs (see Step 4) |
| `Too little text extracted` | Scanned image PDF | Use digitally-created PDF only |
| `Extraction failed after 2 attempts` | Claude could not parse the document | Verify PDF is a drug label / prescribing info doc |
| `403 PERMISSION_DENIED` on Firestore | SA missing Firestore permission | Add `Cloud Datastore User` role to service account |
| `terraform apply` fails | Project ID not set | Fill in `project_id` in `IaC/terraform.tfvars` |

---

## For Teammates — Services B through E

Each service will be a Cloud Function in GCP.

**Firestore document IDs to use (hardcoded across all services):**
- `pfizer-001`
- `moderna-001`
- `herceptin-001`

**Firestore collections:**
- `/shipments/{drug_id}` — thresholds (written by Service A)
- `/monitoring_state/{drug_id}` — live excursion state (written by Service C)
- `/telemetry_state/{drug_id}` — current slider values (written by Service B)
- `/pending_approvals/{approval_id}` — HITL queue (written by Service D)
- `/audit_log/{entry_id}` — compliance trail (written by Service E)

**Pub/Sub topics (provisioned by Terraform):**
- `telemetry-stream` — Service B publishes, Service C subscribes
- `risk-detected` — Service C publishes, Service D subscribes
- `execute-actions` — Firestore trigger publishes, Service E subscribes

---

## Terraform Changes Needed (IaC/)

The current Terraform provisions infrastructure but is missing:

1. **Service account + IAM bindings** — each Cloud Function needs a dedicated
   service account with appropriate roles (Firestore read/write, Pub/Sub publish/subscribe).
   Currently these must be created manually in the console.

2. **Cloud Function deployments** — the Terraform does not yet deploy the function
   code. Add `google_cloudfunctions2_function` resources for Services B–E once
   their code is ready.

3. **`project_id` in `terraform.tfvars`** — the placeholder value
   `"project_id_goes_here"` must be replaced with the actual GCP project ID
   before `terraform apply` will work.
