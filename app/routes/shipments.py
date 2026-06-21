from fastapi import APIRouter, Request, Depends, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import get_db
from app.models import Shipment, TrackingNumber, now_ist
from decimal import Decimal
from datetime import datetime

router = APIRouter(prefix="/shipments")
templates = Jinja2Templates(directory="app/templates")

def parse_decimal(val: str) -> Decimal:
    try:
        return Decimal(val.strip()) if val and val.strip() else Decimal("0.0")
    except Exception:
        return Decimal("0.0")

def parse_float(val: str) -> float:
    try:
        return float(val) if val and val.strip() else 0.0
    except ValueError:
        return 0.0

def parse_int(val: str) -> int | None:
    try:
        return int(val) if val and val.strip() else None
    except ValueError:
        return None

def upsert_tracking(db: Session, shipment_id: int, t_type: str, number: str, courier: str, is_primary: bool):
    number = number.strip() if number else ""
    if not number:
        return
        
    if t_type == "main_awb":
        is_primary = True
    elif t_type == "lm_awb":
        is_primary = False
        
    if is_primary:
        db.query(TrackingNumber).filter(TrackingNumber.shipment_id == shipment_id).update({"is_primary": False})
        
    tn = db.query(TrackingNumber).filter(
        TrackingNumber.shipment_id == shipment_id,
        TrackingNumber.tracking_type == t_type
    ).first()
    
    if tn:
        tn.tracking_number = number
        if courier:
            tn.courier_name = courier.strip()
        tn.is_primary = is_primary
    else:
        new_tn = TrackingNumber(
            shipment_id=shipment_id,
            tracking_type=t_type,
            tracking_number=number,
            courier_name=courier.strip() if courier else "",
            is_primary=is_primary
        )
        db.add(new_tn)

@router.get("")
def list_shipments(
    request: Request, 
    db: Session = Depends(get_db),
    q: str = "",
    status: str = ""
):
    query = db.query(Shipment).outerjoin(TrackingNumber)
    
    if q:
        query = query.filter(
            or_(
                Shipment.customer_name.ilike(f"%{q}%"),
                Shipment.customer_phone.ilike(f"%{q}%"),
                Shipment.destination_country.ilike(f"%{q}%"),
                Shipment.name_country_raw.ilike(f"%{q}%"),
                Shipment.contact_or_reference_raw.ilike(f"%{q}%"),
                TrackingNumber.tracking_number.ilike(f"%{q}%"),
                Shipment.courier_company.ilike(f"%{q}%"),
                Shipment.vendor_partner.ilike(f"%{q}%"),
                Shipment.status_raw_text.ilike(f"%{q}%"),
                Shipment.item_category.ilike(f"%{q}%")
            )
        )
    if status:
        query = query.filter(Shipment.overall_status == status)
        
    shipments = query.order_by(Shipment.booking_date.desc()).distinct().all()
    
    return templates.TemplateResponse("shipments/list.html", {
        "request": request,
        "shipments": shipments,
        "q": q,
        "status": status
    })

@router.get("/new")
def new_shipment_form(request: Request):
    today = now_ist().strftime("%Y-%m-%d")
    return templates.TemplateResponse("shipments/new.html", {"request": request, "today": today})

@router.post("/new")
def create_shipment(
    request: Request,
    db: Session = Depends(get_db),
    customer_name: str = Form(""),
    receiver_name: str = Form(""),
    destination_country: str = Form(""),
    destination_city: str = Form(""),
    customer_phone: str = Form(""),
    name_country_raw: str = Form(""),
    contact_or_reference_raw: str = Form(""),
    
    item_category: str = Form(""),
    parcel_description: str = Form(""),
    
    dead_weight: str = Form("0.0"),
    volumetric_weight: str = Form("0.0"),
    charged_weight: str = Form("0.0"),
    weight_basis: str = Form("actual"),
    dead_weight_text: str = Form(""),
    volumetric_weight_text: str = Form(""),
    charged_weight_text: str = Form(""),
    
    customer_rate_text: str = Form(""),
    vendor_rate_text: str = Form(""),
    courier_company: str = Form(""),
    vendor_partner: str = Form(""),
    
    promised_days_text: str = Form(""),
    promised_days_number: str = Form(""),
    
    billed_amount: str = Form("0.0"),
    received_amount: str = Form("0.0"),
    self_cost: str = Form("0.0"),
    other_expense: str = Form("0.0"),
    
    status_raw_text: str = Form(""),
    overall_status: str = Form("booked"),
    requires_lm_awb: bool = Form(False),
    
    booking_date: str = Form(""),
    main_tracking_number: str = Form(""),
    main_tracking_courier: str = Form(""),
    lm_awb_number: str = Form(""),
    lm_awb_courier: str = Form(""),
    
    internal_notes: str = Form(""),
    customer_notes: str = Form(""),
    balance_notes: str = Form(""),
    raw_excel_notes: str = Form(""),
    raw_excel_row_text: str = Form("")
):
    parsed_billed = parse_decimal(billed_amount)
    parsed_received = parse_decimal(received_amount)
    parsed_self_cost = parse_decimal(self_cost)
    parsed_other_exp = parse_decimal(other_expense)
    
    total_cost = parsed_self_cost + parsed_other_exp
    service_value = parsed_received - total_cost
    balance_amount = parsed_billed - parsed_received
    
    parsed_booking_date = now_ist()
    if booking_date and booking_date.strip():
        try:
            parsed_booking_date = datetime.strptime(booking_date.strip(), "%Y-%m-%d")
        except ValueError:
            pass

    shipment = Shipment(
        booking_date=parsed_booking_date,
        customer_name=customer_name,
        receiver_name=receiver_name,
        destination_country=destination_country,
        destination_city=destination_city,
        customer_phone=customer_phone,
        name_country_raw=name_country_raw,
        contact_or_reference_raw=contact_or_reference_raw,
        item_category=item_category,
        parcel_description=parcel_description,
        dead_weight=parse_float(dead_weight),
        volumetric_weight=parse_float(volumetric_weight),
        charged_weight=parse_float(charged_weight),
        weight_basis=weight_basis,
        dead_weight_text=dead_weight_text,
        volumetric_weight_text=volumetric_weight_text,
        charged_weight_text=charged_weight_text,
        customer_rate_text=customer_rate_text,
        vendor_rate_text=vendor_rate_text,
        courier_company=courier_company,
        vendor_partner=vendor_partner,
        promised_days_text=promised_days_text,
        promised_days_number=parse_int(promised_days_number),
        billed_amount=parsed_billed,
        received_amount=parsed_received,
        self_cost=parsed_self_cost,
        other_expense=parsed_other_exp,
        total_cost=total_cost,
        service_value=service_value,
        balance_amount=balance_amount,
        status_raw_text=status_raw_text,
        overall_status=overall_status,
        requires_lm_awb=requires_lm_awb,
        internal_notes=internal_notes,
        customer_notes=customer_notes,
        balance_notes=balance_notes,
        raw_excel_notes=raw_excel_notes,
        raw_excel_row_text=raw_excel_row_text,
        last_status_text=status_raw_text
    )
    
    if status_raw_text and status_raw_text.strip():
        shipment.last_status_text = status_raw_text
        shipment.last_status_at = parsed_booking_date
    
    if overall_status == "delivered":
        shipment.delivered_at = parsed_booking_date
        
    db.add(shipment)
    db.commit()
    db.refresh(shipment)
    
    # Tracking numbers
    upsert_tracking(db, shipment.id, "main_awb", main_tracking_number, main_tracking_courier, True)
    upsert_tracking(db, shipment.id, "lm_awb", lm_awb_number, lm_awb_courier, False)
    db.commit()
    
    return RedirectResponse(url=f"/shipments/{shipment.id}", status_code=303)

@router.get("/{shipment_id}")
def shipment_detail(request: Request, shipment_id: int, db: Session = Depends(get_db)):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        return RedirectResponse(url="/shipments", status_code=303)
        
    return templates.TemplateResponse("shipments/detail.html", {
        "request": request,
        "shipment": shipment
    })

@router.get("/{shipment_id}/edit")
def edit_shipment_form(request: Request, shipment_id: int, db: Session = Depends(get_db)):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        return RedirectResponse(url="/shipments", status_code=303)
        
    main_awb = next((tn for tn in shipment.tracking_numbers if tn.tracking_type == "main_awb"), None)
    lm_awb = next((tn for tn in shipment.tracking_numbers if tn.tracking_type == "lm_awb"), None)
        
    return templates.TemplateResponse("shipments/edit.html", {
        "request": request,
        "shipment": shipment,
        "main_awb": main_awb,
        "lm_awb": lm_awb
    })

@router.post("/{shipment_id}/edit")
def update_shipment(
    request: Request,
    shipment_id: int,
    db: Session = Depends(get_db),
    customer_name: str = Form(""),
    receiver_name: str = Form(""),
    destination_country: str = Form(""),
    destination_city: str = Form(""),
    customer_phone: str = Form(""),
    name_country_raw: str = Form(""),
    contact_or_reference_raw: str = Form(""),
    
    item_category: str = Form(""),
    parcel_description: str = Form(""),
    
    dead_weight: str = Form("0.0"),
    volumetric_weight: str = Form("0.0"),
    charged_weight: str = Form("0.0"),
    weight_basis: str = Form("actual"),
    dead_weight_text: str = Form(""),
    volumetric_weight_text: str = Form(""),
    charged_weight_text: str = Form(""),
    
    customer_rate_text: str = Form(""),
    vendor_rate_text: str = Form(""),
    courier_company: str = Form(""),
    vendor_partner: str = Form(""),
    
    promised_days_text: str = Form(""),
    promised_days_number: str = Form(""),
    
    billed_amount: str = Form("0.0"),
    received_amount: str = Form("0.0"),
    self_cost: str = Form("0.0"),
    other_expense: str = Form("0.0"),
    
    status_raw_text: str = Form(""),
    overall_status: str = Form("booked"),
    requires_lm_awb: bool = Form(False),
    
    booking_date: str = Form(""),
    main_tracking_number: str = Form(""),
    main_tracking_courier: str = Form(""),
    lm_awb_number: str = Form(""),
    lm_awb_courier: str = Form(""),
    
    internal_notes: str = Form(""),
    customer_notes: str = Form(""),
    balance_notes: str = Form(""),
    raw_excel_notes: str = Form(""),
    raw_excel_row_text: str = Form("")
):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        return RedirectResponse(url="/shipments", status_code=303)
        
    parsed_billed = parse_decimal(billed_amount)
    parsed_received = parse_decimal(received_amount)
    parsed_self_cost = parse_decimal(self_cost)
    parsed_other_exp = parse_decimal(other_expense)
        
    total_cost = parsed_self_cost + parsed_other_exp
    service_value = parsed_received - total_cost
    balance_amount = parsed_billed - parsed_received
    
    if booking_date and booking_date.strip():
        try:
            shipment.booking_date = datetime.strptime(booking_date.strip(), "%Y-%m-%d")
        except ValueError:
            pass
    
    shipment.customer_name = customer_name
    shipment.receiver_name = receiver_name
    shipment.destination_country = destination_country
    shipment.destination_city = destination_city
    shipment.customer_phone = customer_phone
    shipment.name_country_raw = name_country_raw
    shipment.contact_or_reference_raw = contact_or_reference_raw
    
    shipment.item_category = item_category
    shipment.parcel_description = parcel_description
    
    shipment.dead_weight = parse_float(dead_weight)
    shipment.volumetric_weight = parse_float(volumetric_weight)
    shipment.charged_weight = parse_float(charged_weight)
    shipment.weight_basis = weight_basis
    shipment.dead_weight_text = dead_weight_text
    shipment.volumetric_weight_text = volumetric_weight_text
    shipment.charged_weight_text = charged_weight_text
    
    shipment.customer_rate_text = customer_rate_text
    shipment.vendor_rate_text = vendor_rate_text
    shipment.courier_company = courier_company
    shipment.vendor_partner = vendor_partner
    
    shipment.promised_days_text = promised_days_text
    shipment.promised_days_number = parse_int(promised_days_number)
    
    shipment.billed_amount = parsed_billed
    shipment.received_amount = parsed_received
    shipment.self_cost = parsed_self_cost
    shipment.other_expense = parsed_other_exp
    shipment.total_cost = total_cost
    shipment.service_value = service_value
    shipment.balance_amount = balance_amount
    
    shipment.status_raw_text = status_raw_text
    if status_raw_text and status_raw_text.strip():
        shipment.last_status_text = status_raw_text
        if not shipment.last_status_at:
            shipment.last_status_at = shipment.booking_date

    if overall_status == "delivered" and not shipment.delivered_at:
        shipment.delivered_at = shipment.booking_date
    shipment.overall_status = overall_status
    shipment.requires_lm_awb = requires_lm_awb
    
    shipment.internal_notes = internal_notes
    shipment.customer_notes = customer_notes
    shipment.balance_notes = balance_notes
    shipment.raw_excel_notes = raw_excel_notes
    shipment.raw_excel_row_text = raw_excel_row_text
    
    upsert_tracking(db, shipment.id, "main_awb", main_tracking_number, main_tracking_courier, True)
    upsert_tracking(db, shipment.id, "lm_awb", lm_awb_number, lm_awb_courier, False)
    
    db.commit()
    return RedirectResponse(url=f"/shipments/{shipment.id}", status_code=303)
