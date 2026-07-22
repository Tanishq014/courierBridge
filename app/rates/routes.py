from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.rates.domain.models import ShipmentRequest, RateCompareResponse
from app.rates.services.rate_service import rate_service
from app.rates.location.service import location_service
from app.rates.providers.quickship.client import QuickShipClient
from app.rates.providers.quickship.location import quickship_location_service

router = APIRouter(tags=["Rates"])
templates = Jinja2Templates(directory="app/templates")
qs_client = QuickShipClient()

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

@router.get("/api/locations/states/{country_code}")
async def get_states(country_code: str):
    """
    Returns a dictionary of states for the given ISO-2 country code.
    """
    return location_service.get_states(country_code)

@router.get("/api/locations/resolve-pincode")
async def resolve_pincode(pincode: str, country: str):
    """
    Resolves city and state from a pincode using QuickShip's master data API.
    """
    try:
        dest_country_iso3 = quickship_location_service.get_country_code(country)
        data = await qs_client.get_city_state(pincode, dest_country_iso3)
        return data if data else {}
    except Exception:
        return {}

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
