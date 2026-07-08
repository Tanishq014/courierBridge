from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Protocol

from pydantic import ValidationError

from app.attendance_schemas import (
    BUSINESS_TIMEZONE,
    INTEGRATION_RATE_LIMITED,
    INTEGRATION_REAUTH_REQUIRED,
    INTEGRATION_SCHEMA_CHANGED,
    INTEGRATION_TEMPORARILY_UNAVAILABLE,
    JibbleTimesheetPageDto,
)

AUTHORIZE_URL = "https://identity.prod.jibble.io/connect/authorize"
TOKEN_URL = "https://identity.prod.jibble.io/connect/token"
TIMESHEETS_URL = "https://time-attendance.prod.jibble.io/v1/Timesheets"
CLIENT_ID = "spa.client"
REDIRECT_URI = "https://web.jibble.io/silent-renew.html"
SCOPE = "openid profile api1 email phone"
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: str


class HttpClient(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None, timeout: int = READ_TIMEOUT_SECONDS, allow_redirects: bool = True) -> HttpResponse:
        ...


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibHttpClient:
    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None, timeout: int = READ_TIMEOUT_SECONDS, allow_redirects: bool = True) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        opener = urllib.request.build_opener() if allow_redirects else urllib.request.build_opener(NoRedirectHandler)
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return HttpResponse(response.status, dict(response.headers.items()), raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return HttpResponse(exc.code, dict(exc.headers.items()), raw)


class AttendanceSource(Protocol):
    def fetch_month(self, month: str) -> "FetchedAttendanceMonth":
        ...


@dataclass(frozen=True)
class NormalizedImportedAttendanceDay:
    external_person_id: str
    external_code: str | None
    external_name: str
    external_timezone: str | None
    attendance_date: date
    first_in_utc: datetime | None
    last_out_utc: datetime | None
    first_in_local: datetime | None
    last_out_local: datetime | None
    worked_seconds: int
    tracked_seconds: int
    payroll_seconds: int
    break_seconds: int
    paid_break_seconds: int
    unpaid_break_seconds: int
    auto_deduction_seconds: int
    regular_seconds: int
    daily_overtime_seconds: int
    daily_double_overtime_seconds: int
    rest_day_overtime_seconds: int
    holiday_overtime_seconds: int
    weekly_overtime_seconds: int
    paid_time_off_seconds: int
    unpaid_time_off_seconds: int
    is_rest_day_from_source: bool
    has_archived_screenshots: bool
    source_updated_at: datetime | None
    payload_hash: str


@dataclass(frozen=True)
class FetchedAttendanceMonth:
    employees_received: int
    days: list[NormalizedImportedAttendanceDay]
    raw_response_hash: str


class JibbleIntegrationError(RuntimeError):
    def __init__(self, integration_state: str, error_code: str, safe_message: str, retry_after_seconds: int | None = None):
        super().__init__(safe_message)
        self.integration_state = integration_state
        self.error_code = error_code
        self.safe_message = safe_message
        self.retry_after_seconds = retry_after_seconds


def base64url_no_padding(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def generate_pkce_pair() -> tuple[str, str]:
    verifier = base64url_no_padding(secrets.token_bytes(32))
    challenge = base64url_no_padding(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def generate_state() -> str:
    return base64url_no_padding(secrets.token_bytes(24))


def parse_retry_after(value: str | None) -> int:
    if not value:
        return 1
    try:
        return max(1, min(30, int(value.strip())))
    except ValueError:
        return 1


def parse_iso8601_duration_seconds(value: Any) -> int:
    """Parse ISO-8601 durations and round to nearest second using Decimal ROUND_HALF_UP."""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        value = str(value)
    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)

    import re

    pattern = re.compile(
        r"^P"
        r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
        r"(?:T"
        r"(?:(?P<hours>\d+(?:\.\d+)?)H)?"
        r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
        r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
        r")?$"
    )
    match = pattern.fullmatch(text)
    if not match:
        raise ValueError(f"Invalid ISO-8601 duration: {text}")
    total = Decimal("0")
    if match.group("days"):
        total += Decimal(match.group("days")) * Decimal(86400)
    if match.group("hours"):
        total += Decimal(match.group("hours")) * Decimal(3600)
    if match.group("minutes"):
        total += Decimal(match.group("minutes")) * Decimal(60)
    if match.group("seconds"):
        total += Decimal(match.group("seconds"))
    return max(0, int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_datetime(value: datetime | None, as_utc: bool = False) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc) if as_utc else value
    return value.astimezone(timezone.utc) if as_utc else value


def normalized_day_payload(row: Any, daily: Any) -> dict[str, Any]:
    payload = {
        "external_person_id": row.personId,
        "external_code": row.person.code,
        "external_name": row.person.fullName,
        "external_timezone": row.person.timeZone,
        "attendance_date": daily.date,
        "first_in_utc": normalize_datetime(daily.firstInTimestamp, True),
        "last_out_utc": normalize_datetime(daily.lastOutTimestamp, True),
        "first_in_local": normalize_datetime(daily.firstInOffset),
        "last_out_local": normalize_datetime(daily.lastOutOffset),
        "worked_seconds": parse_iso8601_duration_seconds(daily.trackedHours.worked),
        "tracked_seconds": parse_iso8601_duration_seconds(daily.trackedHours.total),
        "payroll_seconds": parse_iso8601_duration_seconds(daily.payrollHours.total),
        "break_seconds": parse_iso8601_duration_seconds(daily.trackedHours.totalBreakTime),
        "paid_break_seconds": parse_iso8601_duration_seconds(daily.trackedHours.paidBreakTime),
        "unpaid_break_seconds": parse_iso8601_duration_seconds(daily.trackedHours.unpaidBreakTime),
        "auto_deduction_seconds": parse_iso8601_duration_seconds(daily.trackedHours.totalAutoDeductionTime),
        "regular_seconds": parse_iso8601_duration_seconds(daily.payrollHours.regular.time),
        "daily_overtime_seconds": parse_iso8601_duration_seconds(daily.payrollHours.dailyOvertime.time),
        "daily_double_overtime_seconds": parse_iso8601_duration_seconds(daily.payrollHours.dailyDoubleOvertime.time),
        "rest_day_overtime_seconds": parse_iso8601_duration_seconds(daily.payrollHours.restDayOvertime.time),
        "holiday_overtime_seconds": parse_iso8601_duration_seconds(daily.payrollHours.publicHolidayOvertime.time),
        "weekly_overtime_seconds": parse_iso8601_duration_seconds(daily.payrollHours.weeklyOvertime.time),
        "paid_time_off_seconds": parse_iso8601_duration_seconds(daily.timeOff.paidTimeOff),
        "unpaid_time_off_seconds": parse_iso8601_duration_seconds(daily.timeOff.unpaidTimeOff),
        "is_rest_day_from_source": bool(daily.timeOff.isRestDay),
        "has_archived_screenshots": bool(daily.hasArchivedScreenshots),
        "source_updated_at": normalize_datetime(daily.updatedAt, True),
    }
    return payload


def normalize_page(page: JibbleTimesheetPageDto) -> list[NormalizedImportedAttendanceDay]:
    days: list[NormalizedImportedAttendanceDay] = []
    for row in page.value:
        if row.personId != row.person.id:
            raise JibbleIntegrationError(INTEGRATION_SCHEMA_CHANGED, "person_id_mismatch", "Jibble returned an unexpected timesheet shape.")
        for daily in row.daily:
            payload = normalized_day_payload(row, daily)
            days.append(NormalizedImportedAttendanceDay(**payload, payload_hash=stable_hash(payload)))
    return days


class JibbleSilentOidcAttendanceSource:
    def __init__(self, session_cookie: str | None = None, timezone_name: str | None = None, http_client: HttpClient | None = None, person_id: str | None = None):
        self.session_cookie = (session_cookie if session_cookie is not None else os.environ.get("JIBBLE_SESSION_COOKIE", "")).strip()
        self.timezone_name = (timezone_name or os.environ.get("JIBBLE_TIMEZONE") or BUSINESS_TIMEZONE).strip()
        self.person_id = (person_id or os.environ.get("JIBBLE_PERSON_ID", "")).strip()
        self.http_client = http_client or UrllibHttpClient()

    def ensure_configured(self) -> None:
        if not self.session_cookie:
            raise JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, "missing_session_cookie", "Jibble session must be renewed.")
        if self.timezone_name != BUSINESS_TIMEZONE:
            raise JibbleIntegrationError(INTEGRATION_TEMPORARILY_UNAVAILABLE, "unsupported_timezone", "Jibble timezone must be Asia/Kolkata for this workflow.")

    def request_with_rate_limit(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None, timeout: int = READ_TIMEOUT_SECONDS, allow_redirects: bool = True) -> HttpResponse:
        for attempt in range(3):
            response = self.http_client.request(method, url, headers=headers, body=body, timeout=timeout, allow_redirects=allow_redirects)
            if response.status_code != 429:
                return response
            retry_after = parse_retry_after(response.headers.get("Retry-After") or response.headers.get("retry-after"))
            if attempt >= 2:
                raise JibbleIntegrationError(INTEGRATION_RATE_LIMITED, "rate_limited", "Jibble is rate limiting attendance sync. Try again later.", retry_after)
            time.sleep(retry_after)
        return response

    def authenticate(self) -> str:
        self.ensure_configured()
        verifier, challenge = generate_pkce_pair()
        state = generate_state()
        query = urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "prompt": "none",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        })
        authorize_response = self.request_with_rate_limit(
            "GET",
            f"{AUTHORIZE_URL}?{query}",
            headers={"Cookie": self.session_cookie, "Accept": "text/html,application/xhtml+xml"},
            timeout=CONNECT_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if authorize_response.status_code in {401, 403}:
            raise JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, f"http_{authorize_response.status_code}", "Jibble session must be renewed.")
        if authorize_response.status_code not in {302, 303}:
            raise JibbleIntegrationError(INTEGRATION_TEMPORARILY_UNAVAILABLE, "authorize_failed", "Jibble authentication is temporarily unavailable.")

        location = authorize_response.headers.get("Location") or authorize_response.headers.get("location")
        if not location:
            raise JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, "missing_redirect_location", "Jibble session must be renewed.")
        code = self.extract_authorization_code(location, state)

        token_body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
            "acr_values": f"prsid:{self.person_id}",
        }).encode("utf-8")
        token_response = self.request_with_rate_limit(
            "POST",
            TOKEN_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://web.jibble.io",
                "Referer": "https://web.jibble.io/",
            },
            body=token_body,
            timeout=READ_TIMEOUT_SECONDS,
        )
        if token_response.status_code in {401, 403}:
            raise JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, f"http_{token_response.status_code}", "Jibble session must be renewed.")
        if token_response.status_code >= 400:
            raise JibbleIntegrationError(INTEGRATION_TEMPORARILY_UNAVAILABLE, "token_failed", "Jibble authentication is temporarily unavailable.")
        try:
            token_payload = json.loads(token_response.body or "{}")
        except json.JSONDecodeError as exc:
            raise JibbleIntegrationError(INTEGRATION_SCHEMA_CHANGED, "token_json_invalid", "Jibble authentication returned an unexpected response.") from exc
        access_token = str(token_payload.get("access_token") or "").strip()
        if not access_token:
            raise JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, "missing_access_token", "Jibble session must be renewed.")
        return access_token

    def extract_authorization_code(self, location: str, expected_state: str) -> str:
        parsed = urllib.parse.urlparse(location)
        if parsed.scheme != "https" or parsed.netloc != "web.jibble.io" or parsed.path != "/silent-renew.html":
            raise JibbleIntegrationError(INTEGRATION_TEMPORARILY_UNAVAILABLE, "unexpected_redirect", "Jibble authentication returned an unexpected redirect.")
        params = urllib.parse.parse_qs(parsed.query)
        returned_state = (params.get("state") or [""])[0]
        if not returned_state:
            raise JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, "missing_state", "Jibble session must be renewed.")
        if returned_state != expected_state:
            raise JibbleIntegrationError(INTEGRATION_TEMPORARILY_UNAVAILABLE, "state_mismatch", "Jibble authentication returned an unexpected state.")
        error = (params.get("error") or [""])[0]
        if error in {"login_required", "interaction_required"}:
            raise JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, error, "Jibble session must be renewed.")
        code = (params.get("code") or [""])[0]
        if not code:
            raise JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, "missing_authorization_code", "Jibble session must be renewed.")
        return code

    def fetch_month(self, month: str) -> FetchedAttendanceMonth:
        access_token = self.authenticate()
        first_day = f"{month}-01"
        all_raw_pages: list[dict[str, Any]] = []
        all_days: list[NormalizedImportedAttendanceDay] = []
        skip = 0
        top = 20
        total = None
        while total is None or skip < total:
            params = {
                "$count": "true",
                "$expand": "person",
                "$orderby": "person/fullName asc",
                "$skip": str(skip),
                "$top": str(top),
                "date": first_day,
                "period": "Month",
            }
            url = TIMESHEETS_URL + "?" + urllib.parse.urlencode(params)
            response = self.request_with_rate_limit(
                "GET",
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "Origin": "https://web.jibble.io",
                    "Referer": "https://web.jibble.io/",
                },
                timeout=READ_TIMEOUT_SECONDS,
            )
            if response.status_code in {401, 403}:
                raise JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, f"http_{response.status_code}", "Jibble session must be renewed.")
            if response.status_code >= 400:
                raise JibbleIntegrationError(INTEGRATION_TEMPORARILY_UNAVAILABLE, "timesheet_http_error", "Jibble timesheets are temporarily unavailable.")
            try:
                raw_page = json.loads(response.body or "{}")
                page = JibbleTimesheetPageDto.model_validate(raw_page)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                raise JibbleIntegrationError(INTEGRATION_SCHEMA_CHANGED, "timesheet_schema_changed", "Jibble returned an unexpected timesheet shape.") from exc
            all_raw_pages.append(raw_page)
            all_days.extend(normalize_page(page))
            total = page.odata_count
            skip += top
            if total == 0:
                break
        people = {day.external_person_id for day in all_days}
        return FetchedAttendanceMonth(
            employees_received=len(people),
            days=all_days,
            raw_response_hash=stable_hash(all_raw_pages),
        )
