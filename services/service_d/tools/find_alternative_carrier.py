"""
LangChain tool: find_alternative_carrier

Uses the Airlabs API to fetch real-time flight data for the current flight
and route alternatives. Returns structured flight context for the agent.

Step 1: GET /flights?flight_icao=<ICAO>  → current flight status, dep/arr airports
Step 2: GET /routes?dep_iata=...&arr_iata=...&airline_iata=... → route schedule
Combined response is returned as a JSON string for Claude to reason over.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

AIRLABS_BASE = "https://airlabs.co/api/v9"


@tool
def find_alternative_carrier(
    flight_icao: str,
    current_carrier: str,
    destination_facility_name: str,
    destination_address: str,
    total_units: int,
    total_weight_kg: float,
    pallet_dimensions: str,
    drug_name: str,
) -> str:
    """
    Fetch real-time flight telemetry and route data for the current shipment flight
    using the Airlabs API. Returns a JSON string with flight status, route details,
    and carrier context for the orchestrator agent to reason over.

    Args:
        flight_icao: ICAO flight number e.g. 'AAL292'.
        current_carrier: Name of current carrier e.g. 'American Airlines Cargo'.
        destination_facility_name: Name of receiving facility.
        destination_address: Full address of destination.
        total_units: Number of vials in shipment.
        total_weight_kg: Shipment weight in kg.
        pallet_dimensions: Pallet dimensions string e.g. '48x40x60 inches'.
        drug_name: Drug name for context.

    Returns:
        JSON string with flight_data and route_data combined, or an error message.
    """
    api_key = os.getenv("AIRLABS_API_KEY", "")
    if not api_key:
        logger.warning("AIRLABS_API_KEY not set — returning placeholder flight data")
        return json.dumps({
            "error": "AIRLABS_API_KEY not configured",
            "flight_icao": flight_icao,
            "current_carrier": current_carrier,
            "note": "Flight data unavailable. Agent should proceed with available shipment metadata.",
        })

    result: dict = {
        "flight_icao": flight_icao,
        "current_carrier": current_carrier,
        "destination_facility": destination_facility_name,
        "destination_address": destination_address,
        "cargo": {
            "drug_name": drug_name,
            "total_units": total_units,
            "total_weight_kg": total_weight_kg,
            "pallet_dimensions": pallet_dimensions,
        },
        "flight_data": None,
        "route_data": None,
        "errors": [],
    }

    # ── Step 1: Fetch live flight telemetry ───────────────────────────────────
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{AIRLABS_BASE}/flights",
                params={"api_key": api_key, "flight_icao": flight_icao},
            )
            resp.raise_for_status()
            data = resp.json()
            flights = data.get("response", [])
            if flights:
                result["flight_data"] = flights[0]
                logger.info("Airlabs flight data fetched for %s", flight_icao)
            else:
                result["errors"].append(f"No live flight found for ICAO {flight_icao}")
                logger.warning("No live flight data for %s", flight_icao)
    except Exception as exc:
        result["errors"].append(f"Flight lookup failed: {exc}")
        logger.error("Airlabs /flights error: %s", exc)

    # ── Step 2: Fetch route data using dep/arr from flight_data ───────────────
    flight_info = result.get("flight_data") or {}
    dep_iata = flight_info.get("dep_iata", "")
    arr_iata = flight_info.get("arr_iata", "")
    dep_icao = flight_info.get("dep_icao", "")
    arr_icao = flight_info.get("arr_icao", "")
    airline_iata = flight_info.get("airline_iata", "")
    airline_icao = flight_info.get("airline_icao", "")

    if dep_iata and arr_iata and airline_iata:
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{AIRLABS_BASE}/routes",
                    params={
                        "api_key": api_key,
                        "dep_iata": dep_iata,
                        "dep_icao": dep_icao,
                        "arr_iata": arr_iata,
                        "arr_icao": arr_icao,
                        "airline_iata": airline_iata,
                        "airline_icao": airline_icao,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                routes = data.get("response", [])
                result["route_data"] = routes[0] if routes else None
                logger.info("Airlabs route data fetched: %s→%s", dep_iata, arr_iata)
        except Exception as exc:
            result["errors"].append(f"Route lookup failed: {exc}")
            logger.error("Airlabs /routes error: %s", exc)
    else:
        result["errors"].append(
            "Could not fetch route data — dep_iata/arr_iata/airline_iata missing from flight response"
        )

    return json.dumps(result, default=str, indent=2)