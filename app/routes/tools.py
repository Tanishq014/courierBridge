from fastapi import APIRouter, Request, Form
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/tools")
templates = Jinja2Templates(directory="app/templates")

@router.get("/quick-add")
def quick_add_form(request: Request):
    return templates.TemplateResponse("tools/quick_add.html", {"request": request})

@router.post("/quick-add/parse")
def parse_quick_add(
    request: Request,
    raw_text: str = Form(...)
):
    # This is a placeholder for AI parsing
    # TODO: Integrate with Gemini API to parse raw_text into JSON
    
    # Rule-based mock parsing for MVP
    # Example: "rahul 98765 canada 2.4kg charge 3200 cost 2450 india awb dtdc123 eta 7-10 days pending"
    
    parsed_data = {
        "customer_name": "Mock Name",
        "name_country_raw": "Mock Name / Canada",
        "destination_country": "Canada",
        "item_category": "documents",
        "charged_weight": 2.4,
        "billed_amount": 3200,
        "self_cost": 2450,
        "received_amount": 0,
        "promised_days_text": "7-10 days",
        "status_raw_text": "PENDING CUSTOM",
        "courier_company": "DTDC",
        "vendor_partner": "Self",
        "raw_text": raw_text
    }
    
    return templates.TemplateResponse("tools/quick_add.html", {
        "request": request,
        "parsed_data": parsed_data,
        "raw_text": raw_text
    })
