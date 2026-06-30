from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import unescape
from typing import Any

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Shipment, ShipmentAIStatus, TrackingCheck, TrackingNumber, now_ist
from app.routes.shipments import add_status_timeline_event, register_tracking_after_save, upsert_tracking
from app.tracking_ai import analyze_tracking_batch
from app.tracking_fetch import fetch_tracking_for_number, fallback_events_from_shipment, find_lm_awb

router = APIRouter(prefix="/ai-tracking")

TERMINAL_STATUSES = {"delivered", "rto", "return_damage"}


@dataclass
class ShipmentTrackingInput:
    shipment: Shipment
    checks: list[TrackingCheck]
    events: list[dict[str, Any]]
    found_lm_awb: str = ""
    found_lm_courier: str = ""


def redirect_back(next_url: str | None = None) -> RedirectResponse:
    safe_url = next_url if next_url and next_url.startswith("/") else "/shipments"
    return RedirectResponse(url=safe_url, status_code=303)


def tracking_numbers_for_fetch(shipment: Shipment) -> list[TrackingNumber]:
    numbers = [tn for tn in shipment.tracking_numbers if (tn.tracking_number or "").strip()]
    numbers.sort(key=lambda tn: 0 if tn.tracking_type == "lm_awb" else 1 if tn.tracking_type == "main_awb" else 2)
    return numbers


def effective_courier(shipment: Shipment, tn: TrackingNumber) -> str:
    if tn.courier_name:
        return tn.courier_name
    if tn.tracking_type == "main_awb":
        return shipment.courier_company or ""
    return ""


def build_tracking_payload(shipment: Shipment, events: list[dict[str, Any]], found_lm_awb: str = "", found_lm_courier: str = "") -> dict[str, Any]:
    age_days = None
    if shipment.booking_date:
        booking_date = shipment.booking_date.date()
        age_days = (now_ist().date() - booking_date).days
    return {
        "shipment_id": shipment.id,
        "booking_date": shipment.booking_date.isoformat() if shipment.booking_date else None,
        "age_days": age_days,
        "promised_days": shipment.promised_days_number,
        "destination_country": shipment.destination_country or "",
        "destination_city": shipment.destination_city or "",
        "current_app_status": shipment.overall_status or "booked",
        "current_status_note": shipment.status_raw_text or shipment.last_status_text or "",
        "customer_name": shipment.customer_name or "",
        "receiver_name": shipment.receiver_name or "",
        "requires_lm_awb": bool(shipment.requires_lm_awb),
        "has_lm_awb": any(tn.tracking_type == "lm_awb" and (tn.tracking_number or "").strip() for tn in shipment.tracking_numbers),
        "main_awbs": [tn.tracking_number.strip().upper() for tn in shipment.tracking_numbers if tn.tracking_type == "main_awb" and tn.tracking_number],
        "lm_awbs": [tn.tracking_number.strip().upper() for tn in shipment.tracking_numbers if tn.tracking_type == "lm_awb" and tn.tracking_number],
        "found_lm_awb": found_lm_awb,
        "found_lm_courier": found_lm_courier,
        "tracking_events": events[:25],
    }


def save_ai_status(db: Session, shipment_input: ShipmentTrackingInput, result: dict[str, Any], provider: str, model_name: str) -> ShipmentAIStatus:
    latest_check = shipment_input.checks[0] if shipment_input.checks else None
    found_lm_awb = str(result.get("found_lm_awb") or shipment_input.found_lm_awb or "").strip()
    found_lm_courier = str(result.get("found_lm_courier") or shipment_input.found_lm_courier or "").strip()

    # Final check: NEVER suggest an LM AWB if the shipment already has it
    if found_lm_awb:
        existing = {tn.tracking_number.strip().lower() for tn in shipment_input.shipment.tracking_numbers if tn.tracking_type == "lm_awb" and tn.tracking_number}
        if found_lm_awb.lower() in existing:
            found_lm_awb = ""
            found_lm_courier = ""

    status = ShipmentAIStatus(
        shipment_id=shipment_input.shipment.id,
        tracking_check_id=latest_check.id if latest_check else None,
        provider=provider,
        model_name=model_name,
        label=str(result.get("label") or "Unknown")[:255],
        severity=str(result.get("severity") or "gray")[:40],
        summary=str(result.get("summary") or "").strip(),
        reason=str(result.get("reason") or "").strip(),
        suggested_status=str(result.get("suggested_status") or "").strip(),
        suggested_status_note=str(result.get("suggested_status_note") or "").strip(),
        found_lm_awb=found_lm_awb,
        found_lm_courier=found_lm_courier,
        confidence=float(result.get("confidence") or 0) if str(result.get("confidence") or "").replace(".", "", 1).isdigit() else None,
        formatted_events_json=result.get("formatted_events_json") or json.dumps(shipment_input.events, default=str, ensure_ascii=False),
        raw_ai_json=result.get("raw_ai_json") or "{}",
    )
    db.add(status)
    db.flush()
    return status


def fetch_and_analyze_shipments(db: Session, shipments: list[Shipment]) -> tuple[int, int]:
    prepared: list[ShipmentTrackingInput] = []
    ai_payload: list[dict[str, Any]] = []

    for shipment in shipments:
        checks: list[TrackingCheck] = []
        all_events: list[dict[str, Any]] = []
        found_lm_awb = ""
        found_lm_courier = ""

        numbers = tracking_numbers_for_fetch(shipment)
        if numbers:
            for tn in numbers[:2]:
                courier = effective_courier(shipment, tn)
                fetch_result = fetch_tracking_for_number(courier, tn.tracking_number, tn.tracking_type)
                check = TrackingCheck(
                    shipment_id=shipment.id,
                    tracking_number_id=tn.id,
                    tracking_type=tn.tracking_type,
                    courier_name=courier,
                    tracking_number=tn.tracking_number,
                    fetch_status="success" if fetch_result.get("ok") else "failed",
                    error_message=fetch_result.get("error") or "",
                    latest_status_text=fetch_result.get("latest_status_text") or "",
                    latest_event_at=fetch_result.get("latest_event_at"),
                    formatted_events_json=fetch_result.get("formatted_events_json") or "[]",
                    raw_response=fetch_result.get("raw_response") or "",
                )
                db.add(check)
                db.flush()
                checks.append(check)
                fetch_events = fetch_result.get("events") or []
                for ev in fetch_events:
                    ev["_source_awb"] = tn.tracking_number
                    ev["_source_courier"] = courier
                    # Add formatted display time
                    try:
                        dt = datetime.fromisoformat(ev.get("event_time", "").replace("Z", "+00:00"))
                        ev["display_time"] = dt.strftime("%d %b, %H:%M")
                    except ValueError:
                        ev["display_time"] = ev.get("event_time", "")
                all_events.extend(fetch_events)
                lm_awb = fetch_result.get("found_lm_awb") or find_lm_awb(fetch_result.get("raw_response") or "")
                if lm_awb and not found_lm_awb:
                    found_lm_awb = lm_awb
                    found_lm_courier = str(fetch_result.get("found_lm_courier") or "").strip()

            # Do not suggest an LM AWB if it is already tracked for this shipment
            if found_lm_awb:
                existing_lms = {tn.tracking_number.strip() for tn in shipment.tracking_numbers if tn.tracking_type == "lm_awb" and tn.tracking_number}
                if found_lm_awb.strip() in existing_lms:
                    found_lm_awb = ""
                    found_lm_courier = ""
        else:
            check = TrackingCheck(
                shipment_id=shipment.id,
                fetch_status="skipped",
                error_message="No tracking number on shipment",
                formatted_events_json="[]",
            )
            db.add(check)
            db.flush()
            checks.append(check)

        if not all_events:
            all_events = fallback_events_from_shipment(shipment)

        # Deduplicate identical events across multiple couriers/AWBs
        dedup_events = []
        seen_events = {}
        for ev in all_events:
            time_key = ev.get("display_time") or ev.get("event_time") or ""
            status_key = " ".join((ev.get("status") or "").lower().split())
            loc_key = " ".join((ev.get("location") or "").lower().split())
            key = f"{time_key}|{status_key}|{loc_key}"

            if key in seen_events:
                existing_ev = seen_events[key]
                existing_couriers = [c.strip() for c in (existing_ev.get("_source_courier") or "").split(",") if c.strip()]
                new_courier = (ev.get("_source_courier") or "").strip()
                if new_courier and new_courier not in existing_couriers:
                    existing_couriers.append(new_courier)
                    existing_ev["_source_courier"] = ", ".join(existing_couriers)
            else:
                seen_events[key] = ev
                dedup_events.append(ev)

        all_events = sorted(dedup_events, key=lambda ev: ev.get("event_time") or "", reverse=True)
        shipment_input = ShipmentTrackingInput(shipment, checks, all_events, found_lm_awb, found_lm_courier)
        prepared.append(shipment_input)
        ai_payload.append(build_tracking_payload(shipment, all_events, found_lm_awb, found_lm_courier))

    results, provider, model_name = analyze_tracking_batch(ai_payload)
    result_by_id = {int(item.get("shipment_id") or 0): item for item in results if item.get("shipment_id")}

    for shipment_input in prepared:
        result = result_by_id.get(shipment_input.shipment.id, {})
        save_ai_status(db, shipment_input, result, provider, model_name)

    db.commit()
    return len(prepared), sum(1 for item in results if item.get("severity") in {"yellow", "red"})


@router.post("/fetch-active")
def fetch_active_shipments(db: Session = Depends(get_db)):
    shipments = (
        db.query(Shipment)
        .filter(Shipment.overall_status.notin_(list(TERMINAL_STATUSES)))
        .order_by(Shipment.booking_date.desc())
        .all()
    )
    total, attention = fetch_and_analyze_shipments(db, shipments)
    return RedirectResponse(url=f"/shipments?ai_checked={total}&ai_attention={attention}", status_code=303)


@router.post("/shipments/{shipment_id}/fetch")
def fetch_single_shipment(shipment_id: int, db: Session = Depends(get_db), next_url: str = Form("/shipments")):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if shipment:
        fetch_and_analyze_shipments(db, [shipment])
    return redirect_back(next_url)


@router.post("/shipments/{shipment_id}/apply-lm")
def apply_found_lm_awb(
    shipment_id: int,
    db: Session = Depends(get_db),
    lm_awb: str = Form(""),
    lm_courier: str = Form(""),
    ai_status_id: int = Form(0),
    next_url: str = Form("/shipments"),
):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    lm_awb = (lm_awb or "").strip()
    if shipment and lm_awb:
        tracking_changed = upsert_tracking(db, shipment.id, "lm_awb", lm_awb, lm_courier, False)
        if ai_status_id:
            status = db.query(ShipmentAIStatus).filter(ShipmentAIStatus.id == ai_status_id).first()
            if status:
                status.applied_at = now_ist()
                status.found_lm_awb = None
                status.found_lm_courier = None
                if not status.suggested_status and not status.suggested_status_note:
                    status.severity = "gray"
        db.commit()
        register_tracking_after_save(lm_courier, lm_awb, tracking_changed)
    return redirect_back(next_url)


@router.post("/shipments/{shipment_id}/apply-status-note")
def apply_ai_status_note(
    shipment_id: int,
    db: Session = Depends(get_db),
    suggested_status: str = Form(""),
    suggested_status_note: str = Form(""),
    ai_status_id: int = Form(0),
    next_url: str = Form("/shipments"),
):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if shipment:
        new_status = (suggested_status or shipment.overall_status or "booked").strip()
        new_note = (suggested_status_note or "").strip()
        if new_note:
            shipment.status_raw_text = new_note
        shipment.overall_status = new_status
        add_status_timeline_event(db, shipment, new_status, new_note, "ai_assisted")
        if ai_status_id:
            status = db.query(ShipmentAIStatus).filter(ShipmentAIStatus.id == ai_status_id).first()
            if status:
                status.applied_at = now_ist()
                status.suggested_status = None
                status.suggested_status_note = None
                if not status.found_lm_awb:
                    status.severity = "gray"
        db.commit()
    return redirect_back(next_url)


@router.post("/shipments/{shipment_id}/ignore")
def ignore_ai_status(
    shipment_id: int,
    db: Session = Depends(get_db),
    ai_status_id: int = Form(0),
    next_url: str = Form("/shipments"),
):
    if ai_status_id:
        status = db.query(ShipmentAIStatus).filter(ShipmentAIStatus.id == ai_status_id, ShipmentAIStatus.shipment_id == shipment_id).first()
        if status:
            status.ignored_at = now_ist()
            db.commit()
    return redirect_back(next_url)

