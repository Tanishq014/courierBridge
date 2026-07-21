from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.rates.domain.models import ShipmentRequest, RateCompareResponse
from app.rates.services.rate_service import rate_service
from app.rates.location.service import location_service

router = APIRouter(tags=["Rates"])
templates = Jinja2Templates(directory="app/templates")

@router.get("/rates", response_class=HTMLResponse)
async def rate_compare_page(request: Request):
    """
    Renders the UI for the rate comparison dashboard.
    """
    return templates.TemplateResponse(
        "rates/compare.html",
        {
            "request": request,
            "countries": location_service.get_countries(),
        }
    )

@router.post("/api/rates/quotes", response_model=RateCompareResponse)
async def get_quotes(request: ShipmentRequest):
    """
    API endpoint for the frontend to fetch quotes.
    Takes a canonical ShipmentRequest and returns a RateCompareResponse containing quotes and provider statuses.
    """
    try:
        quotes = await rate_service.get_all_quotes(request)
        return quotes
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error while fetching quotes.")
