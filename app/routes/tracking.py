from fastapi import APIRouter, Request, Depends, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import TrackingNumber, TrackingEvent, TrackingTemplate, Shipment, now_ist
from datetime import datetime

router = APIRouter(prefix="/tracking")
templates = Jinja2Templates(directory="app/templates")

@router.get("/number/new")
def new_tracking_number_form(request: Request, shipment_id: int):
    return templates.TemplateResponse("tracking/new_number.html", {"request": request, "shipment_id": shipment_id})

@router.post("/number/new")
def create_tracking_number(
    request: Request,
    shipment_id: int = Form(...),
    tracking_type: str = Form(...),
    courier_name: str = Form(...),
    tracking_number: str = Form(...),
    is_primary: bool = Form(False),
    db: Session = Depends(get_db)
):
    from app.routes.shipments import upsert_tracking
    
    if tracking_type == "main_awb":
        is_primary = True
    elif tracking_type == "lm_awb":
        is_primary = False
        
    if tracking_type in ["main_awb", "lm_awb"]:
        upsert_tracking(db, shipment_id, tracking_type, tracking_number, courier_name, is_primary)
        db.commit()
    else:
        tn = TrackingNumber(
            shipment_id=shipment_id,
            tracking_type=tracking_type,
            courier_name=courier_name,
            tracking_number=tracking_number,
            is_primary=is_primary
        )
        if is_primary:
            db.query(TrackingNumber).filter(TrackingNumber.shipment_id == shipment_id).update({"is_primary": False})
        db.add(tn)
        db.commit()
    return RedirectResponse(url=f"/shipments/{shipment_id}", status_code=303)

@router.get("/event/new")
def new_tracking_event_form(request: Request, shipment_id: int):
    return templates.TemplateResponse("tracking/new_event.html", {"request": request, "shipment_id": shipment_id})

@router.post("/event/new")
def create_tracking_event(
    request: Request,
    shipment_id: int = Form(...),
    status_text: str = Form(...),
    location: str = Form(""),
    normalized_status: str = Form("in_transit"),
    db: Session = Depends(get_db)
):
    ev = TrackingEvent(
        shipment_id=shipment_id,
        event_time=now_ist(),
        status_text=status_text,
        location=location,
        normalized_status=normalized_status,
        source="manual"
    )
    db.add(ev)
    
    # Update denormalized fields on shipment
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if shipment:
        shipment.status_raw_text = status_text
        shipment.last_status_text = status_text
        shipment.last_status_at = ev.event_time
        shipment.last_status_location = location
        shipment.last_normalized_status = normalized_status
        
        # Optionally update overall_status based on event
        if normalized_status in ["delivered", "exception", "customs", "out_for_delivery"]:
            shipment.overall_status = normalized_status
            if normalized_status == "delivered" and not shipment.delivered_at:
                shipment.delivered_at = ev.event_time
                
    db.commit()
    return RedirectResponse(url=f"/shipments/{shipment_id}", status_code=303)

@router.get("/number/{tn_id}/open")
def open_tracking_url(tn_id: int, db: Session = Depends(get_db)):
    tn = db.query(TrackingNumber).filter(TrackingNumber.id == tn_id).first()
    if not tn:
        return RedirectResponse(url="/shipments", status_code=303)
        
    template = db.query(TrackingTemplate).filter(TrackingTemplate.courier_name == tn.courier_name).first()
    
    if template and "{awb}" in template.template_url:
        url = template.template_url.replace("{awb}", tn.tracking_number)
        return RedirectResponse(url=url, status_code=303)
        
    # If no template, show fallback page
    return f"No template found for {tn.courier_name}. Tracking Number: {tn.tracking_number}"
