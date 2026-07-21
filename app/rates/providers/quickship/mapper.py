from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from .dtos import QuickShipQuoteRequest, QuickShipQuoteResponse

class QuickShipMapper:
    """
    Translates between canonical domain models and QuickShip DTOs.
    """
    
    @staticmethod
    def to_provider_request(request: ShipmentRequest) -> QuickShipQuoteRequest:
        """
        Maps a canonical ShipmentRequest to a QuickShipQuoteRequest.
        Location normalization happens here.
        """
        # Placeholder
        return QuickShipQuoteRequest()

    @staticmethod
    def to_domain_quotes(response: QuickShipQuoteResponse) -> List[ShipmentQuote]:
        """
        Maps a QuickShipQuoteResponse to a list of canonical ShipmentQuotes.
        """
        # Placeholder
        return []
