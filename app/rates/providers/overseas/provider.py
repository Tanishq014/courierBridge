from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.base import RateProvider
from .mapper import OverseasMapper
from .client import OverseasClient
from .parser import OverseasParser

class OverseasProvider(RateProvider):
    def __init__(self):
        self.client = OverseasClient()
        self.mapper = OverseasMapper()
        self.parser = OverseasParser()

    @property
    def provider_code(self) -> str:
        return "overseas"

    @property
    def provider_name(self) -> str:
        return "Overseas Logistics"

    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        # 1. Map to provider request
        payload = self.mapper.to_provider_request(request)
        
        # 2. Fetch HTML from provider
        html_response = await self.client.get_rates(payload)
        
        # 3. Parse HTML into canonical quotes
        quotes = self.parser.parse_rates(html_response)
        
        # Fill in request-specific info
        for quote in quotes:
            # The payload has total chargeable weight calculated
            quote.chargeableWeight = payload["ChgWeight"]
            quote.volumetricWeight = sum(p["VolumetricWt"] for p in payload["PiecesDetailsTable"])
            quote.deadWeight = sum(p["ActualWt"] for p in payload["PiecesDetailsTable"])
        
        return quotes
