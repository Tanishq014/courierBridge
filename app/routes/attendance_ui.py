from fastapi import APIRouter, Request, Depends, Query
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, date

from app.database import get_db
from app.models import Employee, ExternalEmployeeIdentity
from app.attendance_services import month_bounds, now_utc, as_kolkata, KOLKATA
from app.routes.attendance import get_daily_attendance, list_external_identities

router = APIRouter(tags=["attendance-ui"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/attendance")
def attendance_dashboard(
    request: Request, 
    month: str | None = None,
    db: Session = Depends(get_db)
):
    # Default to current month if not provided
    if not month:
        now = datetime.now(KOLKATA)
        month = now.strftime("%Y-%m")
        
    try:
        # Re-use the existing logic to fetch daily attendance
        records = get_daily_attendance(month=month, employee_id=None, status=None, needs_review=None, db=db)
    except ValueError:
        records = []
        
    return templates.TemplateResponse("attendance/dashboard.html", {
        "request": request,
        "month": month,
        "records": records
    })

@router.get("/attendance/employees")
def attendance_employees(
    request: Request,
    db: Session = Depends(get_db)
):
    # Fetch local employees
    employees = db.query(Employee).order_by(Employee.full_name.asc()).all()
    
    # Fetch Jibble unmapped identities
    try:
        unmapped_identities = list_external_identities(source="JIBBLE", mapped=False, db=db)
    except Exception:
        unmapped_identities = []
        
    return templates.TemplateResponse("attendance/employees.html", {
        "request": request,
        "employees": employees,
        "unmapped_identities": unmapped_identities
    })
