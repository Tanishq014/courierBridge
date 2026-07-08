import os
import copy
import json
import logging
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "courierbridge_attendance_test.db")
if os.path.exists(TEST_DB_PATH):
    try:
        os.remove(TEST_DB_PATH)
    except OSError:
        pass
os.environ["COURIERBRIDGE_DATABASE_URL"] = "sqlite:///" + TEST_DB_PATH.replace("\\", "/")
os.environ["COURIERBRIDGE_REQUIRE_AUTH"] = "true"
os.environ["COURIERBRIDGE_ACCESS_PASSWORD"] = "test-password"
os.environ["COURIERBRIDGE_SECRET_KEY"] = "test-secret-key"

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.attendance_schemas import INTEGRATION_REAUTH_REQUIRED, SOURCE_JIBBLE, JibbleTimesheetPageDto
from app.attendance_services import (
    AttendanceCalculationService,
    AttendancePolicyService,
    EmployeeService,
    JibbleTimesheetSyncService,
)
from app.database import Base, engine
from app.jibble_attendance import (
    FetchedAttendanceMonth,
    HttpResponse,
    JibbleIntegrationError,
    JibbleSilentOidcAttendanceSource,
    NormalizedImportedAttendanceDay,
    base64url_no_padding,
    generate_pkce_pair,
    normalize_page,
    parse_iso8601_duration_seconds,
    stable_hash,
)
from app.models import Employee, ExternalEmployeeIdentity, ImportedAttendanceDay, JibbleSyncRun

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers, body=None, timeout=15, allow_redirects=True):
        self.requests.append({
            "method": method,
            "url": url,
            "headers": dict(headers),
            "body": body,
            "timeout": timeout,
            "allow_redirects": allow_redirects,
        })
        response = self.responses.pop(0)
        if callable(response):
            return response(self.requests[-1])
        return response


class FakeSource:
    def __init__(self, fetched=None, error=None):
        self.fetched = fetched
        self.error = error

    def fetch_month(self, month):
        if self.error:
            raise self.error
        return self.fetched


def day_payload(person_id="p1", attendance_date=date(2026, 7, 1), worked_seconds=8 * 3600, payload_hash="h1"):
    return NormalizedImportedAttendanceDay(
        external_person_id=person_id,
        external_code="E1",
        external_name="Employee One",
        external_timezone="Asia/Kolkata",
        attendance_date=attendance_date,
        first_in_utc=datetime(2026, 7, 1, 4, 30),
        last_out_utc=datetime(2026, 7, 1, 13, 30),
        first_in_local=datetime(2026, 7, 1, 10, 0),
        last_out_local=datetime(2026, 7, 1, 19, 0),
        worked_seconds=worked_seconds,
        tracked_seconds=worked_seconds,
        payroll_seconds=worked_seconds,
        break_seconds=0,
        paid_break_seconds=0,
        unpaid_break_seconds=0,
        auto_deduction_seconds=0,
        regular_seconds=worked_seconds,
        daily_overtime_seconds=0,
        daily_double_overtime_seconds=0,
        rest_day_overtime_seconds=0,
        holiday_overtime_seconds=0,
        weekly_overtime_seconds=0,
        paid_time_off_seconds=0,
        unpaid_time_off_seconds=0,
        is_rest_day_from_source=False,
        has_archived_screenshots=False,
        source_updated_at=None,
        payload_hash=payload_hash,
    )


class DbTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    def setUp(self):
        self.db = SessionLocal()

    def tearDown(self):
        self.db.rollback()
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    def create_employee(self):
        employee = Employee(
            employee_code="EMP1",
            full_name="Employee One",
            active=True,
            joining_date=date(2026, 1, 1),
            default_timezone="Asia/Kolkata",
        )
        self.db.add(employee)
        self.db.commit()
        self.db.refresh(employee)
        return employee

    def create_policy(self, employee):
        from app.attendance_schemas import AttendancePolicyRequest

        policy = AttendancePolicyService(self.db).create_policy(
            employee.id,
            AttendancePolicyRequest(
                monthly_salary_paise=2500000,
                salary_divisor_type="FIXED_DAYS",
                fixed_salary_divisor=26,
                shift_start_local=time(10, 0),
                shift_end_local=time(19, 0),
                grace_minutes=10,
                full_day_required_minutes=480,
                half_day_required_minutes=240,
                missing_clock_out_buffer_minutes=60,
                weekly_off_days=["SUNDAY"],
                effective_from=date(2026, 1, 1),
                active=True,
            ),
        )
        return policy

    def create_imported(self, employee=None, **kwargs):
        run = JibbleSyncRun(requested_month="2026-07", status="SUCCESS", authentication_status="CONNECTED")
        self.db.add(run)
        self.db.flush()
        imported = ImportedAttendanceDay(
            source=SOURCE_JIBBLE,
            external_person_id=kwargs.get("external_person_id", "p1"),
            employee_id=employee.id if employee else None,
            attendance_date=kwargs.get("attendance_date", date(2026, 7, 1)),
            first_in_local=kwargs.get("first_in_local", datetime(2026, 7, 1, 10, 0)),
            last_out_local=kwargs.get("last_out_local", datetime(2026, 7, 1, 19, 0)),
            worked_seconds=kwargs.get("worked_seconds", 8 * 3600),
            tracked_seconds=kwargs.get("tracked_seconds", 8 * 3600),
            payroll_seconds=kwargs.get("payroll_seconds", 8 * 3600),
            paid_time_off_seconds=kwargs.get("paid_time_off_seconds", 0),
            unpaid_time_off_seconds=kwargs.get("unpaid_time_off_seconds", 0),
            is_rest_day_from_source=kwargs.get("is_rest_day_from_source", False),
            payload_hash=kwargs.get("payload_hash", "hash"),
            last_sync_run_id=run.id,
        )
        self.db.add(imported)
        self.db.commit()
        self.db.refresh(imported)
        return imported


class PkceAndJibbleClientTests(unittest.TestCase):
    def test_pkce_verifier_has_no_padding_and_challenge_matches(self):
        verifier, challenge = generate_pkce_pair()
        self.assertNotIn("=", verifier)
        self.assertNotIn("=", challenge)
        import hashlib

        expected = base64url_no_padding(hashlib.sha256(verifier.encode("ascii")).digest())
        self.assertEqual(challenge, expected)

    def test_state_is_sent_and_matching_state_succeeds(self):
        def authorize_response(request):
            from urllib.parse import parse_qs, urlparse

            state = parse_qs(urlparse(request["url"]).query)["state"][0]
            return HttpResponse(302, {"Location": f"https://web.jibble.io/silent-renew.html?code=abc&state={state}"}, "")

        client = FakeHttpClient([
            authorize_response,
            HttpResponse(200, {}, '{"access_token":"token-value"}'),
        ])
        source = JibbleSilentOidcAttendanceSource(session_cookie="placeholder-session-cookie", http_client=client)
        token = source.authenticate()
        self.assertEqual(token, "token-value")
        self.assertFalse(client.requests[0]["allow_redirects"])
        self.assertIn("state=", client.requests[0]["url"])

    def test_mismatched_state_fails(self):
        client = FakeHttpClient([
            HttpResponse(302, {"Location": "https://web.jibble.io/silent-renew.html?code=abc&state=bad"}, ""),
        ])
        source = JibbleSilentOidcAttendanceSource(session_cookie="placeholder-session-cookie", http_client=client)
        with self.assertRaises(JibbleIntegrationError):
            source.authenticate()

    def test_missing_state_fails(self):
        client = FakeHttpClient([
            HttpResponse(302, {"Location": "https://web.jibble.io/silent-renew.html?code=abc"}, ""),
        ])
        source = JibbleSilentOidcAttendanceSource(session_cookie="placeholder-session-cookie", http_client=client)
        with self.assertRaises(JibbleIntegrationError):
            source.authenticate()

    def test_unexpected_redirect_host_and_path_fail(self):
        for location in [
            "https://evil.example/silent-renew.html?code=abc&state=x",
            "https://web.jibble.io/other?code=abc&state=x",
        ]:
            source = JibbleSilentOidcAttendanceSource(session_cookie="placeholder-session-cookie")
            with self.assertRaises(JibbleIntegrationError):
                source.extract_authorization_code(location, "x")

    def test_missing_location_and_login_required_are_reauth(self):
        for response in [
            HttpResponse(302, {}, ""),
            HttpResponse(302, {"Location": "https://web.jibble.io/silent-renew.html?error=login_required&state=s"}, ""),
        ]:
            client = FakeHttpClient([response])
            source = JibbleSilentOidcAttendanceSource(session_cookie="placeholder-session-cookie", http_client=client)
            with self.assertRaises(JibbleIntegrationError) as ctx:
                if response.headers:
                    source.extract_authorization_code(response.headers["Location"], "s")
                else:
                    source.authenticate()
            self.assertEqual(ctx.exception.integration_state, INTEGRATION_REAUTH_REQUIRED)

    def test_401_403_and_missing_access_token_are_reauth(self):
        for status in [401, 403]:
            client = FakeHttpClient([HttpResponse(status, {}, "")])
            source = JibbleSilentOidcAttendanceSource(session_cookie="placeholder-session-cookie", http_client=client)
            with self.assertRaises(JibbleIntegrationError) as ctx:
                source.authenticate()
            self.assertEqual(ctx.exception.integration_state, INTEGRATION_REAUTH_REQUIRED)

        def authorize_response(request):
            from urllib.parse import parse_qs, urlparse

            state = parse_qs(urlparse(request["url"]).query)["state"][0]
            return HttpResponse(302, {"Location": f"https://web.jibble.io/silent-renew.html?code=abc&state={state}"}, "")

        client = FakeHttpClient([authorize_response, HttpResponse(200, {}, "{}")])
        source = JibbleSilentOidcAttendanceSource(session_cookie="placeholder-session-cookie", http_client=client)
        with self.assertRaises(JibbleIntegrationError) as ctx:
            source.authenticate()
        self.assertEqual(ctx.exception.integration_state, INTEGRATION_REAUTH_REQUIRED)

    def test_429_retries_are_bounded(self):
        def authorize_response(request):
            from urllib.parse import parse_qs, urlparse

            state = parse_qs(urlparse(request["url"]).query)["state"][0]
            return HttpResponse(302, {"Location": f"https://web.jibble.io/silent-renew.html?code=abc&state={state}"}, "")

        client = FakeHttpClient([
            HttpResponse(429, {"Retry-After": "1"}, ""),
            HttpResponse(429, {"Retry-After": "1"}, ""),
            authorize_response,
            HttpResponse(200, {}, '{"access_token":"token"}'),
        ])
        source = JibbleSilentOidcAttendanceSource(session_cookie="placeholder-session-cookie", http_client=client)
        with patch("app.jibble_attendance.time.sleep", lambda seconds: None):
            self.assertEqual(source.authenticate(), "token")
        self.assertEqual(len(client.requests), 4)

    def test_cookie_token_and_code_are_absent_from_logs(self):
        def authorize_response(request):
            from urllib.parse import parse_qs, urlparse

            state = parse_qs(urlparse(request["url"]).query)["state"][0]
            return HttpResponse(302, {"Location": f"https://web.jibble.io/silent-renew.html?code=secret-code&state={state}"}, "")

        client = FakeHttpClient([
            authorize_response,
            HttpResponse(200, {}, '{"access_token":"secret-token"}'),
        ])
        records = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = CaptureHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            source = JibbleSilentOidcAttendanceSource(session_cookie="placeholder-session-cookie", http_client=client)
            self.assertEqual(source.authenticate(), "secret-token")
        finally:
            root.removeHandler(handler)

        joined = "\n".join(records)
        self.assertNotIn("placeholder-session-cookie", joined)
        self.assertNotIn("secret-token", joined)
        self.assertNotIn("secret-code", joined)

    def test_iso_duration_parsing(self):
        cases = {
            "PT28.833867S": 29,
            "PT31M13.6934535S": 1874,
            "PT8H30M": 30600,
            "PT1H2M3.5S": 3724,
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_iso8601_duration_seconds(raw), expected)


class JibbleTimesheetContractTests(DbTestCase):
    def load_fixture(self):
        fixture_path = Path(__file__).parent / "fixtures" / "jibble_timesheets_month.json"
        return json.loads(fixture_path.read_text(encoding="utf-8"))

    def fetched_from_payload(self, payload):
        page = JibbleTimesheetPageDto.model_validate(payload)
        days = normalize_page(page)
        return page, FetchedAttendanceMonth(
            employees_received=len({day.external_person_id for day in days}),
            days=days,
            raw_response_hash=stable_hash(payload),
        )

    def test_real_timesheet_contract_validates_and_normalizes_nested_paths(self):
        payload = self.load_fixture()
        page, fetched = self.fetched_from_payload(payload)
        days_by_key = {(day.external_person_id, day.attendance_date): day for day in fetched.days}

        self.assertEqual(page.odata_count, 2)
        self.assertEqual(fetched.employees_received, 2)
        self.assertEqual({day.attendance_date for day in fetched.days}, {date(2026, 7, 8), date(2026, 7, 9)})
        self.assertEqual(len(fetched.days), 3)

        regular_day = days_by_key[("person-001", date(2026, 7, 8))]
        self.assertEqual(regular_day.first_in_local.isoformat(), "2026-07-08T10:00:00+05:30")
        self.assertEqual(regular_day.last_out_local.isoformat(), "2026-07-08T18:00:00+05:30")
        self.assertEqual(parse_iso8601_duration_seconds("PT28.833867S"), 29)
        self.assertEqual(regular_day.worked_seconds, 8 * 3600)
        self.assertEqual(regular_day.tracked_seconds, (8 * 3600) + (15 * 60))
        self.assertEqual(regular_day.payroll_seconds, 8 * 3600)
        self.assertEqual(regular_day.break_seconds, 29)
        self.assertEqual(regular_day.paid_break_seconds, 10 * 60)
        self.assertEqual(regular_day.unpaid_break_seconds, (18 * 60) + 29)
        self.assertEqual(regular_day.auto_deduction_seconds, 0)
        self.assertEqual(regular_day.regular_seconds, (7 * 3600) + (30 * 60))
        self.assertEqual(regular_day.daily_overtime_seconds, 30 * 60)
        self.assertEqual(regular_day.daily_double_overtime_seconds, 0)
        self.assertEqual(regular_day.rest_day_overtime_seconds, 0)
        self.assertEqual(regular_day.holiday_overtime_seconds, 0)
        self.assertEqual(regular_day.weekly_overtime_seconds, 0)

        partial_leave_day = days_by_key[("person-001", date(2026, 7, 9))]
        self.assertEqual(partial_leave_day.paid_time_off_seconds, 2 * 3600)
        self.assertEqual(partial_leave_day.unpaid_time_off_seconds, 1 * 3600)

        rest_day = days_by_key[("person-002", date(2026, 7, 8))]
        self.assertTrue(rest_day.is_rest_day_from_source)
        self.assertTrue(rest_day.has_archived_screenshots)

    def test_real_timesheet_contract_sync_is_idempotent_and_corrections_update(self):
        payload = self.load_fixture()
        _, fetched = self.fetched_from_payload(payload)
        first = JibbleTimesheetSyncService(self.db, FakeSource(fetched=fetched)).sync_month("2026-07")
        self.assertEqual(first["employees_received"], 2)
        self.assertEqual(first["days_inserted"], 3)
        self.assertEqual(self.db.query(ImportedAttendanceDay).count(), 3)

        repeat = JibbleTimesheetSyncService(self.db, FakeSource(fetched=fetched)).sync_month("2026-07")
        self.assertEqual(repeat["days_unchanged"], 3)
        self.assertEqual(self.db.query(ImportedAttendanceDay).count(), 3)

        corrected_payload = copy.deepcopy(payload)
        corrected_payload["value"][0]["daily"][0]["trackedHours"]["worked"] = "PT7H"
        _, corrected = self.fetched_from_payload(corrected_payload)
        corrected_result = JibbleTimesheetSyncService(self.db, FakeSource(fetched=corrected)).sync_month("2026-07")
        self.assertEqual(corrected_result["days_updated"], 1)
        self.assertEqual(self.db.query(ImportedAttendanceDay).count(), 3)
        corrected_row = (
            self.db.query(ImportedAttendanceDay)
            .filter(ImportedAttendanceDay.external_person_id == "person-001", ImportedAttendanceDay.attendance_date == date(2026, 7, 8))
            .first()
        )
        self.assertEqual(corrected_row.worked_seconds, 7 * 3600)


class SyncAndMappingTests(DbTestCase):
    def test_sync_insert_repeat_unchanged_and_correction_update(self):
        fetched = FetchedAttendanceMonth(1, [day_payload()], "r1")
        service = JibbleTimesheetSyncService(self.db, FakeSource(fetched=fetched))
        first = service.sync_month("2026-07")
        self.assertEqual(first["days_inserted"], 1)
        self.assertEqual(self.db.query(ImportedAttendanceDay).count(), 1)

        second = JibbleTimesheetSyncService(self.db, FakeSource(fetched=fetched)).sync_month("2026-07")
        self.assertEqual(second["days_unchanged"], 1)
        self.assertEqual(self.db.query(ImportedAttendanceDay).count(), 1)

        corrected = FetchedAttendanceMonth(1, [day_payload(worked_seconds=7 * 3600, payload_hash="h2")], "r2")
        third = JibbleTimesheetSyncService(self.db, FakeSource(fetched=corrected)).sync_month("2026-07")
        self.assertEqual(third["days_updated"], 1)
        self.assertEqual(self.db.query(ImportedAttendanceDay).count(), 1)
        self.assertEqual(self.db.query(ImportedAttendanceDay).first().worked_seconds, 7 * 3600)

    def test_auth_failure_preserves_existing_attendance(self):
        JibbleTimesheetSyncService(self.db, FakeSource(fetched=FetchedAttendanceMonth(1, [day_payload()], "r1"))).sync_month("2026-07")
        result = JibbleTimesheetSyncService(
            self.db,
            FakeSource(error=JibbleIntegrationError(INTEGRATION_REAUTH_REQUIRED, "login_required", "Jibble session must be renewed.")),
        ).sync_month("2026-07")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.db.query(ImportedAttendanceDay).count(), 1)

    def test_mapping_updates_history_and_prevents_duplicate_mapping(self):
        employee = self.create_employee()
        other = Employee(employee_code="EMP2", full_name="Other", active=True, joining_date=date(2026, 1, 1), default_timezone="Asia/Kolkata")
        self.db.add(other)
        self.db.add(ExternalEmployeeIdentity(source=SOURCE_JIBBLE, external_person_id="p1", external_name="Employee One"))
        self.db.commit()
        imported = self.create_imported()

        result = EmployeeService(self.db).map_jibble_identity(employee, "p1")
        self.assertTrue(result["mapped"])
        self.db.refresh(imported)
        self.assertEqual(imported.employee_id, employee.id)

        with self.assertRaises(ValueError):
            EmployeeService(self.db).map_jibble_identity(other, "p1")

    def test_overlapping_policy_periods_rejected_and_salary_is_paise(self):
        employee = self.create_employee()
        policy = self.create_policy(employee)
        self.assertIsInstance(policy.monthly_salary_paise, int)
        from app.attendance_schemas import AttendancePolicyRequest

        with self.assertRaises(ValueError):
            AttendancePolicyService(self.db).create_policy(
                employee.id,
                AttendancePolicyRequest(
                    monthly_salary_paise=100,
                    salary_divisor_type="FIXED_DAYS",
                    fixed_salary_divisor=26,
                    shift_start_local=time(22, 0),
                    shift_end_local=time(6, 0),
                    grace_minutes=0,
                    full_day_required_minutes=480,
                    half_day_required_minutes=240,
                    missing_clock_out_buffer_minutes=60,
                    weekly_off_days=[],
                    effective_from=date(2026, 1, 1),
                    active=True,
                ),
            )


class CalculationTests(DbTestCase):
    def assert_status(self, imported, employee, expected, now_value=datetime(2026, 7, 10, 22, 0)):
        policy = self.db.query(Employee).filter(Employee.id == employee.id).first().attendance_policies[0] if employee.attendance_policies else None
        result = AttendanceCalculationService(self.db).calculate(imported, employee, policy, now_value)
        self.assertEqual(result["calculated_status"], expected)
        return result

    def test_calculation_core_statuses(self):
        employee = self.create_employee()
        self.create_policy(employee)
        self.db.refresh(employee)
        self.assert_status(self.create_imported(employee, attendance_date=date(2025, 12, 31)), employee, "NOT_APPLICABLE")
        employee.leaving_date = date(2026, 6, 30)
        self.db.commit()
        self.assert_status(self.create_imported(employee, attendance_date=date(2026, 7, 2), external_person_id="p2"), employee, "NOT_APPLICABLE")

    def test_future_in_progress_absent_weekly_off_missing_and_thresholds(self):
        employee = self.create_employee()
        self.create_policy(employee)
        self.db.refresh(employee)
        calc = AttendanceCalculationService(self.db)
        policy = employee.attendance_policies[0]

        future = self.create_imported(employee, attendance_date=date(2026, 7, 20), external_person_id="f")
        self.assertEqual(calc.calculate(future, employee, policy, datetime(2026, 7, 10, 12, 0))["calculated_status"], "NOT_EVALUATED")

        active = self.create_imported(employee, attendance_date=date(2026, 7, 10), external_person_id="a", last_out_local=None)
        self.assertEqual(calc.calculate(active, employee, policy, datetime(2026, 7, 10, 12, 0))["calculated_status"], "IN_PROGRESS")

        absent = self.create_imported(employee, attendance_date=date(2026, 7, 3), external_person_id="z", first_in_local=None, last_out_local=None, worked_seconds=0, payroll_seconds=0)
        self.assertEqual(calc.calculate(absent, employee, policy, datetime(2026, 7, 10, 22, 0))["calculated_status"], "ABSENT")

        weekly_off = self.create_imported(employee, attendance_date=date(2026, 7, 5), external_person_id="w")
        self.assertEqual(calc.calculate(weekly_off, employee, policy, datetime(2026, 7, 10, 22, 0))["calculated_status"], "WEEKLY_OFF")

        missing_out = self.create_imported(employee, attendance_date=date(2026, 7, 6), external_person_id="mo", last_out_local=None)
        self.assertEqual(calc.calculate(missing_out, employee, policy, datetime(2026, 7, 10, 22, 0))["calculated_status"], "MISSING_CLOCK_OUT")

        missing_in = self.create_imported(employee, attendance_date=date(2026, 7, 7), external_person_id="mi", first_in_local=None)
        self.assertEqual(calc.calculate(missing_in, employee, policy, datetime(2026, 7, 10, 22, 0))["calculated_status"], "MISSING_CLOCK_IN")

        half = self.create_imported(employee, attendance_date=date(2026, 7, 8), external_person_id="h", worked_seconds=5 * 3600)
        half_result = calc.calculate(half, employee, policy, datetime(2026, 7, 10, 22, 0))
        self.assertEqual(half_result["calculated_status"], "HALF_DAY")
        self.assertEqual(half_result["suggested_deduction_units"], Decimal("0.5"))

        late = self.create_imported(employee, attendance_date=date(2026, 7, 9), external_person_id="l", first_in_local=datetime(2026, 7, 9, 10, 30))
        late_result = calc.calculate(late, employee, policy, datetime(2026, 7, 10, 22, 0))
        self.assertEqual(late_result["calculated_status"], "LATE_PRESENT")
        self.assertEqual(late_result["late_minutes"], 20)

    def test_leave_missing_policy_overnight_and_idempotent_recalc(self):
        employee = self.create_employee()
        self.create_policy(employee)
        self.db.refresh(employee)
        calc = AttendanceCalculationService(self.db)
        policy = employee.attendance_policies[0]

        paid = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 2),
            external_person_id="paid",
            first_in_local=None,
            last_out_local=None,
            worked_seconds=0,
            payroll_seconds=0,
            paid_time_off_seconds=8 * 3600,
        )
        self.assertEqual(calc.calculate(paid, employee, policy, datetime(2026, 7, 10, 22, 0))["calculated_status"], "PAID_LEAVE")

        unpaid = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 3),
            external_person_id="unpaid",
            first_in_local=None,
            last_out_local=None,
            worked_seconds=0,
            payroll_seconds=0,
            unpaid_time_off_seconds=8 * 3600,
        )
        self.assertEqual(calc.calculate(unpaid, employee, policy, datetime(2026, 7, 10, 22, 0))["calculated_status"], "UNPAID_LEAVE")

        no_policy_employee = Employee(employee_code="EMP3", full_name="No Policy", active=True, joining_date=date(2026, 1, 1), default_timezone="Asia/Kolkata")
        self.db.add(no_policy_employee)
        self.db.commit()
        no_policy_import = self.create_imported(no_policy_employee, external_person_id="np")
        self.assertEqual(calc.calculate(no_policy_import, no_policy_employee, None, datetime(2026, 7, 10, 22, 0))["calculated_status"], "POLICY_MISSING")

        policy.shift_start_local = time(22, 0)
        policy.shift_end_local = time(6, 0)
        self.db.commit()
        overnight = self.create_imported(employee, attendance_date=date(2026, 7, 4), external_person_id="on", first_in_local=datetime(2026, 7, 4, 22, 5), last_out_local=datetime(2026, 7, 5, 6, 5))
        result = calc.calculate(overnight, employee, policy, datetime(2026, 7, 10, 22, 0))
        self.assertEqual(result["calculated_status"], "PRESENT")
        self.assertEqual(result["early_departure_minutes"], 0)

        action1, _ = calc.upsert_calculation(overnight, datetime(2026, 7, 10, 22, 0))
        action2, _ = calc.upsert_calculation(overnight, datetime(2026, 7, 10, 22, 0))
        self.assertEqual(action1, "created")
        self.assertEqual(action2, "unchanged")

    def test_overnight_shift_evaluation_window(self):
        employee = self.create_employee()
        self.create_policy(employee)
        self.db.refresh(employee)
        policy = employee.attendance_policies[0]
        policy.shift_start_local = time(22, 0)
        policy.shift_end_local = time(6, 0)
        policy.missing_clock_out_buffer_minutes = 60
        self.db.commit()
        calc = AttendanceCalculationService(self.db)

        before_start = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 8),
            external_person_id="before",
            first_in_local=None,
            last_out_local=None,
            worked_seconds=0,
            payroll_seconds=0,
        )
        self.assertEqual(calc.calculate(before_start, employee, policy, datetime(2026, 7, 8, 21, 0))["calculated_status"], "NOT_EVALUATED")

        during_shift = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 8),
            external_person_id="during",
            first_in_local=datetime(2026, 7, 8, 22, 5),
            last_out_local=None,
            worked_seconds=2 * 3600,
            payroll_seconds=2 * 3600,
        )
        self.assertEqual(calc.calculate(during_shift, employee, policy, datetime(2026, 7, 9, 1, 0))["calculated_status"], "IN_PROGRESS")

        after_cutoff = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 8),
            external_person_id="cutoff",
            first_in_local=datetime(2026, 7, 8, 22, 5),
            last_out_local=None,
            worked_seconds=7 * 3600,
            payroll_seconds=7 * 3600,
        )
        self.assertEqual(calc.calculate(after_cutoff, employee, policy, datetime(2026, 7, 9, 7, 1))["calculated_status"], "MISSING_CLOCK_OUT")

        completed = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 8),
            external_person_id="complete",
            first_in_local=datetime(2026, 7, 8, 22, 0),
            last_out_local=datetime(2026, 7, 9, 6, 0),
            worked_seconds=8 * 3600,
            payroll_seconds=8 * 3600,
        )
        self.assertEqual(calc.calculate(completed, employee, policy, datetime(2026, 7, 9, 7, 1))["calculated_status"], "PRESENT")

    def test_full_day_and_partial_day_leave(self):
        employee = self.create_employee()
        self.create_policy(employee)
        self.db.refresh(employee)
        calc = AttendanceCalculationService(self.db)
        policy = employee.attendance_policies[0]

        full_paid = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 2),
            external_person_id="full-paid",
            first_in_local=None,
            last_out_local=None,
            worked_seconds=0,
            payroll_seconds=0,
            paid_time_off_seconds=8 * 3600,
        )
        self.assertEqual(calc.calculate(full_paid, employee, policy, datetime(2026, 7, 10, 22, 0))["calculated_status"], "PAID_LEAVE")

        full_unpaid = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 3),
            external_person_id="full-unpaid",
            first_in_local=None,
            last_out_local=None,
            worked_seconds=0,
            payroll_seconds=0,
            unpaid_time_off_seconds=8 * 3600,
        )
        full_unpaid_result = calc.calculate(full_unpaid, employee, policy, datetime(2026, 7, 10, 22, 0))
        self.assertEqual(full_unpaid_result["calculated_status"], "UNPAID_LEAVE")
        self.assertEqual(full_unpaid_result["suggested_deduction_units"], Decimal("1"))

        partial_paid = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 4),
            external_person_id="partial-paid",
            worked_seconds=4 * 3600,
            payroll_seconds=4 * 3600,
            paid_time_off_seconds=2 * 3600,
        )
        partial_paid_result = calc.calculate(partial_paid, employee, policy, datetime(2026, 7, 10, 22, 0))
        self.assertEqual(partial_paid_result["calculated_status"], "HALF_DAY")
        self.assertTrue(partial_paid_result["needs_review"])

        partial_unpaid = self.create_imported(
            employee,
            attendance_date=date(2026, 7, 6),
            external_person_id="partial-unpaid",
            worked_seconds=8 * 3600,
            payroll_seconds=8 * 3600,
            unpaid_time_off_seconds=1 * 3600,
        )
        partial_unpaid_result = calc.calculate(partial_unpaid, employee, policy, datetime(2026, 7, 10, 22, 0))
        self.assertEqual(partial_unpaid_result["calculated_status"], "PRESENT")
        self.assertTrue(partial_unpaid_result["needs_review"])
        self.assertEqual(partial_unpaid_result["suggested_deduction_units"], Decimal("0"))


class AdminEndpointAuthTests(DbTestCase):
    def setUp(self):
        super().setUp()
        from app.main import app

        self.client = TestClient(app)

    def test_admin_endpoint_rejects_unauthenticated_and_allows_authenticated(self):
        unauthenticated = self.client.get("/admin/attendance/integrations/jibble/status", follow_redirects=False)
        self.assertEqual(unauthenticated.status_code, 303)
        self.assertEqual(unauthenticated.headers["location"], "/login")

        login = self.client.post("/login", data={"password": "test-password"}, follow_redirects=False)
        self.assertEqual(login.status_code, 303)
        authenticated = self.client.get("/admin/attendance/integrations/jibble/status")
        self.assertEqual(authenticated.status_code, 200)

    def test_state_changing_admin_endpoint_cannot_be_called_anonymously(self):
        anonymous_create = self.client.post(
            "/admin/employees",
            json={
                "employee_code": "EMP-AUTH",
                "full_name": "Auth Test",
                "active": True,
                "joining_date": "2026-07-01",
                "default_timezone": "Asia/Kolkata",
            },
            follow_redirects=False,
        )
        self.assertEqual(anonymous_create.status_code, 303)
        self.assertEqual(anonymous_create.headers["location"], "/login")


if __name__ == "__main__":
    unittest.main()
