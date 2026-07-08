from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SOURCE_JIBBLE = "JIBBLE"
BUSINESS_TIMEZONE = "Asia/Kolkata"
CALCULATION_VERSION = "attendance-v1"

INTEGRATION_CONNECTED = "CONNECTED"
INTEGRATION_REAUTH_REQUIRED = "REAUTH_REQUIRED"
INTEGRATION_RATE_LIMITED = "RATE_LIMITED"
INTEGRATION_SCHEMA_CHANGED = "SCHEMA_CHANGED"
INTEGRATION_TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"

ATTENDANCE_STATUSES = {
    "NOT_EVALUATED",
    "NOT_APPLICABLE",
    "IN_PROGRESS",
    "PRESENT",
    "LATE_PRESENT",
    "HALF_DAY",
    "ABSENT",
    "PAID_LEAVE",
    "UNPAID_LEAVE",
    "WEEKLY_OFF",
    "MISSING_CLOCK_OUT",
    "MISSING_CLOCK_IN",
    "POLICY_MISSING",
    "UNMAPPED_EMPLOYEE",
    "SOURCE_ERROR",
}

SALARY_DIVISOR_TYPES = {"FIXED_DAYS", "SCHEDULED_WORKING_DAYS", "CALENDAR_DAYS"}
WEEKDAY_VALUES = {"MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class JibblePersonDto(ProviderModel):
    id: str
    fullName: str
    code: str | None = None
    timeZone: str | None = None


class JibbleTrackedHoursDto(ProviderModel):
    added: str | None = None
    total: str
    worked: str
    totalBreakTime: str
    paidBreakTime: str
    unpaidBreakTime: str
    totalAutoDeductionTime: str
    breaks: list[dict[str, Any]] = Field(default_factory=list)
    autoDeductions: list[dict[str, Any]] = Field(default_factory=list)


class JibblePayrollTimeDto(ProviderModel):
    time: str


class JibblePayrollHoursDto(ProviderModel):
    total: str
    billing: Any = None
    regular: JibblePayrollTimeDto
    dailyOvertime: JibblePayrollTimeDto
    dailyDoubleOvertime: JibblePayrollTimeDto
    restDayOvertime: JibblePayrollTimeDto
    publicHolidayOvertime: JibblePayrollTimeDto
    weeklyOvertime: JibblePayrollTimeDto


class JibbleTimeOffDto(ProviderModel):
    paidTimeOff: str
    unpaidTimeOff: str
    isRestDay: bool
    holidays: list[dict[str, Any]] = Field(default_factory=list)
    shortDays: list[dict[str, Any]] = Field(default_factory=list)
    types: list[dict[str, Any]] = Field(default_factory=list)


class JibbleDailyDto(ProviderModel):
    date: date
    firstInTimestamp: datetime | None = None
    lastOutTimestamp: datetime | None = None
    firstInOffset: datetime | None = None
    lastOutOffset: datetime | None = None
    trackedHours: JibbleTrackedHoursDto
    payrollHours: JibblePayrollHoursDto
    timeOff: JibbleTimeOffDto
    hasArchivedScreenshots: bool | None = None
    updatedAt: datetime | None = None


class JibbleTimesheetRowDto(ProviderModel):
    personId: str
    person: JibblePersonDto
    daily: list[JibbleDailyDto]


class JibbleTimesheetPageDto(ProviderModel):
    odata_context: str | None = Field(default=None, alias="@odata.context")
    odata_count: int = Field(alias="@odata.count")
    value: list[JibbleTimesheetRowDto]

    @field_validator("odata_count")
    @classmethod
    def count_is_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("@odata.count must be non-negative")
        return value


class EmployeeCreateRequest(StrictModel):
    employee_code: str
    full_name: str
    active: bool = True
    joining_date: date
    leaving_date: date | None = None
    default_timezone: str = BUSINESS_TIMEZONE


class EmployeePatchRequest(StrictModel):
    employee_code: str | None = None
    full_name: str | None = None
    active: bool | None = None
    joining_date: date | None = None
    leaving_date: date | None = None
    default_timezone: str | None = None


class EmployeeResponse(BaseModel):
    id: int
    employee_code: str
    full_name: str
    active: bool
    joining_date: date
    leaving_date: date | None
    default_timezone: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JibbleMappingRequest(StrictModel):
    external_person_id: str


class AttendancePolicyRequest(StrictModel):
    monthly_salary_paise: int
    salary_divisor_type: Literal["FIXED_DAYS", "SCHEDULED_WORKING_DAYS", "CALENDAR_DAYS"]
    fixed_salary_divisor: int | None = None
    shift_start_local: time
    shift_end_local: time
    grace_minutes: int = 0
    full_day_required_minutes: int
    half_day_required_minutes: int
    missing_clock_out_buffer_minutes: int = 0
    weekly_off_days: list[str] = []
    effective_from: date
    effective_to: date | None = None
    active: bool = True


class AttendancePolicyPatchRequest(StrictModel):
    monthly_salary_paise: int | None = None
    salary_divisor_type: Literal["FIXED_DAYS", "SCHEDULED_WORKING_DAYS", "CALENDAR_DAYS"] | None = None
    fixed_salary_divisor: int | None = None
    shift_start_local: time | None = None
    shift_end_local: time | None = None
    grace_minutes: int | None = None
    full_day_required_minutes: int | None = None
    half_day_required_minutes: int | None = None
    missing_clock_out_buffer_minutes: int | None = None
    weekly_off_days: list[str] | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    active: bool | None = None


class AttendancePolicyResponse(BaseModel):
    id: int
    employee_id: int
    monthly_salary_paise: int
    salary_divisor_type: str
    fixed_salary_divisor: int | None
    shift_start_local: time
    shift_end_local: time
    grace_minutes: int
    full_day_required_minutes: int
    half_day_required_minutes: int
    missing_clock_out_buffer_minutes: int
    weekly_off_days: list[str]
    effective_from: date
    effective_to: date | None
    active: bool
    created_at: datetime
    updated_at: datetime


class JibbleSyncResponse(BaseModel):
    sync_run_id: int
    status: str
    authentication_status: str
    requested_month: str
    employees_received: int
    external_identities_created: int
    external_identities_updated: int
    days_inserted: int
    days_updated: int
    days_unchanged: int
    calculations_recomputed: int
    unmapped_external_people: list[dict[str, Any]]
    warnings: list[str]
    safe_error_message: str | None = None


class RecalculateResponse(BaseModel):
    days_evaluated: int
    days_created: int
    days_updated: int
    days_unchanged: int
    policy_missing_count: int
    review_required_count: int
    warnings: list[str]


class DailyAttendanceResponse(BaseModel):
    employee_id: int
    employee_name: str
    date: date
    calculated_status: str
    first_in: datetime | None
    last_out: datetime | None
    worked_minutes: int
    payroll_minutes: int
    late_minutes: int
    early_departure_minutes: int
    overtime_minutes: int
    suggested_deduction_units: Decimal
    needs_review: bool
    review_reason: str | None
    source_sync_timestamp: datetime | None
    policy_id: int | None
    calculation_version: str
