from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.base import RateProvider
from .client import QuickShipClient
from .mapper import QuickShipMapper
from .location import quickship_location_service

class QuickShipProvider(RateProvider):
    def __init__(self):
        self.client = QuickShipClient()
        self.mapper = QuickShipMapper()

    @property
    def provider_code(self) -> str:
        return "quickship"

    @property
    def provider_name(self) -> str:
        return "QuickShip"

    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        """
        Implementation of the abstract method.
        Orchestrates mapping to provider DTO, calling client, and mapping back to canonical models.
        """
        req_copy = request.model_copy()
        
        # Attempt to get exact QuickShip master city/state to avoid quoting errors
        dest_country_iso3 = quickship_location_service.get_country_code(req_copy.destinationCountry)
        master_data = await self.client.get_city_state(req_copy.postalCode, dest_country_iso3)
        if master_data:
            if master_data.get("city"):
                req_copy.destinationCity = master_data["city"]
            if master_data.get("state"):
                req_copy.destinationState = master_data["state"]

        try:
            provider_req = self.mapper.to_provider_request(req_copy)
            provider_resp = await self.client.get_quotes(provider_req)
            quotes = self.mapper.to_domain_quotes(provider_resp)
            return quotes
        except Exception as e:
            return []
