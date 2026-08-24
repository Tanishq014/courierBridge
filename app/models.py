import uuid
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, Numeric, JSON, Date, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone, timedelta
from app.database import Base

IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST)

class Shipment(Base):
    __tablename__ = "shipments"

    id = Column(Integer, primary_key=True, index=True)
    booking_date = Column(DateTime, index=True, default=now_ist)
    receive_date = Column(DateTime, nullable=True)
    second_booking_date = Column(DateTime, nullable=True)
    connection_date = Column(DateTime, nullable=True)
    is_delayed = Column(Boolean, default=False)
    
    # 1. Names and Destination (Messy fields)
    customer_name = Column(String, index=True)
    receiver_name = Column(String)
    destination_country = Column(String)
    destination_city = Column(String)
    customer_phone = Column(String, index=True)
    name_country_raw = Column(String)
    contact_or_reference_raw = Column(String)
    
    # 2. Content & Items
    parcel_description = Column(String)
    
    # 3. Weight & Rates (Messy strings + Numeric)
    dead_weight = Column(Float)
    volumetric_weight = Column(Float)
    charged_weight = Column(Float)
    weight_basis = Column(String)
    
    dead_weight_text = Column(String)
    volumetric_weight_text = Column(String)
    charged_weight_text = Column(String)
    
    customer_rate_text = Column(String)
    vendor_rate_text = Column(String)
    
    # 4. Company/Vendor
    courier_company = Column(String, index=True)
    vendor_partner = Column(String, index=True)
    
    # 5. Delivery Estimate
    promised_days_text = Column(String)
    promised_days_number = Column(Integer, nullable=True)
    
    # 6. Money / Accounting fields
    billed_amount = Column(Numeric(12, 2), default=0.0) # previously customer_charge
    received_amount = Column(Numeric(12, 2), default=0.0) # previously amount_paid
    self_cost = Column(Numeric(12, 2), default=0.0)
    other_expense = Column(Numeric(12, 2), default=0.0)
    total_cost = Column(Numeric(12, 2), default=0.0) # self_cost + other_expense
    service_value = Column(Numeric(12, 2), default=0.0) # profit: received_amount - self_cost - other_expense
    balance_amount = Column(Numeric(12, 2), default=0.0) # billed_amount - received_amount
    
    # 7. Status & Tracking 
    status_raw_text = Column(String)
    custom_duty = Column(Boolean, default=False)
    overall_status = Column(String, index=True, default="booked") # booked/connected/sent_to_courier/received/bagging/in_transit/hand_over_to_airline/at_destination/custom_clearance/at_lm_partner/out_for_delivery/delivered/undelivered/rto/return_damage/exception/unknown
    row_color = Column(String, nullable=True) # manual row highlight: green/yellow/red; blank uses status default
    requires_lm_awb = Column(Boolean, default=False)
    
    delivered_at = Column(DateTime, nullable=True)
    followup_due_at = Column(DateTime, nullable=True)
    
    # 8. Notes
    internal_notes = Column(Text, nullable=True)
    customer_notes = Column(Text, nullable=True)
    balance_notes = Column(String, nullable=True)
    raw_excel_notes = Column(Text, nullable=True)
    raw_excel_row_text = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=now_ist)
    updated_at = Column(DateTime, default=now_ist, onupdate=now_ist)
    
    # Denormalized fields for listing speed
    last_status_text = Column(String, nullable=True)
    last_status_at = Column(DateTime, nullable=True)
    last_status_location = Column(String, nullable=True)
    last_normalized_status = Column(String, nullable=True)
    
    tracking_numbers = relationship("TrackingNumber", back_populates="shipment")
    tracking_events = relationship("TrackingEvent", back_populates="shipment")

    @property
    def is_stuck(self):
        if self.overall_status in ["delivered", "rto", "return_damage"]:
            return False
        if not self.last_status_at:
            return False
        
        delta = now_ist() - self.last_status_at.replace(tzinfo=IST) if self.last_status_at.tzinfo is None else now_ist() - self.last_status_at
        return delta.total_seconds() > (48 * 3600)

    @property
    def needs_attention(self):
        # Check raw status text for bad words
        bad_words = ["custom", "delay", "hold", "exception", "rto", "return", "damage"]
        status_lower = (self.status_raw_text or "").lower()
        if any(bw in status_lower for bw in bad_words):
            return True
            
        if self.is_delayed:
            return True
            
        if self.is_stuck:
            return True
            
        # LM AWB missing
        if self.requires_lm_awb and self.overall_status not in ["delivered", "rto", "return_damage"]:
            has_lm = any(tn.tracking_type == "lm_awb" for tn in self.tracking_numbers)
            if not has_lm:
                return True
                
        # balance > 0
        if self.balance_amount and float(self.balance_amount) > 0:
            return True
            
        # status_raw_text is blank
        if not self.status_raw_text or self.status_raw_text.strip() == "":
            return True
            
        # tracking number is blank
        if not self.tracking_numbers:
            return True
            
        return False

class TrackingNumber(Base):
    __tablename__ = "tracking_numbers"

    id = Column(Integer, primary_key=True, index=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"))
    tracking_type = Column(String) # main_awb, lm_awb, etc.
    courier_name = Column(String)
    tracking_number = Column(String, index=True)
    is_primary = Column(Boolean, default=False)
    added_at = Column(DateTime, default=now_ist)
    
    shipment = relationship("Shipment", back_populates="tracking_numbers")
    events = relationship("TrackingEvent", back_populates="tracking_number")


class TrackingEvent(Base):
    __tablename__ = "tracking_events"

    id = Column(Integer, primary_key=True, index=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"))
    tracking_number_id = Column(Integer, ForeignKey("tracking_numbers.id"), nullable=True)
    
    event_time = Column(DateTime)
    location = Column(String)
    status_text = Column(String)
    normalized_status = Column(String)
    notes = Column(String)
    source = Column(String, default="manual")
    created_at = Column(DateTime, default=now_ist)
    
    shipment = relationship("Shipment", back_populates="tracking_events")
    tracking_number = relationship("TrackingNumber", back_populates="events")


class TrackingTemplate(Base):
    __tablename__ = "tracking_templates"

    id = Column(Integer, primary_key=True, index=True)
    courier_name = Column(String, unique=True, index=True)
    template_url = Column(String)


class TrackingCheck(Base):
    __tablename__ = "tracking_checks"

    id = Column(Integer, primary_key=True, index=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), index=True)
    tracking_number_id = Column(Integer, ForeignKey("tracking_numbers.id"), nullable=True)
    tracking_type = Column(String)
    courier_name = Column(String)
    tracking_number = Column(String, index=True)
    fetch_status = Column(String, default="pending")  # success/failed/skipped
    error_message = Column(Text, nullable=True)
    latest_status_text = Column(String, nullable=True)
    latest_event_at = Column(DateTime, nullable=True)
    formatted_events_json = Column(Text, nullable=True)
    raw_response = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_ist, index=True)

    shipment = relationship("Shipment")
    tracking_number_ref = relationship("TrackingNumber")


class ShipmentAIStatus(Base):
    __tablename__ = "shipment_ai_statuses"

    id = Column(Integer, primary_key=True, index=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), index=True)
    tracking_check_id = Column(Integer, ForeignKey("tracking_checks.id"), nullable=True)
    provider = Column(String, default="rules")
    model_name = Column(String, nullable=True)
    label = Column(String, default="Unknown")
    severity = Column(String, default="gray")  # green/yellow/red/gray
    summary = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    suggested_status = Column(String, nullable=True)
    suggested_status_note = Column(Text, nullable=True)
    found_lm_awb = Column(String, nullable=True)
    found_lm_courier = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)
    formatted_events_json = Column(Text, nullable=True)
    raw_ai_json = Column(Text, nullable=True)
    applied_at = Column(DateTime, nullable=True)
    ignored_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now_ist, index=True)

    shipment = relationship("Shipment")
    tracking_check = relationship("TrackingCheck")

def generate_uuid():
    return str(uuid.uuid4())

class Vendor(Base):
    __tablename__ = "vendors"
    
    id = Column(String(36), primary_key=True, default=generate_uuid, index=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=now_ist)
    
    documents = relationship("TariffDocument", back_populates="vendor")

class TariffDocument(Base):
    __tablename__ = "tariff_documents"
    
    id = Column(String(36), primary_key=True, default=generate_uuid, index=True)
    vendor_id = Column(String(36), ForeignKey("vendors.id"), nullable=False)
    file_url = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)
    selected_sheets = Column(String, nullable=True)
    status = Column(String, default="DRAFT") # DRAFT, APPROVED, REJECTED
    uploaded_at = Column(DateTime, default=now_ist)
    
    raw_extraction_json = Column(JSON, nullable=True)
    prompt_version = Column(String, nullable=True)
    audit_log = Column(JSON, nullable=True)
    
    vendor = relationship("Vendor", back_populates="documents")
    sections = relationship("TariffSection", back_populates="document", cascade="all, delete-orphan")

class TariffSection(Base):
    __tablename__ = "tariff_sections"
    
    id = Column(String(36), primary_key=True, default=generate_uuid, index=True)
    document_id = Column(String(36), ForeignKey("tariff_documents.id"), nullable=False)
    carrier = Column(String, nullable=True)
    service = Column(String, nullable=True)
    valid_from = Column(Date, nullable=True)
    valid_to = Column(Date, nullable=True)
    currency = Column(String, default="INR")
    zone_resolver_id = Column(String(36), ForeignKey("reusable_zone_resolvers.id"), nullable=True)
    
    document = relationship("TariffDocument", back_populates="sections")
    rate_rows = relationship("TariffRateRow", back_populates="section", cascade="all, delete-orphan")
    notes = relationship("TariffNote", back_populates="section", cascade="all, delete-orphan")
    zone_resolver = relationship("ReusableZoneResolver")

class ReusableZoneResolver(Base):
    """
    Knowledge base for massive, deterministic Zone Mappings (extracted via Hybrid Python parser).
    Stores mappings as an array of normalized objects: {"key_type": "postcode|suburb|country", "key": "3000 or Australia", "zone": "Metro"}
    """
    __tablename__ = "reusable_zone_resolvers"
    
    id = Column(String(36), primary_key=True, default=generate_uuid, index=True)
    name = Column(String, nullable=False) # e.g. "Australia Postcodes (Extracted from Sheet 2)"
    carrier = Column(String, index=True, nullable=True)
    service = Column(String, index=True, nullable=True)
    source_document_id = Column(String(36), ForeignKey("tariff_documents.id"), nullable=True)
    assigned_country = Column(String, nullable=True)
    mapping_data = Column(JSON, nullable=False) # Array of normalized {key_type, key, zone}
    
    created_at = Column(DateTime, default=now_ist)
    updated_at = Column(DateTime, default=now_ist, onupdate=now_ist)

class TariffRateRow(Base):
    """
    Stores a single rate row. Supports both:
    - Point rates: weight_min=5.0, weight_max=NULL (exact weight match, used for FLAT rates)
    - Bracket rates: weight_min=30.1, weight_max=50.0 (range lookup, used for PER_KG brackets)
    - Open-ended: weight_min=30.0, weight_max=99999.0 (for '30+' style ranges)
    """
    __tablename__ = "tariff_rate_rows"
    
    id = Column(String(36), primary_key=True, default=generate_uuid, index=True)
    section_id = Column(String(36), ForeignKey("tariff_sections.id"), nullable=False)
    weight = Column(Numeric(10, 3), nullable=True)   # weight_min — lower bound of bracket (or exact weight)
    weight_max = Column(Numeric(10, 3), nullable=True) # weight_max — upper bound; NULL means point rate
    zone = Column(String, nullable=True)
    price = Column(Numeric(12, 2), nullable=True)
    price_type = Column(String, default="FLAT") # FLAT, PER_KG, INCREMENTAL
    import_status = Column(String, default="AUTO_APPROVED")
    source_ref = Column(JSON, nullable=True) # {"sheet": "Rates", "row": 15}
    
    __table_args__ = (
        UniqueConstraint('section_id', 'weight', 'weight_max', 'zone', name='uix_section_weight_zone'),
    )
    
    section = relationship("TariffSection", back_populates="rate_rows")

class TariffNote(Base):
    __tablename__ = "tariff_notes"
    
    id = Column(String(36), primary_key=True, default=generate_uuid, index=True)
    section_id = Column(String(36), ForeignKey("tariff_sections.id"), nullable=False)
    zone = Column(String, nullable=True) # if null, applies to whole section
    text = Column(Text, nullable=False)
    category = Column(String, default="INFO") # CRITICAL, BILLING, INFO
    
    section = relationship("TariffSection", back_populates="notes")

class ZoneMapping(Base):
    """
    Knowledge base for abstract zone mappings (e.g. Aramex 'METRO' -> ['UAE', 'Qatar']).
    Keyed uniquely by (carrier, service, zone_name) conceptually.
    """
    __tablename__ = "zone_mappings"
    
    id = Column(Integer, primary_key=True, index=True)
    carrier = Column(String, index=True, nullable=False)
    service = Column(String, index=True, nullable=False)
    zone_name = Column(String, index=True, nullable=False)
    
    # Store mapped destinations as JSON array of strings
    mapped_destinations = Column(JSON, nullable=False)
    
    transit_days = Column(String, nullable=True)
    
    created_at = Column(DateTime, default=now_ist)
    updated_at = Column(DateTime, default=now_ist, onupdate=now_ist)
