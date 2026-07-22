from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.base import RateProvider
from app.rates.providers.maww.client import MawwClient

class MawwProvider(RateProvider):
    def __init__(self):
        self.client = MawwClient()

    @property
    def provider_code(self) -> str:
        return "maww"

    @property
    def provider_name(self) -> str:
        return "MAWW Logistics"

    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        return await self.client.get_quotes(request)
