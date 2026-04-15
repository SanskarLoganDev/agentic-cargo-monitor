"""
LangChain tool: calculate_spoilage_time

Estimates remaining viable life, spoilage probability, and financial impact
based on thaw_window_hours, excursion_minutes, flight delay, and ETA.
Returns a structured plain-English assessment string.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from langchain_core.tools import tool


@tool
def calculate_spoilage_time(
    thaw_window_hours: float,
    excursion_minutes: int,
    flight_delay_minutes: int,
    final_destination_eta: str,
    temp_min_celsius: float,
    temp_max_celsius: float,
    current_temp_celsius: float,
    total_units: int,
    drug_name: str,
    stability_note: str = "",
) -> str:
    """
    Calculate the spoilage timeline for a pharmaceutical shipment given current
    cold-chain breach data.

    Args:
        thaw_window_hours: Hours the vaccine remains viable after confirmed thaw (from Firestore).
        excursion_minutes: Cumulative minutes already spent out of temperature range.
        flight_delay_minutes: Integer minutes of flight delay (0, 120, or 360).
        final_destination_eta: ISO 8601 UTC ETA string e.g. '2026-04-06T18:00:00Z'.
        temp_min_celsius: Minimum safe storage temperature.
        temp_max_celsius: Maximum safe storage temperature.
        current_temp_celsius: Current sensor reading.
        total_units: Total vials in the shipment.
        drug_name: Name of the drug for context.
        stability_note: Stability text from the drug label PDF.

    Returns:
        A structured plain-English spoilage assessment string.
    """
    now_utc = datetime.now(timezone.utc)

    # Parse ETA
    try:
        eta_dt = datetime.fromisoformat(final_destination_eta.replace("Z", "+00:00"))
        minutes_to_eta = max(0, (eta_dt - now_utc).total_seconds() / 60)
        eta_str = eta_dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        minutes_to_eta = 0
        eta_str = final_destination_eta

    # Thaw window in minutes
    thaw_window_minutes = thaw_window_hours * 60

    # Excursion already consumed
    excursion_consumed_pct = (excursion_minutes / thaw_window_minutes * 100) if thaw_window_minutes > 0 else 100

    # Remaining viable window
    remaining_viable_minutes = max(0, thaw_window_minutes - excursion_minutes - flight_delay_minutes)
    remaining_viable_hours = remaining_viable_minutes / 60

    # Can the shipment survive to ETA?
    survives_to_eta = remaining_viable_minutes >= minutes_to_eta

    # Estimate viable units
    if excursion_consumed_pct >= 100:
        estimated_viable_pct = 0
    elif excursion_consumed_pct >= 75:
        estimated_viable_pct = 10
    elif excursion_consumed_pct >= 50:
        estimated_viable_pct = 40
    elif excursion_consumed_pct >= 25:
        estimated_viable_pct = 70
    else:
        estimated_viable_pct = 90

    viable_units = int(total_units * estimated_viable_pct / 100)
    compromised_units = total_units - viable_units

    # Temperature context
    in_range = temp_min_celsius <= current_temp_celsius <= temp_max_celsius
    temp_context = (
        f"Currently IN safe range ({current_temp_celsius}°C vs [{temp_min_celsius}°C, {temp_max_celsius}°C])."
        if in_range
        else f"Currently OUT OF safe range ({current_temp_celsius}°C vs [{temp_min_celsius}°C, {temp_max_celsius}°C])."
    )

    assessment = (
        f"SPOILAGE ASSESSMENT for {drug_name}\n"
        f"{'='*60}\n"
        f"Thaw viability window: {thaw_window_hours:.0f} hours ({thaw_window_minutes:.0f} min)\n"
        f"Excursion already consumed: {excursion_minutes} min ({excursion_consumed_pct:.1f}% of window)\n"
        f"Flight delay contribution: {flight_delay_minutes} min\n"
        f"Remaining viable window: {remaining_viable_hours:.1f} hours ({remaining_viable_minutes:.0f} min)\n"
        f"Time until ETA: {minutes_to_eta:.0f} min ({eta_str})\n"
        f"Temperature status: {temp_context}\n"
        f"\n"
        f"CAN SHIPMENT SURVIVE TO DESTINATION? {'YES ✓' if survives_to_eta else 'NO ✗ — SPOILAGE LIKELY BEFORE ARRIVAL'}\n"
        f"\n"
        f"Estimated viable units: {estimated_viable_pct}% → {viable_units:,} of {total_units:,} vials\n"
        f"Compromised units: {compromised_units:,} vials\n"
        f"\n"
        f"Stability note: {stability_note}\n"
    )

    return assessment