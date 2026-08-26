import asyncio
import logging
from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote, RateCompareResponse, ProviderStatus
from app.rates.providers.registry import provider_registry

logger = logging.getLogger(__name__)

class RateService:
    """
    Core service for comparing rates across all enabled providers.
    """
    
    async def get_all_quotes(self, request: ShipmentRequest) -> RateCompareResponse:
        """
        Fetches quotes from all registered providers concurrently.
        """
        all_providers = provider_registry.get_all_providers()
        if request.providers:
            providers = [p for p in all_providers if p.provider_code in request.providers]
            if not providers:
                logger.warning(f"No valid providers found for requested codes: {request.providers}. Defaulting to all.")
                providers = all_providers
        else:
            providers = all_providers
        
        # We use asyncio.gather to fetch quotes concurrently, ignoring failures of individual providers
        tasks = []
        for provider in providers:
            tasks.append(self._fetch_provider_quotes(provider, request))
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_quotes: List[ShipmentQuote] = []
        provider_statuses: List[ProviderStatus] = []
        
        for provider, result in zip(providers, results):
            if isinstance(result, Exception):
                logger.error("Provider %s failed to fetch quotes: %s", provider.provider_name, result)
                provider_statuses.append(
                    ProviderStatus(provider=provider.provider_name, status="FAILED", message=str(result))
                )
                continue
            
            all_quotes.extend(result)
            provider_statuses.append(
                ProviderStatus(provider=provider.provider_name, status="SUCCESS")
            )
            
        # Optional: Sort by total price
        all_quotes.sort(key=lambda q: q.totalPrice)
        
        return RateCompareResponse(
            quotes=all_quotes,
            providers=provider_statuses
        )

    async def _fetch_provider_quotes(self, provider, request: ShipmentRequest) -> List[ShipmentQuote]:
        return await provider.get_quotes(request)

rate_service = RateService()
