from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.base import RateProvider
from .client import QuickShipClient
from .mapper import QuickShipMapper

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
        provider_req = self.mapper.to_provider_request(request)
        provider_resp = await self.client.get_quotes(provider_req)
        quotes = self.mapper.to_domain_quotes(provider_resp)
        return quotes
