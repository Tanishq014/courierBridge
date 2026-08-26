from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.base import RateProvider
from app.rates.providers.skynet.client import SkyNetClient

class SkyNetProvider(RateProvider):
    def __init__(self):
        self.client = SkyNetClient()

    @property
    def provider_code(self) -> str:
        return "skynet"

    @property
    def provider_name(self) -> str:
        return "SkyNet"

    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        return await self.client.get_quotes(request)
