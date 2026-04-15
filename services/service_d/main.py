"""
Service D — FastAPI Application
The Orchestrator Agent microservice.

Pub/Sub Push Subscription endpoint: POST /risk-detected
Cloud Run receives a push message from the 'risk-detected' Pub/Sub topic.
The message payload is base64-encoded JSON (Service C finalresponse.json format).
This handler decodes the message, runs the OrchestratorAgent, and responds 200 OK
so Pub/Sub marks the message as acknowledged.

Startup: Connects to Firestore using Application Default Credentials (ADC) on
Cloud Run, or impersonated credentials in local dev (see .env.example).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, status
from google.cloud import firestore
from pydantic import BaseModel

# Load .env for local development (on Cloud Run, env vars are injected)
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("service_d.main")

# ── Validate required environment variables ───────────────────────────────────
REQUIRED_ENV = ["ANTHROPIC_API_KEY", "GOOGLE_CLOUD_PROJECT", "FIRESTORE_DATABASE"]

def check_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        logger.error("Missing required environment variables: %s", missing)
        sys.exit(1)

check_env()

# ── Firestore client (module-level singleton) ─────────────────────────────────
# On Cloud Run: ADC picks up the service account automatically.
# Locally: set GOOGLE_APPLICATION_CREDENTIALS or use gcloud ADC.
_db: firestore.Client | None = None

def get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            database=os.environ["FIRESTORE_DATABASE"],
        )
        logger.info(
            "Firestore connected: project=%s database=%s",
            os.environ["GOOGLE_CLOUD_PROJECT"],
            os.environ["FIRESTORE_DATABASE"],
        )
    return _db


# ── Lazy import of OrchestratorAgent (avoids import-time model init) ──────────
_agent = None

def get_agent():
    global _agent
    if _agent is None:
        from agents.orchestrator_agent import OrchestratorAgent
        _agent = OrchestratorAgent(db=get_db())
        logger.info("OrchestratorAgent initialised")
    return _agent


# ── FastAPI Lifespan ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Service D — Orchestrator Agent starting up")
    # Warm up Firestore connection and agent at startup
    get_db()
    get_agent()
    logger.info("Service D ready to handle risk events")
    yield
    logger.info("Service D shutting down")


# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="AgenticTerps — Service D: Orchestrator Agent",
    description=(
        "Receives risk events from the 'risk-detected' Pub/Sub topic via push subscription. "
        "Uses a LangChain + Claude agent to generate a cascading recovery plan and writes "
        "it to the Firestore /pending-approvals collection for HITL review."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── Pub/Sub Message Schema ────────────────────────────────────────────────────
class PubSubMessage(BaseModel):
    """Schema for a Pub/Sub push message envelope."""
    data: str          # base64-encoded JSON payload
    messageId: str | None = None
    publishTime: str | None = None
    attributes: dict[str, str] | None = None


class PubSubEnvelope(BaseModel):
    """Outer envelope sent by Cloud Pub/Sub push subscription."""
    message: PubSubMessage
    subscription: str | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for Cloud Run readiness/liveness probe."""
    return {"status": "healthy", "service": "service_d_orchestrator"}


@app.post("/pubsub/risk", status_code=status.HTTP_200_OK)
async def handle_risk_detected(envelope: PubSubEnvelope) -> dict[str, Any]:
    """
    Pub/Sub push subscription endpoint.

    Cloud Pub/Sub sends a POST request with a base64-encoded JSON payload
    whenever a new message is published to the 'risk-detected' topic.

    This handler:
    1. Decodes and validates the Pub/Sub message
    2. Deserializes the risk event JSON (Service C output)
    3. Runs the OrchestratorAgent to produce a recovery plan
    4. Returns 200 OK so Pub/Sub acks the message

    If we return any non-2xx status, Pub/Sub will retry delivery (exponential backoff).
    We return 200 even on agent errors after logging, to prevent infinite retry loops
    for malformed messages.
    """
    msg = envelope.message
    message_id = msg.messageId or "unknown"

    logger.info("Received Pub/Sub message | messageId=%s", message_id)

    # ── Decode base64 payload ─────────────────────────────────────────────────
    try:
        decoded_bytes = base64.b64decode(msg.data)
        risk_event: dict[str, Any] = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as exc:
        logger.error("Failed to decode Pub/Sub message data: %s", exc)
        # Return 200 to ack and prevent infinite retry on malformed messages
        return {"status": "error", "detail": f"Failed to decode message: {exc}"}

    drug_id = risk_event.get("drug_id", "unknown")
    risk_level = risk_event.get("risk_level", "UNKNOWN")
    logger.info(
        "Processing risk event | drug_id=%s | risk_level=%s | messageId=%s",
        drug_id,
        risk_level,
        message_id,
    )

    # ── Run orchestrator agent ────────────────────────────────────────────────
    try:
        agent = get_agent()
        approval_id = agent.run(risk_event)
        logger.info(
            "Recovery plan written | approval_id=%s | drug_id=%s",
            approval_id,
            drug_id,
        )
        return {
            "status": "success",
            "approval_id": approval_id,
            "drug_id": drug_id,
            "message": f"Recovery plan saved to /pending-approvals/{approval_id}",
        }

    except Exception as exc:
        logger.exception(
            "OrchestratorAgent failed for drug_id=%s | messageId=%s | error=%s",
            drug_id,
            message_id,
            exc,
        )
        # Return 200 to ack message — a 500 would cause Pub/Sub to retry indefinitely
        # In production, push to a dead-letter topic instead
        return {
            "status": "error",
            "drug_id": drug_id,
            "detail": str(exc),
        }


@app.post("/risk-detected/raw", status_code=status.HTTP_200_OK)
async def handle_risk_detected_raw(request: Request) -> dict[str, Any]:
    """
    Alternative endpoint for direct JSON testing (bypasses Pub/Sub envelope).
    POST the raw risk_event JSON (finalresponse.json format) for local dev/testing.

    Example:
        curl -X POST http://localhost:8080/risk-detected/raw \
          -H "Content-Type: application/json" \
          -d @finalresponse.json
    """
    try:
        risk_event: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")

    drug_id = risk_event.get("drug_id", "unknown")
    logger.info("Direct test invocation | drug_id=%s", drug_id)

    try:
        agent = get_agent()
        approval_id = agent.run(risk_event)
        return {
            "status": "success",
            "approval_id": approval_id,
            "drug_id": drug_id,
            "message": f"Recovery plan saved to /pending-approvals/{approval_id}",
        }
    except Exception as exc:
        logger.exception("Agent failed in raw test endpoint: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))