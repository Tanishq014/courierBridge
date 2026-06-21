from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, Numeric
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
    
    # 1. Names and Destination (Messy fields)
    customer_name = Column(String, index=True)
    receiver_name = Column(String)
    destination_country = Column(String)
    destination_city = Column(String)
    customer_phone = Column(String, index=True)
    name_country_raw = Column(String)
    contact_or_reference_raw = Column(String)
    
    # 2. Content & Items
    item_category = Column(String)
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
    overall_status = Column(String, index=True, default="booked") # booked/received/in_transit/customs/out_for_delivery/delivered/rto/return_damage/exception/unknown
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
    source = Column(String, default="manual")
    created_at = Column(DateTime, default=now_ist)
    
    shipment = relationship("Shipment", back_populates="tracking_events")
    tracking_number = relationship("TrackingNumber", back_populates="events")


class TrackingTemplate(Base):
    __tablename__ = "tracking_templates"

    id = Column(Integer, primary_key=True, index=True)
    courier_name = Column(String, unique=True, index=True)
    template_url = Column(String)
