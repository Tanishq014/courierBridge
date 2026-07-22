from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.base import RateProvider
from app.rates.providers.atlantic.client import AtlanticClient

class AtlanticProvider(RateProvider):
    def __init__(self):
        self.client = AtlanticClient()

    @property
    def provider_code(self) -> str:
        return "atlantic"

    @property
    def provider_name(self) -> str:
        return "Atlantic Logistics"

    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        return await self.client.get_quotes(request)
