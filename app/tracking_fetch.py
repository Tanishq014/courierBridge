from __future__ import annotations

from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from typing import Any
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from app.models import Shipment
from app.tracking_links import normalize_courier_name

SEVENTEEN_TRACK_REGISTER_ENDPOINT = "https://api.17track.net/track/v2.2/register"
SEVENTEEN_TRACK_GETINFO_ENDPOINT = "https://api.17track.net/track/v2.2/gettrackinfo"
SEVENTEEN_TRACK_DEFAULT_BRAND_CODES = {
    "fedex": 101393,
    "fedexgroup": 101393,
    "ups": 100398,
    "dpd": 100010,
    "dhl": 100001,
    "dtdc": 100069,
    "purolator": 100042,
}
SEVENTEEN_TRACK_BRAND_ALIASES = {
    "fedex": "fedex",
    "fedexgroup": "fedex",
    "fx": "fedex",
    "ups": "ups",
    "upssaver": "ups",
    "upsprim": "ups",
    "upsprime": "ups",
    "unitedparcelservice": "ups",
    "upsmailinnovations": "ups",
    "nzpost": "nzpost",
    "newzealandpost": "nzpost",
    "nz": "nzpost",
    "dhl": "dhl",
    "dhlexpress": "dhl",
    "dpduk": "dpd",
    "dtdc": "dtdc",
    "mydtdc": "dtdc",
    "purolator": "purolator",
}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = " ".join((data or "").split())
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return " ".join(self.parts)


def strip_html(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    return unescape(parser.text()).strip()


def parse_event_datetime(value: str) -> datetime | None:
    value = " ".join((value or "").replace("&nbsp;", " ").split())
    formats = [
        "%d/%m/%Y %I:%M %p",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %I:%M %p",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d %b %y %H:%M",
        "%d %b %Y %H:%M",
        "%d/%m/%y %I:%M %p",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def is_plausible_event_datetime(parsed: datetime | None) -> bool:
    if not parsed:
        return False
    now = datetime.now()
    return 2000 <= parsed.year <= now.year + 1 and parsed <= now + timedelta(days=366)


def event_to_dict(event_time: str, status: str, location: str = "", source: str = "carrier") -> dict[str, Any]:
    event_time = (event_time or "").strip()
    parsed = parse_event_datetime(event_time)
    normalized_time = parsed.isoformat() if is_plausible_event_datetime(parsed) else ("" if parsed else event_time)
    event = {
        "event_time": normalized_time,
        "status": " ".join((status or "").split()),
        "location": " ".join((location or "").split()),
        "source": source,
    }
    if parsed and not is_plausible_event_datetime(parsed):
        event["raw_event_time"] = event_time
    return event


def latest_event_at(events: list[dict[str, Any]]) -> datetime | None:
    parsed_events = []
    for event in events:
        value = event.get("event_time") or ""
        if isinstance(value, datetime):
            parsed_events.append(value.replace(tzinfo=None))
            continue
        try:
            parsed_events.append(datetime.fromisoformat(str(value)).replace(tzinfo=None))
        except ValueError:
            parsed = parse_event_datetime(str(value))
            if parsed:
                parsed_events.append(parsed.replace(tzinfo=None))
    return max(parsed_events) if parsed_events else None


def clean_event_list(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for event in events:
        status = " ".join(str(event.get("status") or "").split())
        location = " ".join(str(event.get("location") or "").split())
        event_time = " ".join(str(event.get("event_time") or "").split())
        source = str(event.get("source") or "")
        if not status and not location:
            continue
        key = (event_time, status.lower(), location.lower(), source)
        if key in seen:
            continue
        seen.add(key)
        updated = dict(event)
        updated["status"] = status
        updated["location"] = location
        updated["event_time"] = event_time
        cleaned.append(updated)
    return cleaned



def extract_17track_local_number(payload: dict[str, Any]) -> str:
    for row in payload.get("shipments") or []:
        misc = ((row.get("shipment") or {}).get("misc_info") or {})
        local_number = str(misc.get("local_number") or "").strip()
        if local_number:
            return local_number.upper()
    for row in ((payload.get("data") or {}).get("accepted") or []):
        track_info = row.get("track_info") or row.get("tracking") or {}
        misc = track_info.get("misc_info") or {}
        local_number = str(misc.get("local_number") or "").strip()
        if local_number:
            return local_number.upper()
    return ""


def find_lm_awb(raw: str) -> str:
    try:
        parsed = json.loads(raw or "{}")
        local_number = extract_17track_local_number(parsed)
        if local_number:
            return local_number
    except Exception:
        pass
    text = strip_html(raw or "")
    patterns = [
        r"Fwd\s*No\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{5,})",
        r"Forwarder\s*No\.?\s*([A-Z0-9][A-Z0-9\-]{5,})",
        r"Forwarder\s*No\.?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{5,})",
        r"\b(1Z[A-Z0-9]{10,})\b",
        r"\b(JFS[A-Z0-9]{6,})\b",
        r"\b(E[A-Z]\d{9}[A-Z]{2})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().upper()
    return ""


def find_lm_courier(raw: str) -> str:
    text = strip_html(raw or "").lower()
    raw_lower = (raw or "").lower()
    if "purolator" in text:
        return "Purolator"
    if "nz_economy" in raw_lower or "new zealand" in text:
        return "NZ Post"
    if "ups" in text or "united parcel" in text:
        return "UPS"
    if "fedex" in text:
        return "FedEx"
    if "dhl" in text:
        return "DHL"
    if "aramex" in text or "aramax" in text:
        return "Aramex"
    if "dpd" in text:
        return "DPD"
    return ""


def parse_atlantic_events(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    activity_table_match = re.search(
        r"<table[^>]*class=\"[^\"]*table-striped[^\"]*\"[^>]*>(.*?)</table>",
        raw or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    section = activity_table_match.group(1) if activity_table_match else (raw or "")
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.IGNORECASE | re.DOTALL)
        if len(cells) < 3:
            continue
        first = strip_html(cells[0])
        second = strip_html(cells[1])
        third = strip_html(cells[2])
        if not first or first.lower() in {"date/time", "consignee name"}:
            continue
        if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", first):
            events.append(event_to_dict(first, second, third, "atlantic"))
    return clean_event_list(events)


def extract_overseas_history_table(raw: str) -> str:
    match = re.search(
        r"<table[^>]*id=\"ContentPlaceHolder1_DataList1_grdstate_0\"[^>]*>(.*?)</table>",
        raw or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else (raw or "")


def parse_overseas_events(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    section = extract_overseas_history_table(raw)
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.IGNORECASE | re.DOTALL)
        if len(cells) < 4:
            continue
        date_text = strip_html(cells[0])
        time_text = strip_html(cells[1])
        location = strip_html(cells[2])
        status = strip_html(cells[3])
        if not date_text or "activity" in date_text.lower() or not status:
            continue
        if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", date_text):
            events.append(event_to_dict(f"{date_text} {time_text}", status, location, "overseas"))
    return clean_event_list(events)


def fetch_atlantic(awb: str) -> dict[str, Any]:
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
            "User-Agent": "Mozilla/5.0 CourierBridge",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        raw = response.read().decode("utf-8", errors="replace")
    events = parse_atlantic_events(raw)
    result = normalize_fetch_result(True, events, raw, "atlantic")
    found_lm_awb = find_lm_awb(raw)
    if found_lm_awb:
        result["found_lm_awb"] = found_lm_awb
        found_lm_courier = find_lm_courier(raw)
        if found_lm_courier:
            result["found_lm_courier"] = found_lm_courier
    return result


def parse_aramex_events(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    trs = re.findall(r'<tr[^>]*>(.*?)</tr>', raw, flags=re.IGNORECASE | re.DOTALL)
    for tr in trs:
        date_match = re.search(r'<div[^>]*class="[^"]*date-time[^"]*"[^>]*>(.*?)</div>', tr, flags=re.IGNORECASE | re.DOTALL)
        activity_match = re.search(r'<td[^>]*class="[^"]*activity[^"]*"[^>]*>(.*?)</td>', tr, flags=re.IGNORECASE | re.DOTALL)
        location_match = re.search(r'<div[^>]*class="[^"]*addr[^"]*"[^>]*>(.*?)</div>', tr, flags=re.IGNORECASE | re.DOTALL)
        if activity_match:
            dt = re.sub(r'\s+', ' ', strip_html(date_match.group(1))) if date_match else ''
            act = re.sub(r'\s+', ' ', strip_html(activity_match.group(1)))
            loc = re.sub(r'\s+', ' ', strip_html(location_match.group(1))) if location_match else ''
            events.append(event_to_dict(dt, act, loc, "aramex"))

    if events:
        return events[:20]

    cards = re.findall(r'<a[^>]*class="[^"]*shipment-card[^"]*"[^>]*>(.*?)</a>', raw or "", flags=re.IGNORECASE | re.DOTALL)
    if not cards:
        cards = [raw or ""]
    for card in cards:
        desc_match = re.search(r'class="shipment-update-descp"[^>]*>(.*?)</p>', card, flags=re.IGNORECASE | re.DOTALL)
        time_match = re.search(r'class="shipment-update-datetime"[^>]*>(.*?)</span>', card, flags=re.IGNORECASE | re.DOTALL)
        status = strip_html(desc_match.group(1)) if desc_match else ""
        event_time = strip_html(time_match.group(1)) if time_match else ""
        if status:
            events.append(event_to_dict(event_time, status, "", "aramex"))
        for point in re.findall(r'<div[^>]*shipment-progess-point[^>]*>(.*?)</div>\s*</div>', card, flags=re.IGNORECASE | re.DOTALL):
            point_text = strip_html(point)
            if point_text and point_text.lower() not in {"origin", "destination"}:
                events.append(event_to_dict("", point_text, "", "aramex"))
    if not events:
        text_value = strip_html(raw or "")
        no_result = "No results found" in text_value
        if no_result:
            events.append(event_to_dict("", "No results found", "", "aramex"))
    return events[:20]


def fetch_aramex(awb: str) -> dict[str, Any]:
    url = "https://www.aramex.com/ae/en/track/results?" + urllib.parse.urlencode({"ShipmentNumber": awb, "source": "aramex"})
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": "country_code=IN; culture=en; country=AE; ShowConfirmLocation=False",
            "Referer": "https://www.aramex.com/ae/en/track/results",
            "User-Agent": "Mozilla/5.0 CourierBridge",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")

    details_link_match = re.search(r'<a[^>]*class="[^"]*shipment-card[^"]*"[^>]*href="(/track/details[^"]+)"', raw)
    if details_link_match:
        details_url = "https://www.aramex.com" + unescape(details_link_match.group(1))
        details_request = urllib.request.Request(
            details_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": "country_code=IN; culture=en; country=AE; ShowConfirmLocation=False",
                "Referer": url,
                "User-Agent": "Mozilla/5.0 CourierBridge",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(details_request, timeout=30) as d_response:
                details_raw = d_response.read().decode("utf-8", errors="replace")
                raw = details_raw
        except Exception:
            pass

    events = parse_aramex_events(raw)
    return normalize_fetch_result(True, events, raw, "aramex")



def parse_quickship_response(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    records = payload.get("data") or []
    if not records:
        return [], "", ""
    first = records[0] or {}
    events: list[dict[str, Any]] = []
    for event in first.get("events") or []:
        status = event.get("statusRemark") or event.get("subStatusRemark") or event.get("statusCode") or ""
        events.append(event_to_dict(
            str(event.get("eventDate") or ""),
            str(status),
            str(event.get("location") or ""),
            "quickship",
        ))
    latest_status = str(first.get("status") or "")
    found_lm_awb = str(first.get("lmAwb") or "").strip().upper()
    found_lm_courier = str(first.get("carrierName") or "").strip()
    return events, latest_status, found_lm_awb, found_lm_courier


def fetch_quickship(awb: str) -> dict[str, Any]:
    endpoint = os.environ.get("QUICKSHIP_TRACK_ENDPOINT") or "https://qsapi.quickshipnow.com/public/track"
    query = urllib.parse.urlencode({"awb": awb, "origin": "track.quickshipnow.com"})
    separator = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{separator}{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,en-IN;q=0.8",
            "Origin": "https://track.quickshipnow.com",
            "Referer": "https://track.quickshipnow.com/",
            "User-Agent": "Mozilla/5.0 CourierBridge",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")
    payload = json.loads(raw)
    events, latest_status, found_lm_awb, found_lm_courier = parse_quickship_response(payload)
    result = normalize_fetch_result(bool(payload.get("success")), events, raw, "quickship", found_lm_awb=found_lm_awb, found_lm_courier=found_lm_courier)
    if latest_status:
        result["latest_status_text"] = latest_status
    if found_lm_awb:
        result["found_lm_awb"] = found_lm_awb
    return result



def hidden_value(page: str, field_id: str) -> str:
    pattern = rf'id="{re.escape(field_id)}" value="([^"]*)"'
    match = re.search(pattern, page)
    return unescape(match.group(1)) if match else ""


def extract_overseas_tracking_section(raw: str) -> str:
    start = raw.find('<table id="ContentPlaceHolder1_DataList1"')
    if start < 0:
        start = raw.find("OVERSEAS TRACKING SYSTEM")
    if start < 0:
        return raw
    wrapper_start = raw.rfind("<div", 0, start)
    if wrapper_start >= 0:
        start = wrapper_start
    end = raw.find("<!-- container -->", start)
    if end < 0:
        end = raw.find('<section class="margin-bottom">', start)
    if end < 0:
        end = raw.find('<aside id="footer-widgets"', start)
    if end < 0:
        end = len(raw)
    return raw[start:end]


def fetch_overseas(awb: str) -> dict[str, Any]:
    url = "https://track.overseaslogistic.com/tracking.aspx"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 CourierBridge",
    }
    get_request = urllib.request.Request(url, headers=headers, method="GET")
    with opener.open(get_request, timeout=25) as response:
        initial_html = response.read().decode("utf-8", errors="replace")
    tokens = {
        "__VIEWSTATE": hidden_value(initial_html, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": hidden_value(initial_html, "__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": hidden_value(initial_html, "__EVENTVALIDATION"),
    }
    missing = [key for key, value in tokens.items() if not value]
    if missing:
        raise RuntimeError(f"Missing hidden fields: {', '.join(missing)}")
    form = {
        "__VIEWSTATE": tokens["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": tokens["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION": tokens["__EVENTVALIDATION"],
        "ctl00$ContentPlaceHolder1$text": awb,
        "ctl00$ContentPlaceHolder1$Button1": "Track",
    }
    body = urllib.parse.urlencode(form).encode("utf-8")
    post_request = urllib.request.Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://track.overseaslogistic.com", "Referer": url},
        method="POST",
    )
    with opener.open(post_request, timeout=25) as response:
        raw = response.read().decode("utf-8", errors="replace")
    useful = extract_overseas_tracking_section(raw)
    events = parse_overseas_events(useful)

    lm_awb = ""
    lm_courier = ""

    # Extract Forwarder No. and Forwarder Name from the HTML table
    stripped = strip_html(useful)
    # Match both "Forwarder No. [AWB]" and "Forwarder [COURIER]" in the text
    forwarder_match = re.search(r"Forwarder\s*No\.?\s*([A-Z0-9\-]{5,}).*?Forwarder\s+([A-Za-z0-9]+)", stripped, flags=re.IGNORECASE)

    if forwarder_match:
        lm_awb = forwarder_match.group(1).strip()
        lm_courier = forwarder_match.group(2).strip()
    else:
        # Fallback if only the number exists
        awb_only_match = re.search(r"Forwarder\s*No\.?\s*([A-Z0-9\-]{5,})", stripped, flags=re.IGNORECASE)
        if awb_only_match:
            lm_awb = awb_only_match.group(1).strip()

    useful_upper = useful.upper()
    if lm_courier.lower() == "self":
        if "NZ_ECONOMY" in useful_upper or "NEW ZEALAND" in useful_upper:
            lm_courier = "NZ Post"
        else:
            lm_courier = ""

    # If the regex above fails, we can also just rely on find_lm_awb as fallback
    if not lm_awb:
        lm_awb = find_lm_awb(useful) or ""

    return normalize_fetch_result(True, events, raw, "overseas", found_lm_awb=lm_awb, found_lm_courier=lm_courier)



def iso_datetime_value(value: str) -> str:
    value = " ".join((value or "").split())
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def parse_nzpost_response(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    events: list[dict[str, Any]] = []
    latest_status = ""
    for result in payload.get("results") or []:
        for event in result.get("tracking_events") or []:
            status = event.get("status") or event.get("description") or ""
            if status and not latest_status:
                latest_status = str(status)
            location = event.get("depot_name") or event.get("run_name") or event.get("location") or ""
            events.append({
                "event_time": iso_datetime_value(str(event.get("date_time") or "")),
                "status": " ".join(str(status).split()),
                "location": " ".join(str(location).split()),
                "source": "nzpost",
                "description": " ".join(str(event.get("description") or "").split()),
            })
    events = [event for event in events if event.get("status") or event.get("location")]
    events.reverse()
    if events:
        latest_status = events[0].get("status") or latest_status
    return events, latest_status


def fetch_nz_post(awb: str) -> dict[str, Any]:
    endpoint = os.environ.get("NZPOST_TRACK_ENDPOINT") or "https://tools.nzpost.co.nz/tracking/api/parceltrack/parcels"
    query = urllib.parse.urlencode({"tracking_reference": awb})
    separator = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{separator}{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,en-IN;q=0.8",
            "Connection": "keep-alive",
            "Origin": "https://www.nzpost.co.nz",
            "Referer": f"https://www.nzpost.co.nz/tools/tracking?trackid={urllib.parse.quote_plus(awb)}",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "pragma": "no-cache"
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")
    payload = json.loads(raw)
    events, latest_status = parse_nzpost_response(payload)
    result = normalize_fetch_result(True, events, raw, "nzpost")
    if latest_status:
        result["latest_status_text"] = latest_status
    return result


def load_17track_brand_codes() -> dict[str, int]:
    codes = dict(SEVENTEEN_TRACK_DEFAULT_BRAND_CODES)
    raw_json = (os.environ.get("SEVENTEEN_TRACK_CARRIER_CODES") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            for name, value in parsed.items():
                if name and value:
                    codes[normalize_courier_name(name)] = int(value)
        except Exception:
            pass
    env_map = {
        "ups": os.environ.get("SEVENTEEN_TRACK_FC_UPS"),
        "dpd": os.environ.get("SEVENTEEN_TRACK_FC_DPD"),
        "dhl": os.environ.get("SEVENTEEN_TRACK_FC_DHL"),
        "nzpost": os.environ.get("SEVENTEEN_TRACK_FC_NZPOST"),
        "fedex": os.environ.get("SEVENTEEN_TRACK_FC_FEDEX"),
    }
    for key, value in env_map.items():
        if value:
            try:
                codes[key] = int(value)
            except ValueError:
                pass
    return codes


def seventeen_track_brand_key(courier_key: str) -> str:
    return SEVENTEEN_TRACK_BRAND_ALIASES.get(courier_key, courier_key)


def seventeen_track_fc_for_courier(courier_key: str) -> int | None:
    brand_key = seventeen_track_brand_key(courier_key)
    return load_17track_brand_codes().get(brand_key)


def parse_17track_event(event: dict[str, Any]) -> dict[str, Any]:
    raw_time = event.get("time_iso") or event.get("time_utc") or ""
    if not raw_time:
        time_raw = event.get("time_raw") or {}
        raw_date = time_raw.get("date") or ""
        raw_clock = time_raw.get("time") or ""
        raw_time = " ".join(part for part in [raw_date, raw_clock] if part)
    return {
        "event_time": str(raw_time),
        "status": " ".join(str(event.get("description") or event.get("stage") or event.get("sub_status") or "").split()),
        "location": " ".join(str(event.get("location") or "").split()),
        "source": "17track",
        "stage": event.get("stage") or "",
        "sub_status": event.get("sub_status") or "",
    }




def official_17track_api_key() -> str:
    return (os.environ.get("SEVENTEEN_TRACK_API_KEY") or os.environ.get("SEVENTEEN_TRACK_17TOKEN") or "").strip()


def official_17track_headers() -> dict[str, str]:
    api_key = official_17track_api_key()
    if not api_key:
        raise RuntimeError("Set SEVENTEEN_TRACK_API_KEY in the environment")
    return {
        "17token": api_key,
        "Content-Type": "application/json",
    }


def official_17track_payload(awb: str, courier_key: str) -> tuple[list[dict[str, Any]], str, int | None]:
    brand_key = seventeen_track_brand_key(courier_key)
    carrier = seventeen_track_fc_for_courier(courier_key)
    if brand_key not in {"fedex", "ups", "dhl", "dpd"} or carrier is None:
        return [], brand_key, carrier
    return [{"number": awb, "carrier": carrier}], brand_key, carrier


def post_17track_official(endpoint: str, rows: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(rows).encode("utf-8"),
        headers=official_17track_headers(),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw), raw


def official_17track_rejected(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    rejected = data.get("rejected") or []
    return rejected if isinstance(rejected, list) else []


def official_17track_accepted(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    accepted = data.get("accepted") or []
    return accepted if isinstance(accepted, list) else []


def rejection_text(rejected: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rejected:
        parts.append(json.dumps(row, ensure_ascii=False, default=str))
    return " ".join(parts).lower()


def official_17track_is_not_registered(payload: dict[str, Any]) -> bool:
    text = rejection_text(official_17track_rejected(payload))
    return "not register" in text or "unregistered" in text or "not exist" in text


def official_17track_already_registered(payload: dict[str, Any]) -> bool:
    text = rejection_text(official_17track_rejected(payload))
    return "already" in text and "register" in text


def parse_17track_track_info(track_info: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    tracking = track_info.get("tracking") or {}
    providers = tracking.get("providers") or []
    events: list[dict[str, Any]] = []
    for provider in providers:
        for event in provider.get("events") or []:
            parsed = parse_17track_event(event)
            if parsed.get("status") or parsed.get("location"):
                events.append(parsed)
    if not events and track_info.get("latest_event"):
        events.append(parse_17track_event(track_info.get("latest_event") or {}))
    latest_status = ""
    latest_event = track_info.get("latest_event") or {}
    latest_status_obj = track_info.get("latest_status") or {}
    if latest_event.get("description"):
        latest_status = str(latest_event.get("description"))
    elif latest_status_obj.get("status"):
        latest_status = str(latest_status_obj.get("status"))
    misc = track_info.get("misc_info") or {}
    local_number = str(misc.get("local_number") or "").strip().upper()
    return events, latest_status, local_number


def parse_17track_official_response(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    all_events: list[dict[str, Any]] = []
    latest_status = ""
    found_lm_awb = ""
    for row in official_17track_accepted(payload):
        track_info = row.get("track_info") or row.get("tracking") or {}
        if not isinstance(track_info, dict):
            continue
        events, row_latest, local_number = parse_17track_track_info(track_info)
        all_events.extend(events)
        if row_latest and not latest_status:
            latest_status = row_latest
        if local_number and not found_lm_awb:
            found_lm_awb = local_number
    return clean_event_list(all_events), latest_status, found_lm_awb


def register_17track_official(awb: str, courier_key: str) -> dict[str, Any]:
    rows, brand_key, carrier = official_17track_payload(awb, courier_key)
    if not rows:
        return {"ok": False, "skipped": True, "brand": brand_key, "carrier": carrier, "error": "17TRACK official carrier is not configured"}
    try:
        payload, raw = post_17track_official(os.environ.get("SEVENTEEN_TRACK_REGISTER_ENDPOINT") or SEVENTEEN_TRACK_REGISTER_ENDPOINT, rows)
    except Exception as exc:
        return {"ok": False, "brand": brand_key, "carrier": carrier, "error": str(exc)}
    accepted = official_17track_accepted(payload)
    rejected = official_17track_rejected(payload)
    already = official_17track_already_registered(payload)
    return {
        "ok": bool(accepted) or already,
        "registered": bool(accepted),
        "already_registered": already,
        "brand": brand_key,
        "carrier": carrier,
        "raw_response": raw[:60000],
        "error": "" if bool(accepted) or already else rejection_text(rejected) or str(payload.get("code") or payload.get("message") or "Registration failed"),
    }


def register_tracking_if_supported(courier: str, tracking_number: str) -> dict[str, Any]:
    awb = (tracking_number or "").strip()
    courier_key = normalize_courier_name(courier)
    if not awb:
        return {"ok": False, "skipped": True, "error": "Missing tracking number"}
    rows, brand_key, carrier = official_17track_payload(awb, courier_key)
    if not rows:
        return {
            "ok": False,
            "skipped": True,
            "brand": brand_key,
            "carrier": carrier,
            "error": "Carrier not registered through 17TRACK official API",
        }

    try:
        payload, raw = post_17track_official(
            os.environ.get("SEVENTEEN_TRACK_GETINFO_ENDPOINT") or SEVENTEEN_TRACK_GETINFO_ENDPOINT,
            rows,
        )
    except Exception as exc:
        return {"ok": False, "brand": brand_key, "carrier": carrier, "error": str(exc)}

    if official_17track_accepted(payload):
        return {
            "ok": True,
            "skipped": True,
            "already_registered": True,
            "brand": brand_key,
            "carrier": carrier,
            "raw_response": raw[:60000],
        }

    if official_17track_is_not_registered(payload):
        return register_17track_official(awb, courier_key)

    return {
        "ok": False,
        "brand": brand_key,
        "carrier": carrier,
        "raw_response": raw[:60000],
        "error": rejection_text(official_17track_rejected(payload)) or str(payload.get("message") or payload.get("code") or "17TRACK did not confirm registration state"),
    }


def fetch_17track_official(awb: str, courier_key: str) -> dict[str, Any]:
    rows, brand_key, carrier = official_17track_payload(awb, courier_key)
    if not rows:
        return normalize_fetch_result(False, [], "", f"17track:{brand_key}", f"17TRACK official carrier is not configured for {brand_key}")
    try:
        payload, raw = post_17track_official(os.environ.get("SEVENTEEN_TRACK_GETINFO_ENDPOINT") or SEVENTEEN_TRACK_GETINFO_ENDPOINT, rows)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return normalize_fetch_result(False, [], raw, f"17track:{brand_key}", f"HTTP {exc.code}")

    if official_17track_accepted(payload):
        events, latest_status, found_lm_awb = parse_17track_official_response(payload)
        result = normalize_fetch_result(True, events, raw, f"17track:{brand_key}", found_lm_awb=found_lm_awb)
        if latest_status and not result.get("latest_status_text"):
            result["latest_status_text"] = latest_status
        return result

    if official_17track_is_not_registered(payload):
        register_result = register_17track_official(awb, courier_key)
        
        if register_result.get("ok"):
            for _ in range(3):
                time.sleep(5)
                try:
                    retry_payload, retry_raw = post_17track_official(os.environ.get("SEVENTEEN_TRACK_GETINFO_ENDPOINT") or SEVENTEEN_TRACK_GETINFO_ENDPOINT, rows)
                    if official_17track_accepted(retry_payload):
                        retry_events, retry_latest_status, retry_found_lm_awb = parse_17track_official_response(retry_payload)
                        if retry_events:
                            retry_result = normalize_fetch_result(True, retry_events, retry_raw, f"17track:{brand_key}", found_lm_awb=retry_found_lm_awb)
                            if retry_latest_status and not retry_result.get("latest_status_text"):
                                retry_result["latest_status_text"] = retry_latest_status
                            return retry_result
                except Exception:
                    pass

        status_text = "Tracking initiated on 17TRACK. Check again in a few minutes."
        events = [{
            "event_time": datetime.now().isoformat(timespec="seconds"),
            "status": status_text,
            "location": "17TRACK",
            "source": "17track",
        }]
        result = normalize_fetch_result(True, events, raw, f"17track:{brand_key}")
        result["latest_status_text"] = status_text
        if not register_result.get("ok"):
            result["error"] = f"17TRACK registration attempted but failed: {register_result.get('error') or 'unknown error'}"
        return result

    error_text = rejection_text(official_17track_rejected(payload)) or str(payload.get("message") or payload.get("code") or "17TRACK returned no tracking info")
    return normalize_fetch_result(False, [], raw, f"17track:{brand_key}", error_text)


# Backwards-compatible name for existing call sites.
def fetch_17track_browser(awb: str, courier_key: str) -> dict[str, Any]:
    return fetch_17track_official(awb, courier_key)


def latest_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    latest: tuple[datetime, dict[str, Any]] | None = None
    for event in events:
        value = event.get("event_time") or ""
        parsed = None
        if isinstance(value, datetime):
            parsed = value.replace(tzinfo=None)
        else:
            try:
                parsed = datetime.fromisoformat(str(value)).replace(tzinfo=None)
            except ValueError:
                raw_parsed = parse_event_datetime(str(value))
                if raw_parsed:
                    parsed = raw_parsed.replace(tzinfo=None)
        if parsed and is_plausible_event_datetime(parsed):
            if latest is None or parsed > latest[0]:
                latest = (parsed, event)
    if latest:
        return latest[1]
    return events[0] if events else {}


def normalize_fetch_result(ok: bool, events: list[dict[str, Any]], raw: str, source: str, error: str = "", found_lm_awb: str = "", found_lm_courier: str = "") -> dict[str, Any]:
    latest = latest_event(events)
    latest_at = latest_event_at(events)
    return {
        "ok": ok,
        "source": source,
        "error": error,
        "events": events,
        "latest_status_text": latest.get("status") or "",
        "latest_event_at": latest_at,
        "formatted_events_json": json.dumps(events, default=str, ensure_ascii=False),
        "raw_response": raw[:60000] if raw else "",
        "found_lm_awb": found_lm_awb,
        "found_lm_courier": found_lm_courier,
    }


def fetch_purolator(awb: str) -> dict[str, Any]:
    url = "https://track.purolator.com/tracking-ext/v1/search"
    headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://www.purolator.com",
        "referer": "https://www.purolator.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
        "x-api-key": "TdIVdHURM65yalzbkDenz5jMWlovpP7L2VrK9QMu"
    }
    data = {
        "search": [{"trackingId": awb, "pod": True, "sequenceId": 1, "eventSortOrder": "d"}],
        "language": "en"
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")

    payload = json.loads(raw)
    events: list[dict[str, Any]] = []

    shipments = payload.get("shipment") or []
    latest_status = ""
    for shipment in shipments:
        status_obj = shipment.get("status") or {}
        if not latest_status:
            latest_status = status_obj.get("description") or ""
        packages = shipment.get("package") or []
        for package in packages:
            for event in package.get("events") or []:
                date_str = str(event.get("dateTime") or "")
                description = str(event.get("description") or "")
                location_obj = event.get("location") or {}
                location_parts = []
                if location_obj.get("city"):
                    location_parts.append(location_obj["city"])
                if location_obj.get("provinceState"):
                    location_parts.append(location_obj["provinceState"])
                location_str = ", ".join(location_parts)
                events.append(event_to_dict(date_str, description, location_str, "purolator"))

    result = normalize_fetch_result(True, events, raw, "purolator")
    if latest_status:
        result["latest_status_text"] = latest_status
    return result


def fetch_mawwl(awb: str) -> dict[str, Any]:
    url = f"https://admin.mawwl.in/api/tracking_api/get_tracking_data?tracking_no={awb}&customer_code=superadmin&company=ma-logsitics-delhi&api_company_id=9"
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://www.mawwl.in",
        "Referer": "https://www.mawwl.in/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
    }
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="replace")

    try:
        payload = json.loads(raw)
    except Exception:
        payload = []

    events: list[dict[str, Any]] = []
    latest_status = ""
    found_lm_awb = ""

    if payload and isinstance(payload, list) and len(payload) > 0:
        data = payload[0]

        # Get the latest status from docket_info if possible
        docket_info = data.get("docket_info") or []
        for row in docket_info:
            if len(row) >= 2 and row[0] == "Status":
                latest_status = str(row[1])
                break

        docket_events = data.get("docket_events") or []
        for event in docket_events:
            date_str = str(event.get("event_at") or event.get("event_date_time") or "")
            description = str(event.get("event_description") or event.get("event_desc") or "")
            location_str = str(event.get("event_location") or "")
            events.append(event_to_dict(date_str, description, location_str, "mawwl"))
            
        all_parcel_no = data.get("all_parcel_no") or {}
        if isinstance(all_parcel_no, dict):
            for k, v in all_parcel_no.items():
                if isinstance(v, list) and len(v) > 0 and v[0]:
                    found_lm_awb = str(v[0]).strip()
                    break
                    
        found_lm_courier = ""
        if found_lm_awb:
            raw_upper = raw.upper()
            couriers = ["FedEx", "DHL", "UPS", "Aramex", "DPD", "DTDC", "Purolator", "NZPost", "QuickShip"]
            for c in couriers:
                if c.upper() in raw_upper:
                    found_lm_courier = c
                    break

    result = normalize_fetch_result(True, events, raw, "mawwl", found_lm_awb=found_lm_awb, found_lm_courier=found_lm_courier)
    if latest_status:
        result["latest_status_text"] = latest_status
    return result


def fetch_tracking_for_number(courier: str, tracking_number: str, tracking_type: str = "") -> dict[str, Any]:
    courier_key = normalize_courier_name(courier)
    awb = (tracking_number or "").strip()
    if not awb:
        return normalize_fetch_result(False, [], "", courier_key, "Missing tracking number")
    try:
        if courier_key in {"maww", "mawwl", "maworldwidelogistics"}:
            return fetch_mawwl(awb)
        if courier_key == "purolator":
            return fetch_purolator(awb)
        if courier_key == "atlantic":
            return fetch_atlantic(awb)
        if courier_key == "overseas":
            return fetch_overseas(awb)
        if courier_key in {"quickship", "qs"}:
            return fetch_quickship(awb)
        if courier_key in {"aramex", "aramax"}:
            return fetch_aramex(awb)
        if courier_key in {"indiapost", "indiaapost", "indianpost", "postindia"}:
            return normalize_fetch_result(False, [], "", courier_key, "India Post backend tracking is not configured")
        brand_key = seventeen_track_brand_key(courier_key)
        if brand_key == "ups":
            return fetch_17track_browser(awb, courier_key)
        if brand_key == "nzpost":
            return fetch_nz_post(awb)
        if seventeen_track_fc_for_courier(courier_key) is not None:
            return fetch_17track_browser(awb, courier_key)
        if brand_key == "fedex":
            return fetch_17track_browser(awb, courier_key)
        return normalize_fetch_result(False, [], "", courier_key or "unsupported", f"Backend fetch not configured for {courier or 'this courier'}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return normalize_fetch_result(False, [], raw, courier_key, f"HTTP {exc.code}")
    except Exception as exc:
        return normalize_fetch_result(False, [], "", courier_key, str(exc))


def fallback_events_from_shipment(shipment: Shipment) -> list[dict[str, Any]]:
    events = []
    for event in shipment.tracking_events or []:
        events.append({
            "event_time": event.event_time.isoformat() if event.event_time else "",
            "status": event.notes or event.status_text or "",
            "location": event.location or "",
            "source": event.source or "manual",
        })
    if not events and (shipment.status_raw_text or shipment.last_status_text):
        events.append({
            "event_time": (shipment.last_status_at or shipment.booking_date or datetime.now()).isoformat(),
            "status": shipment.status_raw_text or shipment.last_status_text or "",
            "location": shipment.last_status_location or shipment.destination_country or "",
            "source": "app_status",
        })
    return events



