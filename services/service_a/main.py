"""
Service A — Intake Agent
FastAPI application entry point.

Responsibilities:
- Receives pharmaceutical drug label PDFs from the frontend
- Extracts structured shipment data using Claude AI
- Persists records to SQLite
- Publishes shipment.created events to the internal event bus

Run with: uvicorn main:app --reload --port 8001
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load .env before any module that reads env vars
load_dotenv()

from db.database import engine, Base
from agents.intake_agent import IntakeAgent
from routers import upload

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — startup and shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once at startup, tears down at shutdown.
    - Creates SQLite tables if they don't exist
    - Initialises the IntakeAgent (validates API key is present)
    """
    logger.info("Starting Service A — Intake Agent")

    # Create DB tables
    Base.metadata.create_all(bind=engine)
    logger.info("SQLite tables ready")

    # Initialise agent and store on app.state so routers can access it
    app.state.intake_agent = IntakeAgent()
    logger.info("IntakeAgent ready")

    yield  # Application runs here

    logger.info("Shutting down Service A")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Agentic Cargo Monitor — Service A: Intake Agent",
    description=(
        "Accepts pharmaceutical drug label PDFs and uses Claude AI to extract "
        "structured shipment monitoring parameters (temperature range, excursion limits, "
        "environmental sensitivities). Part of the Agentic Cargo Monitor system."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — allow the React frontend (localhost:5173 is Vite's default dev port)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(upload.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"], summary="Health check")
async def health():
    """Returns service status. Used by the frontend to verify the service is up."""
    return {
        "service": "service_a",
        "status": "healthy",
        "description": "Intake Agent — PDF extraction pipeline",
    }


@app.get("/", tags=["System"], include_in_schema=False)
async def root():
    return {"message": "Service A is running. Visit /docs for API documentation."}
