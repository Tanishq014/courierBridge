from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Shipment, TrackingNumber, now_ist, IST
from datetime import datetime, timezone

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

from fastapi.responses import RedirectResponse

@router.get("/")
def home():
    return RedirectResponse(url="/shipments", status_code=303)

@router.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    active_shipments = db.query(Shipment).filter(Shipment.overall_status.notin_(["delivered", "rto", "return_damage"])).all()
    
    total_active = len(active_shipments)
    stuck_count = sum(1 for s in active_shipments if s.is_stuck)
    
    payment_pending = db.query(Shipment).filter(Shipment.balance_amount > 0).count()
    
    now = now_ist()
    start_of_today = datetime(now.year, now.month, now.day, tzinfo=IST)
    delivered_today = db.query(Shipment).filter(
        Shipment.overall_status == "delivered",
        Shipment.delivered_at >= start_of_today
    ).count()
    
    followup_due = sum(1 for s in active_shipments if s.followup_due_at and s.followup_due_at < now.replace(tzinfo=None))
    
    attention_required = [s for s in active_shipments if s.needs_attention]
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "total_active": total_active,
        "stuck_count": stuck_count,
        "payment_pending": payment_pending,
        "delivered_today": delivered_today,
        "followup_due": followup_due,
        "attention_required": attention_required
    })
