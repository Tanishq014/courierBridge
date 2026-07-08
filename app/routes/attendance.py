from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.attendance_schemas import (
    AttendancePolicyPatchRequest,
    AttendancePolicyRequest,
    AttendancePolicyResponse,
    DailyAttendanceResponse,
    EmployeeCreateRequest,
    EmployeePatchRequest,
    EmployeeResponse,
    JibbleMappingRequest,
    JibbleSyncResponse,
    RecalculateResponse,
    SOURCE_JIBBLE,
)
from app.attendance_services import (
    AttendancePolicyService,
    AttendanceRecalculationService,
    EmployeeService,
    integration_status,
    month_bounds,
    policy_response,
    validate_month,
)
from app.database import get_db
from app.jibble_attendance import JibbleSilentOidcAttendanceSource
from app.models import (
    CalculatedDailyAttendance,
    Employee,
    EmployeeAttendancePolicy,
    ExternalEmployeeIdentity,
    ImportedAttendanceDay,
    JibbleSyncRun,
)

router = APIRouter(prefix="/admin", tags=["attendance-admin"])


def bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/attendance/sync/jibble", response_model=JibbleSyncResponse)
def sync_jibble_attendance(month: str = Query(...), db: Session = Depends(get_db)):
    try:
        validate_month(month)
        source = JibbleSilentOidcAttendanceSource()
        from app.attendance_services import JibbleTimesheetSyncService

        return JibbleTimesheetSyncService(db, source).sync_month(month)
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.get("/attendance/integrations/jibble/status")
def jibble_integration_status(db: Session = Depends(get_db)):
    return integration_status(db)


@router.get("/employees", response_model=list[EmployeeResponse])
def list_employees(db: Session = Depends(get_db)):
    return db.query(Employee).order_by(Employee.full_name.asc()).all()


@router.post("/employees", response_model=EmployeeResponse)
def create_employee(payload: EmployeeCreateRequest, db: Session = Depends(get_db)):
    try:
        return EmployeeService(db).create_employee(payload)
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.get("/employees/{employee_id}", response_model=EmployeeResponse)
def get_employee(employee_id: int, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


@router.patch("/employees/{employee_id}", response_model=EmployeeResponse)
def patch_employee(employee_id: int, payload: EmployeePatchRequest, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    try:
        return EmployeeService(db).patch_employee(employee, payload)
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.post("/employees/{employee_id}/external-mappings/jibble")
def map_jibble_employee(employee_id: int, payload: JibbleMappingRequest, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    try:
        return EmployeeService(db).map_jibble_identity(employee, payload.external_person_id)
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.get("/attendance/external-identities")
def list_external_identities(source: str = Query(SOURCE_JIBBLE), mapped: bool | None = Query(None), db: Session = Depends(get_db)):
    if source != SOURCE_JIBBLE:
        raise HTTPException(status_code=400, detail="source must be JIBBLE")
    query = db.query(ExternalEmployeeIdentity).filter(ExternalEmployeeIdentity.source == source)
    if mapped is True:
        query = query.filter(ExternalEmployeeIdentity.employee_id != None)
    elif mapped is False:
        query = query.filter(ExternalEmployeeIdentity.employee_id == None)
    rows = query.order_by(ExternalEmployeeIdentity.external_name.asc()).all()
    return [
        {
            "external_person_id": row.external_person_id,
            "external_code": row.external_code,
            "external_name": row.external_name,
            "external_timezone": row.external_timezone,
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
        }
        for row in rows
    ]


@router.get("/employees/{employee_id}/attendance-policies", response_model=list[AttendancePolicyResponse])
def list_attendance_policies(employee_id: int, db: Session = Depends(get_db)):
    if not db.query(Employee).filter(Employee.id == employee_id).first():
        raise HTTPException(status_code=404, detail="Employee not found")
    policies = (
        db.query(EmployeeAttendancePolicy)
        .filter(EmployeeAttendancePolicy.employee_id == employee_id)
        .order_by(EmployeeAttendancePolicy.effective_from.desc())
        .all()
    )
    return [policy_response(policy) for policy in policies]


@router.post("/employees/{employee_id}/attendance-policies", response_model=AttendancePolicyResponse)
def create_attendance_policy(employee_id: int, payload: AttendancePolicyRequest, db: Session = Depends(get_db)):
    if not db.query(Employee).filter(Employee.id == employee_id).first():
        raise HTTPException(status_code=404, detail="Employee not found")
    try:
        return policy_response(AttendancePolicyService(db).create_policy(employee_id, payload))
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.patch("/employees/{employee_id}/attendance-policies/{policy_id}", response_model=AttendancePolicyResponse)
def patch_attendance_policy(employee_id: int, policy_id: int, payload: AttendancePolicyPatchRequest, db: Session = Depends(get_db)):
    policy = (
        db.query(EmployeeAttendancePolicy)
        .filter(EmployeeAttendancePolicy.id == policy_id, EmployeeAttendancePolicy.employee_id == employee_id)
        .first()
    )
    if not policy:
        raise HTTPException(status_code=404, detail="Attendance policy not found")
    try:
        return policy_response(AttendancePolicyService(db).patch_policy(policy, payload))
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.post("/attendance/recalculate", response_model=RecalculateResponse)
def recalculate_attendance(month: str = Query(...), employee_id: int | None = Query(None), db: Session = Depends(get_db)):
    try:
        stats = AttendanceRecalculationService(db).recalculate_month(month, employee_id)
        db.commit()
        return stats.as_dict()
    except ValueError as exc:
        raise bad_request(exc) from exc


@router.get("/attendance/daily", response_model=list[DailyAttendanceResponse])
def get_daily_attendance(
    month: str = Query(...),
    employee_id: int | None = Query(None),
    status: str | None = Query(None),
    needs_review: bool | None = Query(None),
    db: Session = Depends(get_db),
):
    try:
        start, end = month_bounds(month)
    except ValueError as exc:
        raise bad_request(exc) from exc

    query = (
        db.query(CalculatedDailyAttendance, Employee, ImportedAttendanceDay, JibbleSyncRun)
        .join(Employee, Employee.id == CalculatedDailyAttendance.employee_id)
        .outerjoin(ImportedAttendanceDay, ImportedAttendanceDay.id == CalculatedDailyAttendance.imported_attendance_day_id)
        .outerjoin(JibbleSyncRun, JibbleSyncRun.id == ImportedAttendanceDay.last_sync_run_id)
        .filter(CalculatedDailyAttendance.attendance_date >= start, CalculatedDailyAttendance.attendance_date <= end)
    )
    if employee_id:
        query = query.filter(CalculatedDailyAttendance.employee_id == employee_id)
    if status:
        query = query.filter(CalculatedDailyAttendance.calculated_status == status)
    if needs_review is not None:
        query = query.filter(CalculatedDailyAttendance.needs_review == needs_review)
    rows = query.order_by(Employee.full_name.asc(), CalculatedDailyAttendance.attendance_date.asc()).all()
    response: list[dict[str, Any]] = []
    for calculation, employee, imported_day, sync_run in rows:
        response.append({
            "employee_id": employee.id,
            "employee_name": employee.full_name,
            "date": calculation.attendance_date,
            "calculated_status": calculation.calculated_status,
            "first_in": calculation.first_in_local,
            "last_out": calculation.last_out_local,
            "worked_minutes": calculation.worked_minutes,
            "payroll_minutes": calculation.payroll_minutes,
            "late_minutes": calculation.late_minutes,
            "early_departure_minutes": calculation.early_departure_minutes,
            "overtime_minutes": calculation.overtime_minutes,
            "suggested_deduction_units": calculation.suggested_deduction_units,
            "needs_review": calculation.needs_review,
            "review_reason": calculation.review_reason,
            "source_sync_timestamp": sync_run.completed_at if sync_run else None,
            "policy_id": calculation.policy_id,
            "calculation_version": calculation.calculation_version,
        })
    return response
