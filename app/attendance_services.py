from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.attendance_schemas import (
    BUSINESS_TIMEZONE,
    CALCULATION_VERSION,
    INTEGRATION_CONNECTED,
    INTEGRATION_REAUTH_REQUIRED,
    INTEGRATION_TEMPORARILY_UNAVAILABLE,
    SALARY_DIVISOR_TYPES,
    SOURCE_JIBBLE,
    WEEKDAY_VALUES,
)
from app.jibble_attendance import AttendanceSource, FetchedAttendanceMonth, JibbleIntegrationError, NormalizedImportedAttendanceDay
from app.models import (
    CalculatedDailyAttendance,
    Employee,
    EmployeeAttendancePolicy,
    ExternalEmployeeIdentity,
    ImportedAttendanceDay,
    JibbleSyncRun,
    now_utc,
)

KOLKATA = ZoneInfo(BUSINESS_TIMEZONE)
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass
class RecalculationStats:
    days_evaluated: int = 0
    days_created: int = 0
    days_updated: int = 0
    days_unchanged: int = 0
    policy_missing_count: int = 0
    review_required_count: int = 0
    warnings: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "days_evaluated": self.days_evaluated,
            "days_created": self.days_created,
            "days_updated": self.days_updated,
            "days_unchanged": self.days_unchanged,
            "policy_missing_count": self.policy_missing_count,
            "review_required_count": self.review_required_count,
            "warnings": self.warnings or [],
        }


def validate_month(month: str) -> str:
    value = (month or "").strip()
    if not MONTH_RE.fullmatch(value):
        raise ValueError("month must use YYYY-MM")
    return value


def month_bounds(month: str) -> tuple[date, date]:
    validate_month(month)
    year, month_num = [int(part) for part in month.split("-")]
    start = date(year, month_num, 1)
    if month_num == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month_num + 1, 1) - timedelta(days=1)
    return start, end


def normalize_weekly_off_days(days: list[str]) -> list[str]:
    normalized = []
    for day in days or []:
        value = str(day or "").strip().upper()
        if value not in WEEKDAY_VALUES:
            raise ValueError("weekly_off_days must contain valid weekday names")
        if value not in normalized:
            normalized.append(value)
    return normalized


def weekday_name(value: date) -> str:
    return ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"][value.weekday()]


def policy_weekly_off_days(policy: EmployeeAttendancePolicy) -> list[str]:
    try:
        parsed = json.loads(policy.weekly_off_days_json or "[]")
    except json.JSONDecodeError:
        return []
    return normalize_weekly_off_days(parsed if isinstance(parsed, list) else [])


def policy_response(policy: EmployeeAttendancePolicy) -> dict[str, Any]:
    return {
        "id": policy.id,
        "employee_id": policy.employee_id,
        "monthly_salary_paise": policy.monthly_salary_paise,
        "salary_divisor_type": policy.salary_divisor_type,
        "fixed_salary_divisor": policy.fixed_salary_divisor,
        "shift_start_local": policy.shift_start_local,
        "shift_end_local": policy.shift_end_local,
        "grace_minutes": policy.grace_minutes,
        "full_day_required_minutes": policy.full_day_required_minutes,
        "half_day_required_minutes": policy.half_day_required_minutes,
        "missing_clock_out_buffer_minutes": policy.missing_clock_out_buffer_minutes,
        "weekly_off_days": policy_weekly_off_days(policy),
        "effective_from": policy.effective_from,
        "effective_to": policy.effective_to,
        "active": policy.active,
        "created_at": policy.created_at,
        "updated_at": policy.updated_at,
    }


def validate_employee_dates(joining_date: date, leaving_date: date | None) -> None:
    if leaving_date and leaving_date < joining_date:
        raise ValueError("leaving_date must be on or after joining_date")


def validate_policy_fields(values: dict[str, Any]) -> None:
    if values["full_day_required_minutes"] <= values["half_day_required_minutes"]:
        raise ValueError("full_day_required_minutes must be greater than half_day_required_minutes")
    if values["grace_minutes"] < 0:
        raise ValueError("grace_minutes must be non-negative")
    if values["missing_clock_out_buffer_minutes"] < 0:
        raise ValueError("missing_clock_out_buffer_minutes must be non-negative")
    if values["monthly_salary_paise"] < 0:
        raise ValueError("monthly_salary_paise must be non-negative")
    if values["salary_divisor_type"] not in SALARY_DIVISOR_TYPES:
        raise ValueError("salary_divisor_type is invalid")
    if values["salary_divisor_type"] == "FIXED_DAYS" and not values.get("fixed_salary_divisor"):
        raise ValueError("fixed_salary_divisor is required for FIXED_DAYS")
    if values.get("fixed_salary_divisor") is not None and values["fixed_salary_divisor"] <= 0:
        raise ValueError("fixed_salary_divisor must be positive")
    if values.get("effective_to") and values["effective_to"] < values["effective_from"]:
        raise ValueError("effective_to must be on or after effective_from")
    normalize_weekly_off_days(values.get("weekly_off_days") or [])


def policies_overlap(a_start: date, a_end: date | None, b_start: date, b_end: date | None) -> bool:
    a_end_value = a_end or date.max
    b_end_value = b_end or date.max
    return a_start <= b_end_value and b_start <= a_end_value


def ensure_policy_not_overlapping(db: Session, employee_id: int, effective_from: date, effective_to: date | None, ignore_policy_id: int | None = None) -> None:
    query = db.query(EmployeeAttendancePolicy).filter(
        EmployeeAttendancePolicy.employee_id == employee_id,
        EmployeeAttendancePolicy.active == True,
    )
    if ignore_policy_id:
        query = query.filter(EmployeeAttendancePolicy.id != ignore_policy_id)
    for existing in query.all():
        if policies_overlap(effective_from, effective_to, existing.effective_from, existing.effective_to):
            raise ValueError("effective policies for one employee must not overlap")


def get_effective_policy(db: Session, employee_id: int, attendance_date: date) -> EmployeeAttendancePolicy | None:
    return (
        db.query(EmployeeAttendancePolicy)
        .filter(
            EmployeeAttendancePolicy.employee_id == employee_id,
            EmployeeAttendancePolicy.active == True,
            EmployeeAttendancePolicy.effective_from <= attendance_date,
            or_(EmployeeAttendancePolicy.effective_to == None, EmployeeAttendancePolicy.effective_to >= attendance_date),
        )
        .order_by(EmployeeAttendancePolicy.effective_from.desc())
        .first()
    )


def local_datetime(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=KOLKATA)


def scheduled_bounds(day: date, policy: EmployeeAttendancePolicy) -> tuple[datetime, datetime]:
    start = local_datetime(day, policy.shift_start_local)
    end = local_datetime(day, policy.shift_end_local)
    if policy.shift_end_local <= policy.shift_start_local:
        end += timedelta(days=1)
    return start, end


def as_kolkata(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=KOLKATA)
    return value.astimezone(KOLKATA)


def calculation_payload(calculation: CalculatedDailyAttendance) -> dict[str, Any]:
    return {
        "employee_id": calculation.employee_id,
        "attendance_date": calculation.attendance_date.isoformat(),
        "imported_attendance_day_id": calculation.imported_attendance_day_id,
        "policy_id": calculation.policy_id,
        "calculated_status": calculation.calculated_status,
        "first_in_local": calculation.first_in_local,
        "last_out_local": calculation.last_out_local,
        "worked_minutes": calculation.worked_minutes,
        "payroll_minutes": calculation.payroll_minutes,
        "late_minutes": calculation.late_minutes,
        "early_departure_minutes": calculation.early_departure_minutes,
        "overtime_minutes": calculation.overtime_minutes,
        "suggested_deduction_units": str(calculation.suggested_deduction_units),
        "needs_review": calculation.needs_review,
        "review_reason": calculation.review_reason,
        "calculation_version": calculation.calculation_version,
    }


class AttendanceCalculationService:
    def __init__(self, db: Session):
        self.db = db

    def calculate(self, imported_day: ImportedAttendanceDay, employee: Employee, policy: EmployeeAttendancePolicy | None, current_time: datetime | None = None) -> dict[str, Any]:
        """Deterministic rule order follows the Phase 3 implementation request."""
        current_time = as_kolkata(current_time or datetime.now(KOLKATA))
        attendance_date = imported_day.attendance_date
        first_in = as_kolkata(imported_day.first_in_local)
        last_out = as_kolkata(imported_day.last_out_local)
        worked_minutes = max(0, int((imported_day.worked_seconds or 0) // 60))
        payroll_minutes = max(0, int((imported_day.payroll_seconds or 0) // 60))
        overtime_minutes = max(0, int((
            (imported_day.daily_overtime_seconds or 0)
            + (imported_day.daily_double_overtime_seconds or 0)
            + (imported_day.rest_day_overtime_seconds or 0)
            + (imported_day.holiday_overtime_seconds or 0)
            + (imported_day.weekly_overtime_seconds or 0)
        ) // 60))
        result = {
            "policy_id": policy.id if policy else None,
            "calculated_status": "NOT_EVALUATED",
            "first_in_local": first_in,
            "last_out_local": last_out,
            "worked_minutes": worked_minutes,
            "payroll_minutes": payroll_minutes,
            "late_minutes": 0,
            "early_departure_minutes": 0,
            "overtime_minutes": overtime_minutes,
            "suggested_deduction_units": Decimal("0"),
            "needs_review": False,
            "review_reason": None,
        }

        if attendance_date < employee.joining_date:
            result["calculated_status"] = "NOT_APPLICABLE"
            return result
        if employee.leaving_date and attendance_date > employee.leaving_date:
            result["calculated_status"] = "NOT_APPLICABLE"
            return result
        if not policy:
            result.update({"calculated_status": "POLICY_MISSING", "needs_review": True, "review_reason": "No effective attendance policy for this date."})
            return result
        scheduled_start, scheduled_end = scheduled_bounds(attendance_date, policy)
        cutoff = scheduled_end + timedelta(minutes=policy.missing_clock_out_buffer_minutes)
        if current_time < scheduled_start:
            result["calculated_status"] = "NOT_EVALUATED"
            return result
        if current_time < cutoff:
            if first_in and not last_out:
                result["calculated_status"] = "IN_PROGRESS"
                return result
            elif not first_in:
                result["calculated_status"] = "NOT_EVALUATED"
                return result

        if weekday_name(attendance_date) in policy_weekly_off_days(policy) or imported_day.is_rest_day_from_source:
            result["calculated_status"] = "WEEKLY_OFF"
            return result
        paid_leave_minutes = max(0, int((imported_day.paid_time_off_seconds or 0) // 60))
        unpaid_leave_minutes = max(0, int((imported_day.unpaid_time_off_seconds or 0) // 60))
        full_day_leave_minutes = policy.full_day_required_minutes
        if paid_leave_minutes >= full_day_leave_minutes and worked_minutes <= 0:
            result["calculated_status"] = "PAID_LEAVE"
            return result
        if unpaid_leave_minutes >= full_day_leave_minutes and worked_minutes <= 0:
            result.update({"calculated_status": "UNPAID_LEAVE", "suggested_deduction_units": Decimal("1")})
            return result
        partial_leave_with_work = (paid_leave_minutes > 0 or unpaid_leave_minutes > 0) and worked_minutes > 0
        if first_in and not last_out:
            result.update({"calculated_status": "MISSING_CLOCK_OUT", "needs_review": True, "review_reason": "First clock-in exists but no clock-out after the evaluation cutoff."})
            return result
        if last_out and not first_in:
            result.update({"calculated_status": "MISSING_CLOCK_IN", "needs_review": True, "review_reason": "Clock-out exists but no first clock-in."})
            return result
        if not first_in and not last_out and worked_minutes <= 0 and payroll_minutes <= 0:
            result.update({"calculated_status": "ABSENT", "suggested_deduction_units": Decimal("1")})
            return result

        if first_in:
            late_threshold = scheduled_start + timedelta(minutes=policy.grace_minutes)
            result["late_minutes"] = max(0, int((first_in - late_threshold).total_seconds() // 60))
        if last_out:
            result["early_departure_minutes"] = max(0, int((scheduled_end - last_out).total_seconds() // 60))

        if worked_minutes <= 0:
            result.update({"calculated_status": "ABSENT", "suggested_deduction_units": Decimal("1")})
        elif worked_minutes < policy.half_day_required_minutes:
            result.update({"calculated_status": "HALF_DAY", "suggested_deduction_units": Decimal("0.5"), "needs_review": True, "review_reason": "Worked minutes are below the half-day threshold."})
        elif worked_minutes < policy.full_day_required_minutes:
            result.update({"calculated_status": "HALF_DAY", "suggested_deduction_units": Decimal("0.5")})
        else:
            result["calculated_status"] = "LATE_PRESENT" if result["late_minutes"] > 0 else "PRESENT"
        if partial_leave_with_work:
            result["needs_review"] = True
            result["review_reason"] = "Partial leave is combined with worked time and needs payroll review."
        return result

    def upsert_calculation(self, imported_day: ImportedAttendanceDay, current_time: datetime | None = None) -> tuple[str, CalculatedDailyAttendance]:
        if not imported_day.employee_id:
            raise ValueError("Cannot calculate unmapped attendance day")
        employee = self.db.query(Employee).filter(Employee.id == imported_day.employee_id).first()
        if not employee:
            raise ValueError("Employee not found")
        policy = get_effective_policy(self.db, employee.id, imported_day.attendance_date)
        result = self.calculate(imported_day, employee, policy, current_time)
        existing = (
            self.db.query(CalculatedDailyAttendance)
            .filter(CalculatedDailyAttendance.employee_id == employee.id, CalculatedDailyAttendance.attendance_date == imported_day.attendance_date)
            .first()
        )
        if not existing:
            existing = CalculatedDailyAttendance(employee_id=employee.id, attendance_date=imported_day.attendance_date)
            self.db.add(existing)
            action = "created"
        else:
            before = calculation_payload(existing)
            action = "updated"

        existing.imported_attendance_day_id = imported_day.id
        existing.policy_id = result["policy_id"]
        existing.calculated_status = result["calculated_status"]
        existing.first_in_local = result["first_in_local"]
        existing.last_out_local = result["last_out_local"]
        existing.worked_minutes = result["worked_minutes"]
        existing.payroll_minutes = result["payroll_minutes"]
        existing.late_minutes = result["late_minutes"]
        existing.early_departure_minutes = result["early_departure_minutes"]
        existing.overtime_minutes = result["overtime_minutes"]
        existing.suggested_deduction_units = result["suggested_deduction_units"]
        existing.needs_review = result["needs_review"]
        existing.review_reason = result["review_reason"]
        existing.calculation_version = CALCULATION_VERSION
        existing.calculated_at = now_utc()
        existing.updated_at = now_utc()
        self.db.flush()
        if action == "updated" and before == calculation_payload(existing):
            action = "unchanged"
        return action, existing


class AttendanceRecalculationService:
    def __init__(self, db: Session):
        self.db = db
        self.calculator = AttendanceCalculationService(db)

    def recalculate_imported_days(self, imported_days: list[ImportedAttendanceDay]) -> RecalculationStats:
        stats = RecalculationStats(warnings=[])
        seen: set[int] = set()
        for imported_day in imported_days:
            if not imported_day.employee_id or imported_day.id in seen:
                continue
            seen.add(imported_day.id)
            stats.days_evaluated += 1
            action, calculation = self.calculator.upsert_calculation(imported_day)
            if action == "created":
                stats.days_created += 1
            elif action == "updated":
                stats.days_updated += 1
            else:
                stats.days_unchanged += 1
            if calculation.calculated_status == "POLICY_MISSING":
                stats.policy_missing_count += 1
            if calculation.needs_review:
                stats.review_required_count += 1
        return stats

    def recalculate_month(self, month: str, employee_id: int | None = None) -> RecalculationStats:
        start, end = month_bounds(month)
        query = self.db.query(ImportedAttendanceDay).filter(
            ImportedAttendanceDay.attendance_date >= start,
            ImportedAttendanceDay.attendance_date <= end,
            ImportedAttendanceDay.employee_id != None,
        )
        if employee_id:
            query = query.filter(ImportedAttendanceDay.employee_id == employee_id)
        return self.recalculate_imported_days(query.order_by(ImportedAttendanceDay.attendance_date.asc()).all())


class JibbleTimesheetSyncService:
    def __init__(self, db: Session, source: AttendanceSource):
        self.db = db
        self.source = source

    def sync_month(self, month: str) -> dict[str, Any]:
        requested_month = validate_month(month)
        sync_run = JibbleSyncRun(requested_month=requested_month, status="RUNNING")
        self.db.add(sync_run)
        self.db.commit()
        self.db.refresh(sync_run)
        warnings: list[str] = []
        try:
            fetched = self.source.fetch_month(requested_month)
            result = self.persist_successful_sync(sync_run, requested_month, fetched, warnings)
            self.db.commit()
            return result
        except JibbleIntegrationError as exc:
            self.db.rollback()
            self.mark_sync_failed(sync_run.id, exc.integration_state, exc.error_code, exc.safe_message)
            return {
                "sync_run_id": sync_run.id,
                "status": "FAILED",
                "authentication_status": exc.integration_state,
                "requested_month": requested_month,
                "employees_received": 0,
                "external_identities_created": 0,
                "external_identities_updated": 0,
                "days_inserted": 0,
                "days_updated": 0,
                "days_unchanged": 0,
                "calculations_recomputed": 0,
                "unmapped_external_people": [],
                "warnings": ["Jibble session must be renewed."] if exc.integration_state == INTEGRATION_REAUTH_REQUIRED else [],
                "safe_error_message": exc.safe_message,
            }
        except Exception:
            self.db.rollback()
            self.mark_sync_failed(sync_run.id, INTEGRATION_TEMPORARILY_UNAVAILABLE, "sync_failed", "Attendance synchronization failed safely.")
            raise

    def mark_sync_failed(self, sync_run_id: int, auth_status: str, error_code: str, safe_message: str) -> None:
        run = self.db.query(JibbleSyncRun).filter(JibbleSyncRun.id == sync_run_id).first()
        if run:
            run.status = "FAILED"
            run.authentication_status = auth_status
            run.completed_at = now_utc()
            run.error_code = error_code
            run.safe_error_message = safe_message
            run.warning_count = 1 if auth_status == INTEGRATION_REAUTH_REQUIRED else 0
            self.db.commit()

    def persist_successful_sync(self, sync_run: JibbleSyncRun, requested_month: str, fetched: FetchedAttendanceMonth, warnings: list[str]) -> dict[str, Any]:
        external_created = 0
        external_updated = 0
        days_inserted = 0
        days_updated = 0
        days_unchanged = 0
        affected_days: list[ImportedAttendanceDay] = []
        identity_by_person: dict[str, ExternalEmployeeIdentity] = {}

        for day in fetched.days:
            existing_identity = identity_by_person.get(day.external_person_id)
            if existing_identity:
                identity = existing_identity
            else:
                identity, identity_action = self.upsert_identity(day)
                if identity_action == "created":
                    external_created += 1
                elif identity_action == "updated":
                    external_updated += 1
            identity_by_person[day.external_person_id] = identity
        self.db.flush()

        for day in fetched.days:
            imported, action = self.upsert_imported_day(day, identity_by_person[day.external_person_id], sync_run.id)
            if action == "inserted":
                days_inserted += 1
                affected_days.append(imported)
            elif action == "updated":
                days_updated += 1
                affected_days.append(imported)
            else:
                days_unchanged += 1

        recalc_stats = AttendanceRecalculationService(self.db).recalculate_imported_days(affected_days)
        unmapped = self.unmapped_people_from_identities(identity_by_person.values())
        if unmapped:
            warnings.append(f"{len(unmapped)} Jibble people are not mapped to local employees.")

        sync_run.status = "SUCCESS"
        sync_run.authentication_status = INTEGRATION_CONNECTED
        sync_run.completed_at = now_utc()
        sync_run.employees_received = fetched.employees_received
        sync_run.days_inserted = days_inserted
        sync_run.days_updated = days_updated
        sync_run.days_unchanged = days_unchanged
        sync_run.calculations_recomputed = recalc_stats.days_created + recalc_stats.days_updated
        sync_run.warning_count = len(warnings)
        sync_run.raw_response_hash = fetched.raw_response_hash
        return {
            "sync_run_id": sync_run.id,
            "status": sync_run.status,
            "authentication_status": sync_run.authentication_status,
            "requested_month": requested_month,
            "employees_received": fetched.employees_received,
            "external_identities_created": external_created,
            "external_identities_updated": external_updated,
            "days_inserted": days_inserted,
            "days_updated": days_updated,
            "days_unchanged": days_unchanged,
            "calculations_recomputed": sync_run.calculations_recomputed,
            "unmapped_external_people": unmapped,
            "warnings": warnings,
            "safe_error_message": None,
        }

    def upsert_identity(self, day: NormalizedImportedAttendanceDay) -> tuple[ExternalEmployeeIdentity, str]:
        now = now_utc()
        identity = (
            self.db.query(ExternalEmployeeIdentity)
            .filter(ExternalEmployeeIdentity.source == SOURCE_JIBBLE, ExternalEmployeeIdentity.external_person_id == day.external_person_id)
            .first()
        )
        if not identity:
            identity = ExternalEmployeeIdentity(
                source=SOURCE_JIBBLE,
                external_person_id=day.external_person_id,
                external_code=day.external_code,
                external_name=day.external_name,
                external_timezone=day.external_timezone,
                first_seen_at=now,
                last_seen_at=now,
                active=True,
            )
            self.db.add(identity)
            return identity, "created"
        changed = (
            identity.external_code != day.external_code
            or identity.external_name != day.external_name
            or identity.external_timezone != day.external_timezone
            or not identity.active
        )
        identity.external_code = day.external_code
        identity.external_name = day.external_name
        identity.external_timezone = day.external_timezone
        identity.last_seen_at = now
        identity.active = True
        if changed:
            identity.updated_at = now
            return identity, "updated"
        return identity, "unchanged"

    def upsert_imported_day(self, day: NormalizedImportedAttendanceDay, identity: ExternalEmployeeIdentity, sync_run_id: int) -> tuple[ImportedAttendanceDay, str]:
        imported = (
            self.db.query(ImportedAttendanceDay)
            .filter(
                ImportedAttendanceDay.source == SOURCE_JIBBLE,
                ImportedAttendanceDay.external_person_id == day.external_person_id,
                ImportedAttendanceDay.attendance_date == day.attendance_date,
            )
            .first()
        )
        values = {
            "employee_id": identity.employee_id,
            "first_in_utc": day.first_in_utc,
            "last_out_utc": day.last_out_utc,
            "first_in_local": day.first_in_local,
            "last_out_local": day.last_out_local,
            "worked_seconds": day.worked_seconds,
            "tracked_seconds": day.tracked_seconds,
            "payroll_seconds": day.payroll_seconds,
            "break_seconds": day.break_seconds,
            "paid_break_seconds": day.paid_break_seconds,
            "unpaid_break_seconds": day.unpaid_break_seconds,
            "auto_deduction_seconds": day.auto_deduction_seconds,
            "regular_seconds": day.regular_seconds,
            "daily_overtime_seconds": day.daily_overtime_seconds,
            "daily_double_overtime_seconds": day.daily_double_overtime_seconds,
            "rest_day_overtime_seconds": day.rest_day_overtime_seconds,
            "holiday_overtime_seconds": day.holiday_overtime_seconds,
            "weekly_overtime_seconds": day.weekly_overtime_seconds,
            "paid_time_off_seconds": day.paid_time_off_seconds,
            "unpaid_time_off_seconds": day.unpaid_time_off_seconds,
            "is_rest_day_from_source": day.is_rest_day_from_source,
            "has_archived_screenshots": day.has_archived_screenshots,
            "payload_hash": day.payload_hash,
            "last_sync_run_id": sync_run_id,
            "source_updated_at": day.source_updated_at,
        }
        if not imported:
            imported = ImportedAttendanceDay(source=SOURCE_JIBBLE, external_person_id=day.external_person_id, attendance_date=day.attendance_date, **values)
            self.db.add(imported)
            self.db.flush()
            return imported, "inserted"
        if imported.payload_hash == day.payload_hash and imported.employee_id == identity.employee_id:
            imported.last_sync_run_id = sync_run_id
            return imported, "unchanged"
        for key, value in values.items():
            setattr(imported, key, value)
        imported.updated_at = now_utc()
        self.db.flush()
        return imported, "updated"

    def unmapped_people_from_identities(self, identities: Any) -> list[dict[str, Any]]:
        return [
            {
                "external_person_id": identity.external_person_id,
                "external_code": identity.external_code,
                "external_name": identity.external_name,
                "external_timezone": identity.external_timezone,
            }
            for identity in identities
            if not identity.employee_id
        ]


class EmployeeService:
    def __init__(self, db: Session):
        self.db = db

    def create_employee(self, payload: Any) -> Employee:
        validate_employee_dates(payload.joining_date, payload.leaving_date)
        employee = Employee(
            employee_code=payload.employee_code.strip(),
            full_name=payload.full_name.strip(),
            active=payload.active,
            joining_date=payload.joining_date,
            leaving_date=payload.leaving_date,
            default_timezone=payload.default_timezone or BUSINESS_TIMEZONE,
        )
        self.db.add(employee)
        self.db.commit()
        self.db.refresh(employee)
        return employee

    def patch_employee(self, employee: Employee, payload: Any) -> Employee:
        before_dates = (employee.joining_date, employee.leaving_date)
        for field in ["employee_code", "full_name", "active", "joining_date", "leaving_date", "default_timezone"]:
            value = getattr(payload, field)
            if value is not None:
                setattr(employee, field, value.strip() if isinstance(value, str) else value)
        validate_employee_dates(employee.joining_date, employee.leaving_date)
        employee.updated_at = now_utc()
        if before_dates != (employee.joining_date, employee.leaving_date):
            self.recalculate_employee_history(employee.id)
        else:
            self.db.commit()
        self.db.refresh(employee)
        return employee

    def map_jibble_identity(self, employee: Employee, external_person_id: str) -> dict[str, Any]:
        identity = (
            self.db.query(ExternalEmployeeIdentity)
            .filter(ExternalEmployeeIdentity.source == SOURCE_JIBBLE, ExternalEmployeeIdentity.external_person_id == external_person_id)
            .first()
        )
        if not identity:
            raise ValueError("Jibble external identity not found")
        if identity.employee_id and identity.employee_id != employee.id:
            raise ValueError("Jibble external identity is already mapped to another employee")
        identity.employee_id = employee.id
        identity.updated_at = now_utc()
        imported_days = (
            self.db.query(ImportedAttendanceDay)
            .filter(ImportedAttendanceDay.source == SOURCE_JIBBLE, ImportedAttendanceDay.external_person_id == external_person_id)
            .all()
        )
        for imported in imported_days:
            imported.employee_id = employee.id
            imported.updated_at = now_utc()
        recalc_stats = AttendanceRecalculationService(self.db).recalculate_imported_days(imported_days)
        self.db.commit()
        return {"mapped": True, "recalculation": recalc_stats.as_dict()}

    def recalculate_employee_history(self, employee_id: int) -> None:
        imported_days = self.db.query(ImportedAttendanceDay).filter(ImportedAttendanceDay.employee_id == employee_id).all()
        AttendanceRecalculationService(self.db).recalculate_imported_days(imported_days)
        self.db.commit()


class AttendancePolicyService:
    def __init__(self, db: Session):
        self.db = db

    def create_policy(self, employee_id: int, payload: Any) -> EmployeeAttendancePolicy:
        values = payload.model_dump()
        validate_policy_fields(values)
        ensure_policy_not_overlapping(self.db, employee_id, values["effective_from"], values["effective_to"])
        policy = EmployeeAttendancePolicy(
            employee_id=employee_id,
            monthly_salary_paise=values["monthly_salary_paise"],
            salary_divisor_type=values["salary_divisor_type"],
            fixed_salary_divisor=values["fixed_salary_divisor"],
            shift_start_local=values["shift_start_local"],
            shift_end_local=values["shift_end_local"],
            grace_minutes=values["grace_minutes"],
            full_day_required_minutes=values["full_day_required_minutes"],
            half_day_required_minutes=values["half_day_required_minutes"],
            missing_clock_out_buffer_minutes=values["missing_clock_out_buffer_minutes"],
            weekly_off_days_json=json.dumps(normalize_weekly_off_days(values["weekly_off_days"]), separators=(",", ":")),
            effective_from=values["effective_from"],
            effective_to=values["effective_to"],
            active=values["active"],
        )
        self.db.add(policy)
        self.db.flush()
        self.recalculate_policy_range(employee_id, policy.effective_from, policy.effective_to)
        self.db.commit()
        self.db.refresh(policy)
        return policy

    def patch_policy(self, policy: EmployeeAttendancePolicy, payload: Any) -> EmployeeAttendancePolicy:
        values = policy_response(policy)
        updates = payload.model_dump(exclude_unset=True)
        values.update(updates)
        validate_policy_fields(values)
        ensure_policy_not_overlapping(self.db, policy.employee_id, values["effective_from"], values["effective_to"], policy.id)
        policy.monthly_salary_paise = values["monthly_salary_paise"]
        policy.salary_divisor_type = values["salary_divisor_type"]
        policy.fixed_salary_divisor = values["fixed_salary_divisor"]
        policy.shift_start_local = values["shift_start_local"]
        policy.shift_end_local = values["shift_end_local"]
        policy.grace_minutes = values["grace_minutes"]
        policy.full_day_required_minutes = values["full_day_required_minutes"]
        policy.half_day_required_minutes = values["half_day_required_minutes"]
        policy.missing_clock_out_buffer_minutes = values["missing_clock_out_buffer_minutes"]
        policy.weekly_off_days_json = json.dumps(normalize_weekly_off_days(values["weekly_off_days"]), separators=(",", ":"))
        policy.effective_from = values["effective_from"]
        policy.effective_to = values["effective_to"]
        policy.active = values["active"]
        policy.updated_at = now_utc()
        self.recalculate_policy_range(policy.employee_id, policy.effective_from, policy.effective_to)
        self.db.commit()
        self.db.refresh(policy)
        return policy

    def recalculate_policy_range(self, employee_id: int, start: date, end: date | None) -> None:
        query = self.db.query(ImportedAttendanceDay).filter(
            ImportedAttendanceDay.employee_id == employee_id,
            ImportedAttendanceDay.attendance_date >= start,
        )
        if end:
            query = query.filter(ImportedAttendanceDay.attendance_date <= end)
        AttendanceRecalculationService(self.db).recalculate_imported_days(query.all())


def integration_status(db: Session) -> dict[str, Any]:
    last_attempt = db.query(JibbleSyncRun).order_by(JibbleSyncRun.started_at.desc()).first()
    last_success = db.query(JibbleSyncRun).filter(JibbleSyncRun.status == "SUCCESS").order_by(JibbleSyncRun.completed_at.desc()).first()
    state = INTEGRATION_CONNECTED if last_success and (not last_attempt or last_attempt.status == "SUCCESS") else INTEGRATION_TEMPORARILY_UNAVAILABLE
    if last_attempt and last_attempt.authentication_status:
        state = last_attempt.authentication_status if last_attempt.status != "SUCCESS" else INTEGRATION_CONNECTED
    return {
        "integration_state": state,
        "last_successful_sync": last_success.completed_at if last_success else None,
        "last_attempted_sync": last_attempt.started_at if last_attempt else None,
        "last_requested_month": last_attempt.requested_month if last_attempt else None,
        "reauthentication_required": state == INTEGRATION_REAUTH_REQUIRED,
        "safe_warning_message": last_attempt.safe_error_message if last_attempt and last_attempt.status != "SUCCESS" else None,
    }
