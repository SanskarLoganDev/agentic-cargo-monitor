"""
SQLAlchemy ORM model for the shipments table.

Each row represents one pharmaceutical drug label PDF that has been
processed by Service A's Claude extraction agent.
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, JSON, Text
from .database import Base


class Shipment(Base):
    __tablename__ = "shipments"

    # System fields
    id = Column(Integer, primary_key=True, index=True)
    shipment_id = Column(String, unique=True, index=True, nullable=False)
    source_pdf_filename = Column(String, nullable=False)
    source_pdf_path = Column(String, nullable=False)
    extraction_model = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="active")  # active | completed | flagged

    # Drug identity
    drug_name = Column(String, nullable=False)
    manufacturer = Column(String, nullable=False)
    cargo_category = Column(String, nullable=False)
    batch_numbers = Column(JSON, default=list)
    quantity_description = Column(String, nullable=False)

    # Temperature requirements
    temp_classification = Column(String, nullable=False)
    temp_min_celsius = Column(Float, nullable=False)
    temp_max_celsius = Column(Float, nullable=False)
    max_excursion_duration_minutes = Column(Integer, nullable=False)
    do_not_freeze = Column(Boolean, default=False)
    freeze_threshold_celsius = Column(Float, nullable=True)

    # Environmental sensitivities
    max_humidity_percent = Column(Float, nullable=True)
    light_sensitive = Column(Boolean, default=False)
    shake_sensitive = Column(Boolean, default=False)
    max_shock_g = Column(Float, nullable=True)

    # Stability
    shelf_life_days = Column(Integer, nullable=True)
    stability_note = Column(Text, nullable=True)

    # Route and parties
    origin_airport_iata = Column(String, nullable=True)
    destination_airport_iata = Column(String, nullable=True)
    consignee_name = Column(String, nullable=True)
    consignee_contact = Column(String, nullable=True)
    responsible_person = Column(String, nullable=True)

    # Regulatory
    regulatory_framework = Column(String, default="EU GDP 2013/C 343/01")
    iata_handling_codes = Column(JSON, default=list)
    special_instructions = Column(Text, nullable=True)
