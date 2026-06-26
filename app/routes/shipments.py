from fastapi import APIRouter, Request, Depends, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import get_db
from app.models import Shipment, TrackingEvent, TrackingNumber, now_ist
from app.tracking_links import build_tracking_site_url, build_tracking_url
from decimal import Decimal
from datetime import datetime
import json

router = APIRouter(prefix="/shipments")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["tracking_url"] = build_tracking_url
templates.env.globals["tracking_site_url"] = build_tracking_site_url

RECEIVER_ADDRESS_PREFIX = "RECEIVER_ADDRESS_JSON:"
LEGACY_SENDER_ADDRESS_PREFIX = "SENDER_ADDRESS_JSON:"
ITEM_RAW_PREFIX = "ITEM_RAW_TEXT_JSON:"
RATE_DETAILS_PREFIX = "RATE_DETAILS_JSON:"
VOLUMETRIC_DIMENSIONS_PREFIX = "VOLUMETRIC_DIMENSIONS_JSON:"

DEFAULT_COURIERS = [
    "DHL",
    "Aramax",
    "Quickship",
    "DTDC",
    "UPS",
    "Overseas",
    "Atlantic",
    "FedEx",
    "IndiaPost",
    "DPD",
    "MAWW",
]

COUNTRY_ALIASES = {
    "AUS": "AUSTRALIA",
    "AUST": "AUSTRALIA",
    "AUSTRALIA": "AUSTRALIA",
    "CAN": "CANADA",
    "CANADA": "CANADA",
    "GER": "GERMANY",
    "GERMANY": "GERMANY",
    "EUROPE": "EUROPE",
    "NZ": "NEW ZEALAND",
    "NEW ZEALAND": "NEW ZEALAND",
    "SINGAPORE": "SINGAPORE",
    "UK": "UNITED KINGDOM",
    "U.K.": "UNITED KINGDOM",
    "UNITED KINGDOM": "UNITED KINGDOM",
    "LONDON": "UNITED KINGDOM",
    "US": "UNITED STATES",
    "USA": "UNITED STATES",
    "U.S.A.": "UNITED STATES",
    "UNITED STATES": "UNITED STATES",
}

DEFAULT_COUNTRIES = [
    "AUSTRALIA",
    "CANADA",
    "GERMANY",
    "EUROPE",
    "NEW ZEALAND",
    "SINGAPORE",
    "UNITED KINGDOM",
    "UNITED STATES",
]

def normalize_country(value: str | None) -> str:
    country = " ".join((value or "").strip().upper().replace("/", " ").replace("(", " ").replace(")", " ").split())
    if country.startswith("AUS") or country.startswith("AUST"):
        return "AUSTRALIA"
    if country.startswith("CAN"):
        return "CANADA"
    if country.startswith("GER"):
        return "GERMANY"
    return COUNTRY_ALIASES.get(country, country)

def normalize_proper_case(value: str | None) -> str:
    text = " ".join((value or "").strip().split())
    return text.title() if text else ""

def get_country_options(db: Session) -> list[str]:
    existing = [normalize_country(row[0]) for row in db.query(Shipment.destination_country).distinct().all() if row[0]]
    return sorted({country for country in DEFAULT_COUNTRIES + existing if country}, key=str.lower)

def get_courier_options(db: Session) -> list[str]:
    shipment_couriers = [row[0] for row in db.query(Shipment.courier_company).distinct().all() if row[0]]
    tracking_couriers = [row[0] for row in db.query(TrackingNumber.courier_name).distinct().all() if row[0]]
    return sorted({c.strip() for c in DEFAULT_COURIERS + shipment_couriers + tracking_couriers if c and c.strip()}, key=str.lower)

def parse_receiver_address(raw_notes: str | None) -> dict[str, str]:
    blank = {
        "line_1": "",
        "line_2": "",
        "line_3": "",
        "city": "",
        "state": "",
        "zip": "",
    }
    if not raw_notes:
        return blank

    for line in raw_notes.splitlines():
        if line.startswith(RECEIVER_ADDRESS_PREFIX):
            try:
                parsed = json.loads(line[len(RECEIVER_ADDRESS_PREFIX):])
            except json.JSONDecodeError:
                return blank
            return {key: str(parsed.get(key, "") or "") for key in blank}
    return blank

def format_receiver_address(address: dict[str, str]) -> str:
    return ", ".join(value for value in address.values() if value)

def encode_receiver_address(raw_notes: str | None, address: dict[str, str]) -> str:
    preserved_lines = [
        line for line in (raw_notes or "").splitlines()
        if not line.startswith(RECEIVER_ADDRESS_PREFIX)
        and not line.startswith(LEGACY_SENDER_ADDRESS_PREFIX)
    ]
    if any(value.strip() for value in address.values()):
        preserved_lines.append(
            RECEIVER_ADDRESS_PREFIX + json.dumps(address, separators=(",", ":"))
        )
    return "\n".join(line for line in preserved_lines if line.strip())

def parse_item_raw_text(raw_notes: str | None) -> str:
    if not raw_notes:
        return ""

    for line in raw_notes.splitlines():
        if line.startswith(ITEM_RAW_PREFIX):
            try:
                parsed = json.loads(line[len(ITEM_RAW_PREFIX):])
            except json.JSONDecodeError:
                return ""
            return str(parsed or "")
    return ""

def encode_item_raw_text(raw_notes: str | None, raw_text: str) -> str:
    preserved_lines = [
        line for line in (raw_notes or "").splitlines()
        if not line.startswith(ITEM_RAW_PREFIX)
    ]
    if raw_text.strip():
        preserved_lines.append(
            ITEM_RAW_PREFIX + json.dumps(raw_text.strip(), separators=(",", ":"))
        )
    return "\n".join(line for line in preserved_lines if line.strip())

def parse_rate_details(raw_notes: str | None) -> dict[str, str]:
    blank = {
        "customer_charged_weight": "",
        "customer_charged_weight_unit": "KG",
        "vendor_charged_weight": "",
        "vendor_charged_weight_unit": "KG",
    }
    if not raw_notes:
        return blank

    for line in raw_notes.splitlines():
        if line.startswith(RATE_DETAILS_PREFIX):
            try:
                parsed = json.loads(line[len(RATE_DETAILS_PREFIX):])
            except json.JSONDecodeError:
                return blank
            return {
                "customer_charged_weight": str(parsed.get("customer_charged_weight", parsed.get("customer_charged_rate", "")) or ""),
                "customer_charged_weight_unit": normalize_unit(str(parsed.get("customer_charged_weight_unit", "KG") or "KG")),
                "vendor_charged_weight": str(parsed.get("vendor_charged_weight", parsed.get("vendor_charged_rate", "")) or ""),
                "vendor_charged_weight_unit": normalize_unit(str(parsed.get("vendor_charged_weight_unit", "KG") or "KG")),
            }
    return blank

def encode_rate_details(raw_notes: str | None, rates: dict[str, str]) -> str:
    preserved_lines = [
        line for line in (raw_notes or "").splitlines()
        if not line.startswith(RATE_DETAILS_PREFIX)
    ]
    cleaned = {
        "customer_charged_weight": str(rates.get("customer_charged_weight", "") or "").strip(),
        "customer_charged_weight_unit": "KG",
        "vendor_charged_weight": str(rates.get("vendor_charged_weight", "") or "").strip(),
        "vendor_charged_weight_unit": "KG",
    }
    if cleaned["customer_charged_weight"] or cleaned["vendor_charged_weight"]:
        preserved_lines.append(
            RATE_DETAILS_PREFIX + json.dumps(cleaned, separators=(",", ":"))
        )
    return "\n".join(line for line in preserved_lines if line.strip())


def parse_volumetric_dimensions(raw_notes: str | None) -> dict[str, str]:
    blank = {"length": "", "width": "", "height": ""}
    if not raw_notes:
        return blank
    for line in raw_notes.splitlines():
        if line.startswith(VOLUMETRIC_DIMENSIONS_PREFIX):
            try:
                parsed = json.loads(line[len(VOLUMETRIC_DIMENSIONS_PREFIX):])
            except json.JSONDecodeError:
                return blank
            return {key: str(parsed.get(key, "") or "") for key in blank}
    return blank


def encode_volumetric_dimensions(raw_notes: str | None, dimensions: dict[str, str]) -> str:
    preserved_lines = [
        line for line in (raw_notes or "").splitlines()
        if not line.startswith(VOLUMETRIC_DIMENSIONS_PREFIX)
    ]
    cleaned = {key: str(dimensions.get(key, "") or "").strip() for key in ["length", "width", "height"]}
    if any(cleaned.values()):
        preserved_lines.append(
            VOLUMETRIC_DIMENSIONS_PREFIX + json.dumps(cleaned, separators=(",", ":"))
        )
    return "\n".join(line for line in preserved_lines if line.strip())


def normalize_unit(val: str, default: str = "KG") -> str:
    normalized = (val or default).strip().upper()
    return normalized or default

def parse_int(val: str) -> int | None:
    try:
        return int(val) if val and val.strip() else None
    except ValueError:
        return None

def parse_decimal(val: str | int | float | Decimal | None) -> Decimal:
    try:
        return Decimal(str(val).strip()) if val is not None and str(val).strip() else Decimal("0.0")
    except Exception:
        return Decimal("0.0")


def decimal_to_plain(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")


def parse_float(val: str | int | float | None) -> float:
    try:
        return float(val) if val is not None and str(val).strip() else 0.0
    except ValueError:
        return 0.0


def calculate_volumetric_weight(length: str, width: str, height: str) -> float:
    length_value = parse_float(length)
    width_value = parse_float(width)
    height_value = parse_float(height)
    if not length_value or not width_value or not height_value:
        return 0.0
    return round((length_value * width_value * height_value) / 5000, 2)


def extract_per_kg_rate(rate_text: str | None) -> str:
    if not rate_text:
        return ""
    import re
    numbers = re.findall(r'\d+(?:\.\d+)?', str(rate_text))
    return numbers[-1] if numbers else ""


def calculate_rate_amount(weight: str, per_kg_rate: str) -> Decimal:
    kg_weight = parse_decimal(weight)
    rate = parse_decimal(extract_per_kg_rate(per_kg_rate))
    return (kg_weight * rate).quantize(Decimal("0.01")) if kg_weight and rate else Decimal("0.0")


def status_label(status: str) -> str:
    return (status or "booked").replace("_", " ").title()


def add_status_timeline_event(db: Session, shipment: Shipment, status: str, notes: str = "", source: str = "status_update"):
    event_time = now_ist()
    event = TrackingEvent(
        shipment_id=shipment.id,
        event_time=event_time,
        status_text=status_label(status),
        normalized_status=status,
        notes=(notes or "").strip(),
        source=source,
    )
    db.add(event)
    shipment.last_status_text = (notes or "").strip() or status_label(status)
    shipment.last_status_at = event_time
    shipment.last_normalized_status = status
    if status == "delivered" and not shipment.delivered_at:
        shipment.delivered_at = event_time

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
    status: str = "",
    country: str = ""
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
                Shipment.status_raw_text.ilike(f"%{q}%")
            )
        )
    if status:
        query = query.filter(Shipment.overall_status == status)
    normalized_country_filter = normalize_country(country)
    if normalized_country_filter:
        query = query.filter(Shipment.destination_country == normalized_country_filter)
        
    shipments = query.order_by(Shipment.booking_date.desc()).distinct().all()
    shipment_previews = {}
    for shipment in shipments:
        item_raw_text = parse_item_raw_text(shipment.raw_excel_notes)
        receiver_address = parse_receiver_address(shipment.raw_excel_notes)
        shipment_previews[shipment.id] = {
            "item_raw_text": item_raw_text,
            "address": format_receiver_address(receiver_address),
        }
    courier_options = get_courier_options(db)
    country_options = get_country_options(db)
    
    return templates.TemplateResponse("shipments/list.html", {
        "request": request,
        "shipments": shipments,
        "shipment_previews": shipment_previews,
        "courier_options": courier_options,
        "country_options": country_options,
        "q": q,
        "status": status,
        "country": normalized_country_filter
    })

@router.get("/new")
def new_shipment_form(request: Request, db: Session = Depends(get_db)):
    today = now_ist().strftime("%Y-%m-%d")
    return templates.TemplateResponse("shipments/new.html", {
        "request": request,
        "today": today,
        "item_raw_text": "",
        "courier_options": get_courier_options(db),
        "country_options": get_country_options(db)
    })

@router.post("/new")
def create_shipment(
    request: Request,
    db: Session = Depends(get_db),
    customer_name: str = Form(""),
    receiver_address_line_1: str = Form(""),
    receiver_address_line_2: str = Form(""),
    receiver_address_line_3: str = Form(""),
    receiver_state: str = Form(""),
    receiver_zip: str = Form(""),
    receiver_name: str = Form(""),
    destination_country: str = Form(""),
    destination_city: str = Form(""),
    customer_phone: str = Form(""),
    name_country_raw: str = Form(""),
    contact_or_reference_raw: str = Form(""),
    
    item_raw_text: str = Form(""),
    
    dead_weight: str = Form("0.0"),
    volumetric_weight: str = Form("0.0"),
    volumetric_length: str = Form(""),
    volumetric_width: str = Form(""),
    volumetric_height: str = Form(""),
    weight_basis: str = Form("actual"),
    dead_weight_unit: str = Form("KG"),
    volumetric_weight_unit: str = Form("KG"),
    
    customer_rate_text: str = Form(""),
    customer_charged_weight: str = Form(""),
    customer_charged_weight_unit: str = Form("KG"),
    vendor_rate_text: str = Form(""),
    vendor_charged_weight: str = Form(""),
    vendor_charged_weight_unit: str = Form("KG"),
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
    parsed_billed = calculate_rate_amount(customer_charged_weight, customer_rate_text) or parse_decimal(billed_amount)
    parsed_received = parse_decimal(received_amount)
    parsed_self_cost = calculate_rate_amount(vendor_charged_weight, vendor_rate_text) or parse_decimal(self_cost)
    parsed_other_exp = parse_decimal(other_expense)
    
    total_cost = parsed_self_cost + parsed_other_exp
    service_value = parsed_billed - total_cost
    balance_amount = parsed_billed - parsed_received
    
    parsed_booking_date = now_ist()
    if booking_date and booking_date.strip():
        try:
            parsed_booking_date = datetime.strptime(booking_date.strip(), "%Y-%m-%d")
        except ValueError:
            pass

    normalized_destination_city = normalize_proper_case(destination_city)
    normalized_receiver_state = normalize_proper_case(receiver_state)
    normalized_receiver_zip = " ".join((receiver_zip or "").strip().split()).upper()
    normalized_vendor_partner = normalize_proper_case(vendor_partner)
    dead_weight_value = parse_float(dead_weight)
    volumetric_weight_value = calculate_volumetric_weight(volumetric_length, volumetric_width, volumetric_height) or parse_float(volumetric_weight)
    weight_basis_value = "volumetric" if volumetric_weight_value > dead_weight_value else "actual"

    receiver_address = {
        "line_1": receiver_address_line_1.strip(),
        "line_2": receiver_address_line_2.strip(),
        "line_3": receiver_address_line_3.strip(),
        "city": normalized_destination_city,
        "state": normalized_receiver_state,
        "zip": normalized_receiver_zip,
    }

    notes_with_receiver = encode_receiver_address(raw_excel_notes, receiver_address)
    notes_with_items = encode_item_raw_text(notes_with_receiver, item_raw_text)
    notes_with_dimensions = encode_volumetric_dimensions(notes_with_items, {
        "length": volumetric_length,
        "width": volumetric_width,
        "height": volumetric_height,
    })
    notes_with_rates = encode_rate_details(notes_with_dimensions, {
        "customer_charged_weight": customer_charged_weight,
        "customer_charged_weight_unit": customer_charged_weight_unit,
        "vendor_charged_weight": vendor_charged_weight,
        "vendor_charged_weight_unit": vendor_charged_weight_unit,
    })

    shipment = Shipment(
        booking_date=parsed_booking_date,
        customer_name=customer_name,
        receiver_name=receiver_name,
        destination_country=normalize_country(destination_country),
        destination_city=normalized_destination_city,
        customer_phone=customer_phone,
        name_country_raw=name_country_raw,
        contact_or_reference_raw=contact_or_reference_raw,
        parcel_description="",
        dead_weight=dead_weight_value,
        volumetric_weight=volumetric_weight_value,
        charged_weight=0.0,
        weight_basis=weight_basis_value,
        dead_weight_text="KG",
        volumetric_weight_text="KG",
        charged_weight_text="",
        customer_rate_text=customer_rate_text,
        vendor_rate_text=vendor_rate_text,
        courier_company=main_tracking_courier,
        vendor_partner=normalized_vendor_partner,
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
        raw_excel_notes=notes_with_rates,
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

    if overall_status or status_raw_text.strip():
        add_status_timeline_event(db, shipment, overall_status or "booked", status_raw_text, "shipment_create")
    
    # Tracking numbers
    upsert_tracking(db, shipment.id, "main_awb", main_tracking_number, main_tracking_courier, True)
    upsert_tracking(db, shipment.id, "lm_awb", lm_awb_number, lm_awb_courier, False)
    db.commit()
    
    return RedirectResponse(url=f"/shipments/{shipment.id}", status_code=303)

@router.post("/{shipment_id}/quick-update")
def quick_update_shipment(
    shipment_id: int,
    db: Session = Depends(get_db),
    overall_status: str = Form("booked"),
    status_raw_text: str = Form(""),
    main_tracking_number: str = Form(""),
    main_tracking_courier: str = Form(""),
    lm_awb_number: str = Form(""),
    lm_awb_courier: str = Form(""),
    internal_notes: str = Form(""),
    row_color: str | None = Form(None),
    next_url: str = Form("/shipments")
):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    redirect_url = next_url if next_url.startswith("/shipments") else "/shipments"
    if not shipment:
        return RedirectResponse(url=redirect_url, status_code=303)

    old_status = shipment.overall_status or "booked"
    old_notes = shipment.status_raw_text or ""
    new_status = overall_status.strip() or old_status
    new_notes = status_raw_text.strip()
    shipment.overall_status = new_status
    shipment.status_raw_text = new_notes
    if new_status != old_status or new_notes != old_notes:
        add_status_timeline_event(db, shipment, new_status, new_notes, "row_status_update")
    shipment.internal_notes = internal_notes.strip()
    if row_color is not None:
        selected_color = row_color.strip().lower()
        shipment.row_color = selected_color if selected_color in {"green", "yellow", "red"} else None

    upsert_tracking(db, shipment.id, "main_awb", main_tracking_number, main_tracking_courier, True)
    upsert_tracking(db, shipment.id, "lm_awb", lm_awb_number, lm_awb_courier, False)
    db.commit()

    return RedirectResponse(url=redirect_url, status_code=303)
@router.get("/{shipment_id}")
def shipment_detail(request: Request, shipment_id: int, db: Session = Depends(get_db)):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        return RedirectResponse(url="/shipments", status_code=303)
        
    receiver_address = parse_receiver_address(shipment.raw_excel_notes)
    item_raw_text = parse_item_raw_text(shipment.raw_excel_notes)
    rate_details = parse_rate_details(shipment.raw_excel_notes)
    volumetric_dimensions = parse_volumetric_dimensions(shipment.raw_excel_notes)
    return templates.TemplateResponse("shipments/detail.html", {
        "request": request,
        "shipment": shipment,
        "receiver_address": receiver_address,
        "receiver_address_text": format_receiver_address(receiver_address),
        "item_raw_text": item_raw_text,
        "rate_details": rate_details,
        "volumetric_dimensions": volumetric_dimensions,
        "customer_per_kg_rate": extract_per_kg_rate(shipment.customer_rate_text),
        "vendor_per_kg_rate": extract_per_kg_rate(shipment.vendor_rate_text)
    })

@router.get("/{shipment_id}/edit")
def edit_shipment_form(request: Request, shipment_id: int, db: Session = Depends(get_db)):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        return RedirectResponse(url="/shipments", status_code=303)
        
    main_awb = next((tn for tn in shipment.tracking_numbers if tn.tracking_type == "main_awb"), None)
    lm_awb = next((tn for tn in shipment.tracking_numbers if tn.tracking_type == "lm_awb"), None)
        
    receiver_address = parse_receiver_address(shipment.raw_excel_notes)
    item_raw_text = parse_item_raw_text(shipment.raw_excel_notes)
    rate_details = parse_rate_details(shipment.raw_excel_notes)
    volumetric_dimensions = parse_volumetric_dimensions(shipment.raw_excel_notes)
    return templates.TemplateResponse("shipments/edit.html", {
        "request": request,
        "shipment": shipment,
        "main_awb": main_awb,
        "lm_awb": lm_awb,
        "receiver_address": receiver_address,
        "item_raw_text": item_raw_text,
        "rate_details": rate_details,
        "volumetric_dimensions": volumetric_dimensions,
        "customer_per_kg_rate": extract_per_kg_rate(shipment.customer_rate_text),
        "vendor_per_kg_rate": extract_per_kg_rate(shipment.vendor_rate_text),
        "courier_options": get_courier_options(db),
        "country_options": get_country_options(db)
    })

@router.post("/{shipment_id}/edit")
def update_shipment(
    request: Request,
    shipment_id: int,
    db: Session = Depends(get_db),
    customer_name: str = Form(""),
    receiver_address_line_1: str = Form(""),
    receiver_address_line_2: str = Form(""),
    receiver_address_line_3: str = Form(""),
    receiver_state: str = Form(""),
    receiver_zip: str = Form(""),
    receiver_name: str = Form(""),
    destination_country: str = Form(""),
    destination_city: str = Form(""),
    customer_phone: str = Form(""),
    name_country_raw: str = Form(""),
    contact_or_reference_raw: str = Form(""),
    
    item_raw_text: str = Form(""),
    
    dead_weight: str = Form("0.0"),
    volumetric_weight: str = Form("0.0"),
    volumetric_length: str = Form(""),
    volumetric_width: str = Form(""),
    volumetric_height: str = Form(""),
    weight_basis: str = Form("actual"),
    dead_weight_unit: str = Form("KG"),
    volumetric_weight_unit: str = Form("KG"),
    
    customer_rate_text: str = Form(""),
    customer_charged_weight: str = Form(""),
    customer_charged_weight_unit: str = Form("KG"),
    vendor_rate_text: str = Form(""),
    vendor_charged_weight: str = Form(""),
    vendor_charged_weight_unit: str = Form("KG"),
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

    old_status = shipment.overall_status or "booked"
    old_notes = shipment.status_raw_text or ""
        
    parsed_billed = calculate_rate_amount(customer_charged_weight, customer_rate_text) or parse_decimal(billed_amount)
    parsed_received = parse_decimal(received_amount)
    parsed_self_cost = calculate_rate_amount(vendor_charged_weight, vendor_rate_text) or parse_decimal(self_cost)
    parsed_other_exp = parse_decimal(other_expense)
        
    total_cost = parsed_self_cost + parsed_other_exp
    service_value = parsed_billed - total_cost
    balance_amount = parsed_billed - parsed_received
    
    if booking_date and booking_date.strip():
        try:
            shipment.booking_date = datetime.strptime(booking_date.strip(), "%Y-%m-%d")
        except ValueError:
            pass
    
    normalized_destination_city = normalize_proper_case(destination_city)
    normalized_receiver_state = normalize_proper_case(receiver_state)
    normalized_receiver_zip = " ".join((receiver_zip or "").strip().split()).upper()
    normalized_vendor_partner = normalize_proper_case(vendor_partner)
    dead_weight_value = parse_float(dead_weight)
    volumetric_weight_value = calculate_volumetric_weight(volumetric_length, volumetric_width, volumetric_height) or parse_float(volumetric_weight)
    weight_basis_value = "volumetric" if volumetric_weight_value > dead_weight_value else "actual"

    receiver_address = {
        "line_1": receiver_address_line_1.strip(),
        "line_2": receiver_address_line_2.strip(),
        "line_3": receiver_address_line_3.strip(),
        "city": normalized_destination_city,
        "state": normalized_receiver_state,
        "zip": normalized_receiver_zip,
    }

    shipment.customer_name = customer_name
    shipment.receiver_name = receiver_name
    shipment.destination_country = normalize_country(destination_country)
    shipment.destination_city = normalized_destination_city
    shipment.customer_phone = customer_phone
    shipment.name_country_raw = name_country_raw
    shipment.contact_or_reference_raw = contact_or_reference_raw
    
    shipment.parcel_description = ""
    
    shipment.dead_weight = dead_weight_value
    shipment.volumetric_weight = volumetric_weight_value
    shipment.charged_weight = 0.0
    shipment.weight_basis = weight_basis_value
    shipment.dead_weight_text = "KG"
    shipment.volumetric_weight_text = "KG"
    shipment.charged_weight_text = ""
    
    shipment.customer_rate_text = customer_rate_text
    shipment.vendor_rate_text = vendor_rate_text
    shipment.courier_company = main_tracking_courier
    shipment.vendor_partner = normalized_vendor_partner
    
    shipment.promised_days_text = promised_days_text
    shipment.promised_days_number = parse_int(promised_days_number)
    
    shipment.billed_amount = parsed_billed
    shipment.received_amount = parsed_received
    shipment.self_cost = parsed_self_cost
    shipment.other_expense = parsed_other_exp
    shipment.total_cost = total_cost
    shipment.service_value = service_value
    shipment.balance_amount = balance_amount
    
    new_status = overall_status.strip() or old_status
    new_notes = status_raw_text.strip()
    shipment.status_raw_text = new_notes
    shipment.overall_status = new_status
    if new_status != old_status or new_notes != old_notes:
        add_status_timeline_event(db, shipment, new_status, new_notes, "shipment_edit")
    shipment.requires_lm_awb = requires_lm_awb
    
    shipment.internal_notes = internal_notes
    shipment.customer_notes = customer_notes
    shipment.balance_notes = balance_notes
    notes_with_receiver = encode_receiver_address(raw_excel_notes, receiver_address)
    notes_with_items = encode_item_raw_text(notes_with_receiver, item_raw_text)
    notes_with_dimensions = encode_volumetric_dimensions(notes_with_items, {
        "length": volumetric_length,
        "width": volumetric_width,
        "height": volumetric_height,
    })
    shipment.raw_excel_notes = encode_rate_details(notes_with_dimensions, {
        "customer_charged_weight": customer_charged_weight,
        "customer_charged_weight_unit": customer_charged_weight_unit,
        "vendor_charged_weight": vendor_charged_weight,
        "vendor_charged_weight_unit": vendor_charged_weight_unit,
    })
    shipment.raw_excel_row_text = raw_excel_row_text
    
    upsert_tracking(db, shipment.id, "main_awb", main_tracking_number, main_tracking_courier, True)
    upsert_tracking(db, shipment.id, "lm_awb", lm_awb_number, lm_awb_courier, False)
    
    db.commit()
    return RedirectResponse(url=f"/shipments/{shipment.id}", status_code=303)
