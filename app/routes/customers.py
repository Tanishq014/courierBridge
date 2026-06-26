from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from urllib.parse import urlencode
from app.database import get_db
from app.models import Shipment

router = APIRouter(prefix="/customers")
templates = Jinja2Templates(directory="app/templates")


def normalize_customer_name(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def customer_history_response(request: Request, db: Session, phone: str = "", name: str = ""):
    phone = (phone or "").strip()
    name = normalize_customer_name(name)
    query = db.query(Shipment)
    if phone:
        shipments = query.filter(Shipment.customer_phone == phone).order_by(Shipment.booking_date.desc()).all()
    elif name:
        shipments = query.filter(Shipment.customer_name.ilike(name)).order_by(Shipment.booking_date.desc()).all()
    else:
        shipments = []

    if not shipments:
        return templates.TemplateResponse("customers/detail.html", {
            "request": request,
            "phone": phone,
            "name": name or "Customer not found",
            "total": 0,
            "active": 0,
            "delivered": 0,
            "pending_total": 0,
            "shipments": []
        }, status_code=404)

    display_name = shipments[0].customer_name or name or "Unnamed customer"
    display_phone = phone or shipments[0].customer_phone or ""
    active = sum(1 for s in shipments if s.overall_status not in ['delivered', 'rto', 'return_damage'])
    delivered = sum(1 for s in shipments if s.overall_status == 'delivered')
    total_charge = sum(s.billed_amount or 0 for s in shipments)
    total_paid = sum(s.received_amount or 0 for s in shipments)

    return templates.TemplateResponse("customers/detail.html", {
        "request": request,
        "phone": display_phone,
        "name": display_name,
        "total": len(shipments),
        "active": active,
        "delivered": delivered,
        "pending_total": total_charge - total_paid,
        "shipments": shipments
    })


@router.get("")
def list_customers(request: Request, db: Session = Depends(get_db)):
    groups = {}
    shipments = db.query(Shipment).order_by(Shipment.booking_date.desc()).all()
    for shipment in shipments:
        phone = (shipment.customer_phone or "").strip()
        name = normalize_customer_name(shipment.customer_name) or "Unnamed customer"
        key_type = "phone" if phone else "name"
        key_value = phone or name.lower()
        key = (key_type, key_value)
        if key not in groups:
            params = {key_type: phone or name}
            groups[key] = {
                "phone": phone,
                "name": name,
                "total_shipments": 0,
                "total_paid": 0,
                "total_charge": 0,
                "history_url": f"/customers/history?{urlencode(params)}",
            }
        group = groups[key]
        group["total_shipments"] += 1
        group["total_paid"] += shipment.received_amount or 0
        group["total_charge"] += shipment.billed_amount or 0

    customers = []
    for group in groups.values():
        pending = group["total_charge"] - group["total_paid"]
        customers.append({
            "phone": group["phone"],
            "name": group["name"],
            "total_shipments": group["total_shipments"],
            "payment_pending_total": pending,
            "history_url": group["history_url"],
        })
    customers.sort(key=lambda c: str(c["name"]).lower())

    return templates.TemplateResponse("customers/list.html", {
        "request": request,
        "customers": customers
    })


@router.get("/history")
def customer_history(request: Request, phone: str = "", name: str = "", db: Session = Depends(get_db)):
    return customer_history_response(request, db, phone=phone, name=name)


@router.get("/{phone}")
def customer_detail(request: Request, phone: str, db: Session = Depends(get_db)):
    return customer_history_response(request, db, phone=phone)
