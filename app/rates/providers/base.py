from abc import ABC, abstractmethod
from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote

class RateProvider(ABC):
    """
    Abstract base class for all rate providers.
    Each provider must implement the get_quotes method to translate the canonical
    ShipmentRequest into provider-specific requests, call their API, and return
    canonical ShipmentQuotes.
    """
    
    @property
    @abstractmethod
    def provider_code(self) -> str:
        """Returns the internal code of the provider."""
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Returns the display name of the provider."""
        pass

    @abstractmethod
    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        """
        Fetches quotes from the provider for the given canonical shipment request.
        """
        pass
