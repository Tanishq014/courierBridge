from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, Numeric, Date, Time, UniqueConstraint
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
    overall_status = Column(String, index=True, default="booked") # booked/received/bagging/in_transit/hand_over_to_airline/at_destination/custom_clearance/at_lm_partner/out_for_delivery/delivered/undelivered/rto/return_damage/exception/unknown
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


def now_utc():
    return datetime.now(timezone.utc)


class JibbleSyncRun(Base):
    __tablename__ = "jibble_sync_runs"

    id = Column(Integer, primary_key=True, index=True)
    requested_month = Column(String(7), index=True, nullable=False)
    started_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(40), default="RUNNING", nullable=False, index=True)
    authentication_status = Column(String(40), default="TEMPORARILY_UNAVAILABLE", nullable=False)
    employees_received = Column(Integer, default=0, nullable=False)
    days_inserted = Column(Integer, default=0, nullable=False)
    days_updated = Column(Integer, default=0, nullable=False)
    days_unchanged = Column(Integer, default=0, nullable=False)
    calculations_recomputed = Column(Integer, default=0, nullable=False)
    warning_count = Column(Integer, default=0, nullable=False)
    error_code = Column(String(80), nullable=True)
    safe_error_message = Column(Text, nullable=True)
    raw_response_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)


class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, index=True)
    employee_code = Column(String(80), unique=True, index=True, nullable=False)
    full_name = Column(String(255), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    joining_date = Column(Date, nullable=False)
    leaving_date = Column(Date, nullable=True)
    default_timezone = Column(String(80), default="Asia/Kolkata", nullable=False)
    created_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    external_identities = relationship("ExternalEmployeeIdentity", back_populates="employee")
    attendance_policies = relationship("EmployeeAttendancePolicy", back_populates="employee")
    calculated_attendance = relationship("CalculatedDailyAttendance", back_populates="employee")


class ExternalEmployeeIdentity(Base):
    __tablename__ = "external_employee_identities"
    __table_args__ = (
        UniqueConstraint("source", "external_person_id", name="uq_external_identity_source_person"),
    )

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String(40), nullable=False, index=True)
    external_person_id = Column(String(120), nullable=False, index=True)
    external_code = Column(String(120), nullable=True)
    external_name = Column(String(255), nullable=False)
    external_timezone = Column(String(80), nullable=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    first_seen_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    employee = relationship("Employee", back_populates="external_identities")


class ImportedAttendanceDay(Base):
    __tablename__ = "imported_attendance_days"
    __table_args__ = (
        UniqueConstraint("source", "external_person_id", "attendance_date", name="uq_imported_attendance_source_person_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String(40), nullable=False, index=True)
    external_person_id = Column(String(120), nullable=False, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    attendance_date = Column(Date, nullable=False, index=True)
    first_in_utc = Column(DateTime(timezone=True), nullable=True)
    last_out_utc = Column(DateTime(timezone=True), nullable=True)
    first_in_local = Column(DateTime(timezone=True), nullable=True)
    last_out_local = Column(DateTime(timezone=True), nullable=True)
    worked_seconds = Column(Integer, default=0, nullable=False)
    tracked_seconds = Column(Integer, default=0, nullable=False)
    payroll_seconds = Column(Integer, default=0, nullable=False)
    break_seconds = Column(Integer, default=0, nullable=False)
    paid_break_seconds = Column(Integer, default=0, nullable=False)
    unpaid_break_seconds = Column(Integer, default=0, nullable=False)
    auto_deduction_seconds = Column(Integer, default=0, nullable=False)
    regular_seconds = Column(Integer, default=0, nullable=False)
    daily_overtime_seconds = Column(Integer, default=0, nullable=False)
    daily_double_overtime_seconds = Column(Integer, default=0, nullable=False)
    rest_day_overtime_seconds = Column(Integer, default=0, nullable=False)
    holiday_overtime_seconds = Column(Integer, default=0, nullable=False)
    weekly_overtime_seconds = Column(Integer, default=0, nullable=False)
    paid_time_off_seconds = Column(Integer, default=0, nullable=False)
    unpaid_time_off_seconds = Column(Integer, default=0, nullable=False)
    is_rest_day_from_source = Column(Boolean, default=False, nullable=False)
    has_archived_screenshots = Column(Boolean, default=False, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    last_sync_run_id = Column(Integer, ForeignKey("jibble_sync_runs.id"), nullable=False)
    source_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class EmployeeAttendancePolicy(Base):
    __tablename__ = "employee_attendance_policies"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    monthly_salary_paise = Column(Integer, default=0, nullable=False)
    salary_divisor_type = Column(String(40), nullable=False)
    fixed_salary_divisor = Column(Integer, nullable=True)
    shift_start_local = Column(Time, nullable=False)
    shift_end_local = Column(Time, nullable=False)
    grace_minutes = Column(Integer, default=0, nullable=False)
    full_day_required_minutes = Column(Integer, nullable=False)
    half_day_required_minutes = Column(Integer, nullable=False)
    missing_clock_out_buffer_minutes = Column(Integer, default=0, nullable=False)
    weekly_off_days_json = Column(Text, default="[]", nullable=False)
    effective_from = Column(Date, nullable=False, index=True)
    effective_to = Column(Date, nullable=True, index=True)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    employee = relationship("Employee", back_populates="attendance_policies")


class CalculatedDailyAttendance(Base):
    __tablename__ = "calculated_daily_attendance"
    __table_args__ = (
        UniqueConstraint("employee_id", "attendance_date", name="uq_calculated_attendance_employee_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    attendance_date = Column(Date, nullable=False, index=True)
    imported_attendance_day_id = Column(Integer, ForeignKey("imported_attendance_days.id"), nullable=True)
    policy_id = Column(Integer, ForeignKey("employee_attendance_policies.id"), nullable=True)
    calculated_status = Column(String(40), nullable=False, index=True)
    first_in_local = Column(DateTime(timezone=True), nullable=True)
    last_out_local = Column(DateTime(timezone=True), nullable=True)
    worked_minutes = Column(Integer, default=0, nullable=False)
    payroll_minutes = Column(Integer, default=0, nullable=False)
    late_minutes = Column(Integer, default=0, nullable=False)
    early_departure_minutes = Column(Integer, default=0, nullable=False)
    overtime_minutes = Column(Integer, default=0, nullable=False)
    suggested_deduction_units = Column(Numeric(3, 1), default=0, nullable=False)
    needs_review = Column(Boolean, default=False, nullable=False)
    review_reason = Column(Text, nullable=True)
    calculation_version = Column(String(40), nullable=False)
    calculated_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    employee = relationship("Employee", back_populates="calculated_attendance")
