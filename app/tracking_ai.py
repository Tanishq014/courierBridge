from __future__ import annotations

from datetime import datetime
from typing import Any
import json
import os
import re
import urllib.error
import urllib.request


def parse_event_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def days_since(value: Any) -> int | None:
    parsed = parse_event_time(value)
    if not parsed:
        return None
    return (datetime.now(parsed.tzinfo) - parsed).days if parsed.tzinfo else (datetime.now() - parsed).days


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_severity(value: str) -> str:
    value = (value or "").strip().lower()
    return value if value in {"green", "yellow", "red", "gray"} else "gray"


def normalize_status(value: str) -> str:
    value = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    allowed = {
        "booked", "packed", "bagged", "in_scan", "bagging", "received", "in_transit", "customs",
        "hand_over_to_airline", "at_destination", "custom_clearance", "at_lm_partner", "out_for_delivery",
        "delivered", "undelivered", "rto", "return_damage", "exception", "unknown",
    }
    return value if value in allowed else ""


def rule_based_result(shipment: dict[str, Any]) -> dict[str, Any]:
    events = shipment.get("tracking_events") or []
    latest = events[0] if events else {}
    latest_text = str(latest.get("status") or shipment.get("current_status_note") or "").strip()
    latest_lower = latest_text.lower()
    age_days = shipment.get("age_days")
    promised_days = shipment.get("promised_days")
    latest_age = days_since(latest.get("event_time")) if latest else None
    current_status = shipment.get("current_app_status") or "booked"

    label = "Unknown"
    severity = "gray"
    suggested_status = ""
    summary = "No tracking data available yet."
    reason = "No carrier tracking events were available, so no safe judgement can be made."

    if latest_text:
        label = "OK"
        severity = "green"
        suggested_status = current_status
        summary = latest_text
        reason = "Latest tracking/status text does not show a clear exception."

    if any(word in latest_lower for word in ["deliver", "delivered"]):
        label = "Delivered"
        severity = "green"
        suggested_status = "delivered"
        summary = "Carrier/status text indicates delivery."
        reason = f"Latest event says: {latest_text}"
    elif any(word in latest_lower for word in ["custom", "duty", "clearance"]):
        label = "Customs/duty issue"
        severity = "red"
        suggested_status = "custom_clearance"
        summary = "Customs or duty wording was found."
        reason = f"Latest event says: {latest_text}"
    elif any(word in latest_lower for word in ["attempt", "unavailable", "not available", "undelivered"]):
        label = "Delivery attempted"
        severity = "red"
        suggested_status = "undelivered"
        summary = "Delivery attempt or receiver unavailable wording was found."
        reason = f"Latest event says: {latest_text}"
    elif any(word in latest_lower for word in ["rto", "return", "damage", "lost"]):
        label = "Carrier issue"
        severity = "red"
        suggested_status = "exception"
        summary = "Return/damage/lost wording was found."
        reason = f"Latest event says: {latest_text}"
    elif latest_age is not None and latest_age >= 3 and current_status not in {"delivered", "rto", "return_damage"}:
        label = "No movement"
        severity = "yellow"
        suggested_status = current_status
        summary = f"No fresh movement for {latest_age} days."
        reason = f"Latest event is {latest_age} days old: {latest_text or 'no text'}"
    elif promised_days and age_days is not None and age_days >= max(int(promised_days) - 1, 0) and current_status not in {"delivered", "rto", "return_damage"}:
        label = "Possible delay"
        severity = "yellow"
        suggested_status = current_status
        summary = "Shipment is close to or past the promised timeline."
        reason = f"Booking is day {age_days} of promised {promised_days} days. Latest event: {latest_text or 'not available'}"

    if shipment.get("found_lm_awb") and not shipment.get("has_lm_awb"):
        label = "LM AWB found"
        severity = "yellow" if severity == "green" else severity
        summary = f"Found possible LM AWB: {shipment.get('found_lm_awb')}"
        reason = (reason + " Found last-mile AWB in carrier response.").strip()

    suggested_note = summary
    if latest_text and latest_text not in suggested_note:
        suggested_note = f"{summary} Latest: {latest_text}"

    return {
        "shipment_id": shipment.get("shipment_id"),
        "label": label,
        "severity": severity,
        "summary": summary,
        "reason": reason,
        "suggested_status": normalize_status(suggested_status) or current_status,
        "suggested_status_note": suggested_note,
        "found_lm_awb": shipment.get("found_lm_awb") or "",
        "found_lm_courier": shipment.get("found_lm_courier") or "",
        "confidence": 0.65 if severity != "gray" else 0.25,
        "formatted_events_json": json.dumps(events, ensure_ascii=False, default=str),
        "raw_ai_json": compact_json({"source": "rules"}),
    }


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def call_gemini(shipments: list[dict[str, Any]], api_key: str, model_name: str) -> list[dict[str, Any]]:
    prompt = """
You are an operations assistant for an international courier ledger. Analyze each shipment's tracking events and app metadata.
Return strict JSON only in this shape:
{"results":[{"shipment_id":number,"label":string,"severity":"green|yellow|red|gray","summary":string,"reason":string,"suggested_status":string,"suggested_status_note":string,"found_lm_awb":string,"found_lm_courier":string,"confidence":number}]}

Important rules:
- "label": A short 2-3 word phrase summarizing the status (e.g. "On track", "Delayed", "Needs action", "Delivered").
- Be conservative. If unsure, use gray/Unknown or yellow/Needs attention, not a confident wrong answer.
- Do not invent tracking numbers or dates.
- Use operational judgement, not just the latest headline status.
- Consider promised_days, age_days, stale movement, customs/duty, delivery attempts, receiver unavailable, RTO/return/damage/lost, destination scans, and whether delivery seems close or delayed.
- In your `summary` and `reason`, explicitly reference the individual couriers and AWBs (e.g. "Main AWB (FedEx) arrived, LM AWB (Purolator) out for delivery").
- Use suggested_status only from: booked, packed, bagged, in_scan, bagging, received, in_transit, customs, hand_over_to_airline, at_destination, custom_clearance, at_lm_partner, out_for_delivery, delivered, undelivered, rto, return_damage, exception, unknown.
- Keep suggested_status_note short enough to fit in a ledger row. If the status is 'delivered', the note MUST contain the delivery date/time strictly formatted as DD.MM.YY (e.g. "Delivered on 29.06.26") instead of just the location.
- "found_lm_awb": Do not invent a tracking number here. Only populate this if you explicitly detect a NEW Last-Mile tracking number in the tracking events that is DIFFERENT from the main AWB.
""".strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    body = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt + "\n\nShipments JSON:\n" + json.dumps({"shipments": shipments}, ensure_ascii=False, default=str)}],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    text = ""
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            text += part.get("text") or ""
    parsed = extract_json_object(text)
    if not parsed or not isinstance(parsed.get("results"), list):
        raise RuntimeError("Gemini did not return the expected JSON results shape")
    return parsed["results"]


def clean_ai_result(item: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    result = {**fallback, **(item or {})}
    result["shipment_id"] = fallback.get("shipment_id")
    result["severity"] = normalize_severity(result.get("severity") or fallback.get("severity"))
    result["suggested_status"] = normalize_status(result.get("suggested_status") or "") or fallback.get("suggested_status") or "unknown"
    try:
        result["confidence"] = max(0, min(1, float(result.get("confidence") or 0)))
    except Exception:
        result["confidence"] = fallback.get("confidence") or 0
    return result


def analyze_tracking_batch(shipments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    fallbacks = [rule_based_result(shipment) for shipment in shipments]
    api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    model_name = (os.environ.get("GEMINI_MODEL") or "gemini-3.1-flash-lite").strip()
    if not api_key or not shipments:
        return fallbacks, "rules", ""
    try:
        ai_results = call_gemini(shipments, api_key, model_name)
        ai_by_id = {int(item.get("shipment_id") or 0): item for item in ai_results if item.get("shipment_id")}
        merged = []
        for fallback in fallbacks:
            merged.append(clean_ai_result(ai_by_id.get(int(fallback.get("shipment_id") or 0), {}), fallback))
        for item in merged:
            item["raw_ai_json"] = compact_json({"source": "gemini", "result": item})
        return merged, "gemini", model_name
    except Exception as exc:
        results = []
        for fallback in fallbacks:
            fallback["reason"] = f"Gemini failed, used rules fallback. {fallback.get('reason') or ''}".strip()
            fallback["raw_ai_json"] = compact_json({"source": "rules_fallback", "gemini_error": str(exc)})
            results.append(fallback)
        return results, "rules_fallback", model_name
