from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Shipment, TrackingNumber, now_ist, IST
from datetime import datetime, timezone, timedelta

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

from fastapi.responses import RedirectResponse

@router.get("/")
def home():
    return RedirectResponse(url="/shipments", status_code=303)

@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    terminal_statuses = {"delivered", "rto", "return_damage"}
    active_shipments = db.query(Shipment).filter(Shipment.overall_status.notin_(list(terminal_statuses))).all()
    all_shipments = db.query(Shipment).order_by(Shipment.booking_date.desc()).all()

    total_active = len(active_shipments)
    stuck_count = sum(1 for s in active_shipments if s.is_stuck)

    payment_pending = db.query(Shipment).filter(Shipment.balance_amount > 0).count()

    now = now_ist()
    today = now.date()
    start_of_today = datetime(now.year, now.month, now.day, tzinfo=IST)
    delivered_today = db.query(Shipment).filter(
        Shipment.overall_status == "delivered",
        Shipment.delivered_at >= start_of_today
    ).count()

    followup_due = sum(1 for s in active_shipments if s.followup_due_at and s.followup_due_at < now.replace(tzinfo=None))

    def is_active(shipment):
        return shipment.overall_status not in terminal_statuses

    def booked_today(shipment):
        return shipment.booking_date and shipment.booking_date.date() == today

    def missing_tracking(shipment):
        return is_active(shipment) and not any((tn.tracking_number or "").strip() for tn in shipment.tracking_numbers)

    def missing_lm(shipment):
        return is_active(shipment) and shipment.requires_lm_awb and not any(tn.tracking_type == "lm_awb" and (tn.tracking_number or "").strip() for tn in shipment.tracking_numbers)

    def overdue(shipment):
        if not shipment.booking_date or not shipment.promised_days_number or not is_active(shipment):
            return False
        return shipment.booking_date.date() + timedelta(days=shipment.promised_days_number) < today

    def due_followup(shipment):
        return shipment.followup_due_at and shipment.followup_due_at < now.replace(tzinfo=None)

    today_bookings = [s for s in all_shipments if booked_today(s)]
    attention_required = [s for s in active_shipments if s.needs_attention]
    missing_tracking_shipments = [s for s in active_shipments if missing_tracking(s)]
    missing_lm_shipments = [s for s in active_shipments if missing_lm(s)]
    payment_pending_shipments = [s for s in all_shipments if s.balance_amount and float(s.balance_amount) > 0]
    custom_duty_shipments = [s for s in active_shipments if s.custom_duty or s.overall_status in ["customs", "custom_clearance"]]
    stale_shipments = [s for s in active_shipments if s.is_stuck]
    overdue_shipments = [s for s in active_shipments if overdue(s)]
    followup_shipments = [s for s in active_shipments if due_followup(s)]

    pending_sections = [
        {
            "title": "Today's bookings",
            "subtitle": "New work entered today.",
            "shipments": today_bookings,
            "href": "/shipments?date=today",
            "tone": "info",
        },
        {
            "title": "Missing main tracking",
            "subtitle": "Booked shipments where the AWB is still blank.",
            "shipments": missing_tracking_shipments,
            "href": "/shipments?quick=missing_tracking",
            "tone": "warning",
        },
        {
            "title": "Missing LM AWB",
            "subtitle": "Last-mile required but no last-mile number added.",
            "shipments": missing_lm_shipments,
            "href": "/shipments?quick=missing_lm",
            "tone": "warning",
        },
        {
            "title": "Payment pending",
            "subtitle": "Balance still due from customer.",
            "shipments": payment_pending_shipments,
            "href": "/shipments?quick=pending_balance",
            "tone": "danger",
        },
        {
            "title": "Custom duty / clearance",
            "subtitle": "Duty-marked and customs status shipments.",
            "shipments": custom_duty_shipments,
            "href": "/shipments?quick=custom_duty",
            "tone": "warning",
        },
        {
            "title": "Stale movement",
            "subtitle": "No status movement for more than 48 hours.",
            "shipments": stale_shipments,
            "href": "/shipments?quick=stale",
            "tone": "danger",
        },
        {
            "title": "Promise overdue",
            "subtitle": "Promised delivery days have passed.",
            "shipments": overdue_shipments,
            "href": "/shipments?quick=overdue",
            "tone": "danger",
        },
        {
            "title": "Follow-up due",
            "subtitle": "Manual follow-up date has passed.",
            "shipments": followup_shipments,
            "href": "/shipments?quick=attention",
            "tone": "info",
        },
    ]

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "total_active": total_active,
        "stuck_count": stuck_count,
        "payment_pending": payment_pending,
        "delivered_today": delivered_today,
        "followup_due": followup_due,
        "attention_required": attention_required,
        "pending_sections": pending_sections,
    })
