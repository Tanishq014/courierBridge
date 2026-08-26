from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.base import RateProvider
from .client import ShipglobalClient
from .mapper import ShipglobalMapper

class ShipglobalProvider(RateProvider):
    def __init__(self):
        self.client = ShipglobalClient()
        self.mapper = ShipglobalMapper()

    @property
    def provider_code(self) -> str:
        return "shipglobal"

    @property
    def provider_name(self) -> str:
        return "Shipglobal"

    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        """
        Implementation of the abstract method.
        Orchestrates mapping to provider format, calling client, and mapping back.
        """
        # Validate or adjust request if needed
        req_copy = request.model_copy()
        
        # 1. Map to Provider Request
        provider_req = self.mapper.to_provider_request(req_copy)
        
        # 2. Call API
        provider_resp = await self.client.get_quotes(provider_req)
        
        # 3. Map to Domain Quotes
        quotes = self.mapper.to_domain_quotes(provider_resp)
        return quotes
