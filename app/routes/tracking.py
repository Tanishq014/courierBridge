from fastapi import APIRouter, Request, Depends, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import TrackingNumber, TrackingEvent, TrackingTemplate, Shipment, now_ist
from app.tracking_links import build_tracking_site_url, build_tracking_url
from datetime import datetime
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

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
    notes: str = Form(""),
    db: Session = Depends(get_db)
):
    ev = TrackingEvent(
        shipment_id=shipment_id,
        event_time=now_ist(),
        status_text=status_text,
        location=location,
        normalized_status=normalized_status,
        notes=notes.strip(),
        source="manual"
    )
    db.add(ev)
    
    # Update denormalized fields on shipment
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if shipment:
        shipment.status_raw_text = notes.strip() or status_text
        shipment.last_status_text = notes.strip() or status_text
        shipment.last_status_at = ev.event_time
        shipment.last_status_location = location
        shipment.last_normalized_status = normalized_status
        
        shipment.overall_status = normalized_status
        if normalized_status == "delivered" and not shipment.delivered_at:
            shipment.delivered_at = ev.event_time
                
    db.commit()
    return RedirectResponse(url=f"/shipments/{shipment_id}", status_code=303)


@router.get("/atlantic")
def atlantic_tracking_page(request: Request, awb: str = ""):
    return templates.TemplateResponse("tracking/atlantic_debug.html", {
        "request": request,
        "awb": awb.strip(),
        "atlantic_url": "https://atlanticcourier.net/track/",
    })

@router.get("/atlantic/lookup")
def atlantic_tracking_lookup(awb: str = ""):
    awb = awb.strip()
    if not awb:
        return JSONResponse({"ok": False, "error": "Missing AWB", "debug": {"stage": "validate"}}, status_code=400)

    payload = {"function": "track", "awbno": awb, "searchby": "A"}
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        "https://atlanticcourier.net/action",
        data=body,
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://atlanticcourier.net",
            "Referer": "https://atlanticcourier.net/track/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    debug = {
        "url": "https://atlanticcourier.net/action",
        "method": "POST",
        "payload": payload,
        "content_type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            parsed = None
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                pass
            return JSONResponse({
                "ok": True,
                "status": response.status,
                "debug": debug,
                "raw": raw,
                "json": parsed,
            })
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return JSONResponse({"ok": False, "status": exc.code, "debug": debug, "raw": raw}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc), "debug": debug}, status_code=502)



def extract_overseas_tracking_section(raw: str) -> str:
    start = raw.find('<table id="ContentPlaceHolder1_DataList1"')
    if start < 0:
        start = raw.find('OVERSEAS TRACKING SYSTEM')
    if start < 0:
        return raw
    wrapper_start = raw.rfind('<div', 0, start)
    if wrapper_start >= 0:
        start = wrapper_start
    end_marker = '<!-- container -->'
    end = raw.find(end_marker, start)
    if end < 0:
        end = raw.find('<section class="margin-bottom">', start)
    if end < 0:
        end = raw.find('<aside id="footer-widgets"', start)
    if end < 0:
        end = len(raw)
    return raw[start:end]

@router.get("/overseas")
def overseas_tracking_page(request: Request, awb: str = ""):
    return templates.TemplateResponse("tracking/overseas_debug.html", {
        "request": request,
        "awb": awb.strip(),
        "overseas_url": "https://track.overseaslogistic.com/tracking.aspx",
    })

@router.get("/overseas/lookup")
def overseas_tracking_lookup(awb: str = ""):
    awb = awb.strip()
    if not awb:
        return JSONResponse({"ok": False, "error": "Missing AWB", "debug": {"stage": "validate"}}, status_code=400)

    url = "https://track.overseaslogistic.com/tracking.aspx"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
    }
    debug = {"url": url, "method": "GET+POST", "awb": awb}

    def hidden_value(page: str, field_id: str) -> str:
        pattern = rf'id="{re.escape(field_id)}" value="([^"]*)"'
        match = re.search(pattern, page)
        return html.unescape(match.group(1)) if match else ""

    try:
        get_request = urllib.request.Request(url, headers=headers, method="GET")
        with opener.open(get_request, timeout=20) as response:
            initial_html = response.read().decode("utf-8", errors="replace")
        tokens = {
            "__VIEWSTATE": hidden_value(initial_html, "__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": hidden_value(initial_html, "__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": hidden_value(initial_html, "__EVENTVALIDATION"),
        }
        missing = [key for key, value in tokens.items() if not value]
        debug["tokens_found"] = {key: bool(value) for key, value in tokens.items()}
        if missing:
            return JSONResponse({"ok": False, "error": f"Missing hidden fields: {', '.join(missing)}", "debug": debug}, status_code=502)

        form = {
            "__VIEWSTATE": tokens["__VIEWSTATE"],
            "__VIEWSTATEGENERATOR": tokens["__VIEWSTATEGENERATOR"],
            "__EVENTVALIDATION": tokens["__EVENTVALIDATION"],
            "ctl00$ContentPlaceHolder1$text": awb,
            "ctl00$ContentPlaceHolder1$Button1": "Track",
        }
        body = urllib.parse.urlencode(form).encode("utf-8")
        post_headers = {
            **headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://track.overseaslogistic.com",
            "Referer": url,
        }
        post_request = urllib.request.Request(url, data=body, headers=post_headers, method="POST")
        with opener.open(post_request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
        debug["posted_fields"] = list(form.keys())
        useful_html = extract_overseas_tracking_section(raw)
        debug["raw_length"] = len(raw)
        debug["useful_length"] = len(useful_html)
        return JSONResponse({"ok": True, "status": 200, "debug": debug, "raw": useful_html})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return JSONResponse({"ok": False, "status": exc.code, "debug": debug, "raw": raw}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc), "debug": debug}, status_code=502)

@router.get("/number/{tn_id}/open")
def open_tracking_url(tn_id: int, db: Session = Depends(get_db)):
    tn = db.query(TrackingNumber).filter(TrackingNumber.id == tn_id).first()
    if not tn:
        return RedirectResponse(url="/shipments", status_code=303)
        
    db_templates = {
        row.courier_name: row.template_url
        for row in db.query(TrackingTemplate).all()
        if row.courier_name and row.template_url
    }
    effective_courier = tn.courier_name or (tn.shipment.courier_company if tn.shipment and tn.tracking_type == "main_awb" else "")
    url = build_tracking_url(effective_courier, tn.tracking_number, db_templates, tn.tracking_type)
    if url:
        return RedirectResponse(url=url, status_code=303)
    site_url = build_tracking_site_url(effective_courier, tn.tracking_number)
    if site_url:
        return RedirectResponse(url=site_url, status_code=303)
        
    # If no template, show fallback page
    return f"No template found for {effective_courier or tn.courier_name}. Tracking Number: {tn.tracking_number}"

@router.post("/event/{event_id}/delete")
def delete_tracking_event(event_id: int, db: Session = Depends(get_db)):
    event = db.query(TrackingEvent).filter(TrackingEvent.id == event_id).first()
    if not event:
        return RedirectResponse(url="/shipments", status_code=303)

    shipment_id = event.shipment_id
    db.delete(event)
    db.commit()
    return RedirectResponse(url=f"/shipments/{shipment_id}", status_code=303)
