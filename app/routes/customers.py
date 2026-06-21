from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import Shipment

router = APIRouter(prefix="/customers")
templates = Jinja2Templates(directory="app/templates")

@router.get("")
def list_customers(request: Request, db: Session = Depends(get_db)):
    # Group by customer_phone for MVP
    customers_data = db.query(
        Shipment.customer_phone,
        func.max(Shipment.customer_name).label("name"),
        func.count(Shipment.id).label("total_shipments"),
        func.sum(Shipment.received_amount).label("total_paid"),
        func.sum(Shipment.billed_amount).label("total_charge")
    ).group_by(Shipment.customer_phone).all()
    
    customers = []
    for c in customers_data:
        pending = (c.total_charge or 0) - (c.total_paid or 0)
        customers.append({
            "phone": c.customer_phone,
            "name": c.name,
            "total_shipments": c.total_shipments,
            "payment_pending_total": pending
        })
        
    return templates.TemplateResponse("customers/list.html", {
        "request": request,
        "customers": customers
    })

@router.get("/{phone}")
def customer_detail(request: Request, phone: str, db: Session = Depends(get_db)):
    shipments = db.query(Shipment).filter(Shipment.customer_phone == phone).order_by(Shipment.booking_date.desc()).all()
    
    if not shipments:
        return "Customer not found"
        
    name = shipments[0].customer_name
    active = sum(1 for s in shipments if s.overall_status not in ['delivered', 'rto', 'return_damage'])
    delivered = sum(1 for s in shipments if s.overall_status == 'delivered')
    
    total_charge = sum(s.billed_amount or 0 for s in shipments)
    total_paid = sum(s.received_amount or 0 for s in shipments)
    pending_total = total_charge - total_paid
    
    return templates.TemplateResponse("customers/detail.html", {
        "request": request,
        "phone": phone,
        "name": name,
        "total": len(shipments),
        "active": active,
        "delivered": delivered,
        "pending_total": pending_total,
        "shipments": shipments
    })
